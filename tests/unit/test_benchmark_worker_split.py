"""Phase 2 (benchmark-worker process split) — coordinator / worker role gates.

The split runs the benchmark sim in a SEPARATE container (same image,
``BENCHMARK_WORKER_ONLY=1``) that SHARES ``solver_rounds.json`` on the ``/data``
volume with the api coordinator. Two process-unsafe side-effects had to be
gated so the second process is a safe, read-only sharer of round state:

  * ``EpochManager(coordinator_runs_slate=False)`` — the api must NOT drive the
    full-slate ``run_once`` on its own loop once a worker owns the slate (else
    the split achieves nothing: the sim still stalls the api loop). It still
    re-benches the single incumbent (a separate path), so the flag gates ONLY
    the slate ``run_once``.
  * ``RoundStore(sweep_orphan_temps=False)`` — the worker ``_load``s the shared
    file read-only; its orphan-temp sweep would ``glob`` + ``unlink`` the api
    coordinator's in-flight ``mkstemp`` persist temp between the api's
    ``mkstemp`` and ``os.replace`` → a silently-lost round / champion write.

Both default to the unchanged monolith behavior; only the worker flips them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from unittest.mock import AsyncMock, MagicMock

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.round_store import RoundStore
from minotaur_subnet.harness.submission_store import SubmissionStore


# ── coordinator_runs_slate gate ─────────────────────────────────────────────


def _mock_worker():
    worker = MagicMock()
    worker.run_once = AsyncMock()
    return worker


def _mock_block_loop():
    loop = MagicMock()
    loop.set_solver = MagicMock()
    return loop


@pytest.mark.asyncio
async def test_coordinator_runs_slate_false_skips_slate_run_once():
    """With the split active the api coordinator must NOT drive the full-slate
    benchmark on its own event loop — that is the worker's job now."""
    worker = _mock_worker()
    mgr = EpochManager(
        block_loop=_mock_block_loop(),
        benchmark_worker=worker,
        submission_store=SubmissionStore(),
        coordinator_runs_slate=False,
    )

    await mgr.on_epoch_boundary(epoch=1)

    assert worker.run_once.await_count == 0, (
        "coordinator ran the slate run_once despite coordinator_runs_slate=False "
        "— the sim would stall the api loop and the split would achieve nothing"
    )


@pytest.mark.asyncio
async def test_coordinator_runs_slate_default_drives_run_once():
    """Control: the monolith default (True) DOES drive the slate — proving the
    skip above is the flag's doing, not an unrelated early return."""
    worker = _mock_worker()
    mgr = EpochManager(
        block_loop=_mock_block_loop(),
        benchmark_worker=worker,
        submission_store=SubmissionStore(),
        # coordinator_runs_slate defaults True
    )

    await mgr.on_epoch_boundary(epoch=1)

    assert worker.run_once.await_count >= 1, (
        "monolith default must still run the slate benchmark"
    )


def test_coordinator_runs_slate_defaults_true():
    """The flag defaults to the monolith behavior so every existing caller and
    the worker process itself keep running the slate unchanged."""
    mgr = EpochManager(
        block_loop=_mock_block_loop(),
        benchmark_worker=_mock_worker(),
        submission_store=SubmissionStore(),
    )
    assert mgr._coordinator_runs_slate is True


# ── RoundStore no-sweep (read-only sharer) gate ─────────────────────────────


def test_worker_no_sweep_preserves_api_inflight_persist_temp(tmp_path: Path):
    """A ``sweep_orphan_temps=False`` sharer (the worker) must NOT unlink the
    api coordinator's in-flight ``.<name>.<rand>.tmp`` persist temp on ``_load``
    — doing so would drop the round/champion write the api is mid-``os.replace``
    on the shared volume."""
    p = tmp_path / "solver_rounds.json"
    # api coordinator writes the real file.
    api_store = RoundStore(persist_path=p)
    api_store.ensure_open_round(opened_epoch=7)
    assert p.exists()

    # Simulate the api's in-flight persist temp (created by mkstemp, not yet
    # os.replace'd) sitting on the shared /data volume.
    inflight = tmp_path / f".{p.name}.abc123.tmp"
    inflight.write_text("{}")

    # The worker reloads read-only. It must leave the api's temp alone.
    worker_store = RoundStore(persist_path=p, sweep_orphan_temps=False)
    worker_store._load()
    assert inflight.exists(), (
        "worker (no-sweep) deleted the api coordinator's in-flight persist temp "
        "— a round/champion write would be silently lost"
    )

    # Control: the sole-writer default DOES sweep leftover temps (crash cleanup).
    api_store._load()
    assert not inflight.exists(), "sole-writer default must sweep orphan temps"


def test_sweep_flag_defaults_true(tmp_path: Path):
    """Default construction keeps the sole-writer crash-cleanup sweep enabled."""
    store = RoundStore(persist_path=tmp_path / "solver_rounds.json")
    assert store._sweep_orphan_temps_enabled is True


# ── worker fork-isolation boot guard ────────────────────────────────────────


def test_worker_fork_guard_rejects_shared_api_forks(monkeypatch):
    """The worker forking on the api's SHARED anvils (…-eth/-base/-btevm) is a
    consensus hazard — snapshot/revert races corrupt both processes' scoring —
    so the boot guard must fail-closed."""
    from minotaur_subnet.api import startup

    monkeypatch.delenv("BENCHMARK_WORKER_ALLOW_SHARED_FORKS", raising=False)
    monkeypatch.setattr(
        "minotaur_subnet.chains.wiring.sim_rpc_urls",
        lambda: {1: "http://anvil-eth:8545", 8453: "http://anvil-base:8546"},
    )
    with pytest.raises(RuntimeError, match="SHARED simulator fork"):
        startup._assert_worker_forks_isolated()


def test_worker_fork_guard_accepts_dedicated_bench_forks(monkeypatch):
    """Dedicated anvil-*-bench forks (isolated from the api) pass the guard."""
    from minotaur_subnet.api import startup

    monkeypatch.delenv("BENCHMARK_WORKER_ALLOW_SHARED_FORKS", raising=False)
    monkeypatch.setattr(
        "minotaur_subnet.chains.wiring.sim_rpc_urls",
        lambda: {
            1: "http://anvil-eth-bench:8545",
            8453: "http://anvil-base-bench:8546",
            964: "http://anvil-btevm-bench:8547",
        },
    )
    startup._assert_worker_forks_isolated()  # must not raise


def test_worker_fork_guard_escape_hatch(monkeypatch):
    """The single-fork dev/test escape hatch disables the guard."""
    from minotaur_subnet.api import startup

    monkeypatch.setenv("BENCHMARK_WORKER_ALLOW_SHARED_FORKS", "1")
    monkeypatch.setattr(
        "minotaur_subnet.chains.wiring.sim_rpc_urls",
        lambda: {1: "http://anvil-eth:8545"},
    )
    startup._assert_worker_forks_isolated()  # escape hatch → no raise


# ── worker forward-progress heartbeat ───────────────────────────────────────


def test_worker_heartbeat_noop_in_monolith(tmp_path: Path, monkeypatch):
    """No heartbeat file in the monolith api (BENCHMARK_WORKER_ONLY unset) — the
    split-container healthcheck must not leak into normal api operation."""
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    hb = tmp_path / "hb"
    monkeypatch.setenv("BENCHMARK_WORKER_HEARTBEAT_FILE", str(hb))
    monkeypatch.delenv("BENCHMARK_WORKER_ONLY", raising=False)
    w = object.__new__(BenchmarkWorker)  # bypass heavy __init__; helper is self-contained
    w._touch_worker_heartbeat()
    assert not hb.exists()


def test_worker_heartbeat_written_in_worker_mode(tmp_path: Path, monkeypatch):
    """The split worker bumps its heartbeat file so a wedged loop goes stale and
    the container healthcheck can catch it."""
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

    hb = tmp_path / "hb"
    monkeypatch.setenv("BENCHMARK_WORKER_HEARTBEAT_FILE", str(hb))
    monkeypatch.setenv("BENCHMARK_WORKER_ONLY", "1")
    w = object.__new__(BenchmarkWorker)
    w._touch_worker_heartbeat()
    assert hb.exists()
    assert float(hb.read_text()) > 0


# ── GET /solver/round is READ-ONLY on the worker ────────────────────────────


@pytest.mark.asyncio
async def test_get_solver_round_readonly_on_worker(tmp_path: Path, monkeypatch):
    """On the worker, GET /solver/round must NOT hit the writing helper
    (_get_current_solver_round does set_active_champion + ensure_open_round) — it
    serves a pure read of the shared round store."""
    from minotaur_subnet.api.routes.submissions import routes, state
    from minotaur_subnet.harness.round_store import RoundStore

    rs = RoundStore(persist_path=tmp_path / "sr.json")
    rs.ensure_open_round(opened_epoch=3)
    state.set_round_store(rs)
    monkeypatch.setenv("BENCHMARK_WORKER_ONLY", "1")
    calls = {"n": 0}

    def _boom(**_kw):
        calls["n"] += 1
        raise AssertionError("worker must not call the round-store write helper")

    monkeypatch.setattr(routes, "_get_current_solver_round", _boom)
    try:
        resp = await routes.get_solver_round()
        assert calls["n"] == 0
        assert resp.round_id == "round-e3-n1"
    finally:
        state.set_round_store(None)


@pytest.mark.asyncio
async def test_get_solver_round_uses_write_helper_off_worker(tmp_path: Path, monkeypatch):
    """Control: without the worker flag the endpoint keeps its lazy create/sync
    (calls the write helper) — the monolith/coordinator behavior is unchanged."""
    from minotaur_subnet.api.routes.submissions import routes, state
    from minotaur_subnet.harness.round_store import RoundStore

    rs = RoundStore(persist_path=tmp_path / "sr.json")
    opened = rs.ensure_open_round(opened_epoch=4)
    state.set_round_store(rs)
    monkeypatch.delenv("BENCHMARK_WORKER_ONLY", raising=False)
    calls = {"n": 0}

    def _spy(**_kw):
        calls["n"] += 1
        return opened

    monkeypatch.setattr(routes, "_get_current_solver_round", _spy)
    try:
        await routes.get_solver_round()
        assert calls["n"] == 1
    finally:
        state.set_round_store(None)
