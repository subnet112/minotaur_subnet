"""Tests for the LIVE champion keyless+metered RPC proxy path and the
live-network internal-only guard.

The live champion historically got the KEYED ``BASE_RPC_URL`` (env + init_cfg)
and ran on a non-internal net — a hostile champion could read the provider API
key and exfiltrate user order data. This suite pins the hardening:

  - ``solver_read_proxy.live_read_proxy_config()`` is OPT-IN + FAIL-SAFE.
  - ``DockerRuntimeSolver`` resets the proxy budget PER ORDER (BLIND-3 metering)
    and closes the session on shutdown, only when the feature is on.
  - ``orchestrator``'s live-net guard warns by default and hard-fails on an
    explicitly non-internal net only when ``LIVE_SOLVER_REQUIRE_INTERNAL=1``.

Unit-level: no Docker daemon, no proxy container — the orchestrator's
``start_docker`` and the proxy control calls are stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness import solver_read_proxy as srp
from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata


# ── live_read_proxy_config: opt-in + fail-safe ──────────────────────────────


def test_live_config_disabled_by_default(monkeypatch):
    """Feature ships INERT: no env => None => champion keeps direct RPC."""
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    monkeypatch.delenv("SOLVER_LIVE_RPC_PROXY", raising=False)
    assert srp.live_read_proxy_config() is None
    assert srp.live_rpc_via_proxy_enabled() is False


def test_live_config_fail_safe_when_url_missing(monkeypatch):
    """Enabled but the proxy hasn't exported its live URL yet => None (fail-safe:
    never point the champion at an unreachable proxy)."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.delenv("SOLVER_LIVE_RPC_PROXY", raising=False)
    assert srp.live_rpc_via_proxy_enabled() is True
    assert srp.live_read_proxy_config() is None


def test_live_config_resolves_keyless_and_metered(monkeypatch):
    """Enabled + URL present => a keyless, budget-enforced config; the URLs the
    solver dials carry NO API key (they point at the proxy)."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.31.0.5:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_CONTROL", "http://minotaur-rpc-pin-proxy:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "tok")
    monkeypatch.delenv("LIVE_SOLVER_RPC_BUDGET", raising=False)
    cfg = srp.live_read_proxy_config()
    assert cfg is not None
    assert cfg.url == "http://172.31.0.5:8645"
    assert cfg.control_url == "http://minotaur-rpc-pin-proxy:8645"
    assert cfg.token == "tok"
    assert cfg.budget == srp.DEFAULT_LIVE_RPC_BUDGET > 0
    url = srp.proxy_rpc_url(cfg, srp.LIVE_PROXY_SESSION_ID, 8453)
    assert url == "http://172.31.0.5:8645/rpc/live-champion/base"
    assert "alchemy" not in url and "key" not in url  # keyless


def test_live_config_budget_override(monkeypatch):
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.31.0.5:8645")
    monkeypatch.setenv("LIVE_SOLVER_RPC_BUDGET", "12345")
    assert srp.live_read_proxy_config().budget == 12345


def test_live_config_negative_budget_falls_back(monkeypatch):
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.31.0.5:8645")
    monkeypatch.setenv("LIVE_SOLVER_RPC_BUDGET", "-5")
    assert srp.live_read_proxy_config().budget == srp.DEFAULT_LIVE_RPC_BUDGET


# ── DockerRuntimeSolver: per-order metering + session lifecycle ─────────────


def _rt(*, live_cfg=None, session=None) -> DockerRuntimeSolver:
    session = session or MagicMock()
    session._closed = False
    session.quote = AsyncMock(return_value="q")
    session.generate_plan = AsyncMock(return_value="p")
    session.shutdown = AsyncMock()
    return DockerRuntimeSolver(
        session=session,
        image_ref="ghcr.io/test/solver:latest",
        metadata=SolverMetadata(name="t", version="0", author="t", description=""),
        chain_ids=[8453],
        rpc_urls={8453: "http://172.31.0.5:8645/rpc/live-champion/base"},
        live_proxy_cfg=live_cfg,
        live_proxy_session_id=(srp.LIVE_PROXY_SESSION_ID if live_cfg else None),
    )


def _intent_state_snapshot():
    intent = MagicMock(); intent.app_id = "app_test"
    state = MagicMock(); state.chain_id = 8453
    return intent, state, MarketSnapshot.empty(chain_id=8453)


@pytest.mark.asyncio
async def test_per_order_reset_when_proxy_on():
    """BLIND-3: with the live proxy on, the budget is reset before EACH order."""
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock()) as reset:
        await rt.generate_plan(intent, state, snap)
        await rt.quote(intent, state, snap)
    assert reset.await_count == 2  # one per order op
    reset.assert_awaited_with(cfg, srp.LIVE_PROXY_SESSION_ID)


@pytest.mark.asyncio
async def test_no_reset_when_proxy_off():
    """Feature off => no control-plane calls at all (zero overhead / inert)."""
    rt = _rt(live_cfg=None)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock()) as reset:
        await rt.generate_plan(intent, state, snap)
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_closes_session_when_proxy_on():
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    with patch.object(srp, "close_session", new=AsyncMock()) as close:
        await rt.shutdown()
    close.assert_awaited_once_with(cfg, srp.LIVE_PROXY_SESSION_ID)


@pytest.mark.asyncio
async def test_shutdown_no_close_when_proxy_off():
    rt = _rt(live_cfg=None)
    with patch.object(srp, "close_session", new=AsyncMock()) as close:
        await rt.shutdown()
    close.assert_not_awaited()


# ── create(): opens a metered HEAD session + routes RPC keylessly ───────────


@pytest.mark.asyncio
async def test_create_wires_keyless_proxy(monkeypatch):
    """With the feature on, create() opens a HEAD (blocks={}) session and forwards
    KEYLESS proxy URLs to the container (rpc_overrides), never the keyed URL."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.31.0.5:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "tok")
    monkeypatch.setenv("LIVE_SOLVER_NETWORK", "live-solver")

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)

    captured = {}

    async def _open(cfg, sid, blocks):
        captured["open"] = (cfg, sid, blocks)
        return {}

    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=_open), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        rt = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453],
            rpc_urls={8453: "https://base-mainnet.g.alchemy.com/v2/SECRETKEY"},
        )

    # HEAD session opened with an empty pin (latest reads), metered.
    cfg, sid, blocks = captured["open"]
    assert blocks == {} and sid == srp.LIVE_PROXY_SESSION_ID and cfg.budget > 0
    # start_docker got KEYLESS proxy URLs as rpc_overrides — the SECRETKEY never
    # reaches the container via env.
    kwargs = start.await_args.kwargs
    assert kwargs["rpc_overrides"] == {8453: "http://172.31.0.5:8645/rpc/live-champion/base"}
    assert "SECRETKEY" not in str(kwargs["rpc_overrides"])
    # init_cfg rpc_urls were rewritten to the keyless proxy URL too.
    init_cfg = fake_session.initialize.await_args.args[0]
    assert init_cfg["rpc_urls"][8453] == "http://172.31.0.5:8645/rpc/live-champion/base"
    assert rt._live_proxy_cfg is not None


@pytest.mark.asyncio
async def test_create_fails_safe_to_direct_rpc_when_disabled(monkeypatch):
    """Feature off => no session opened, keyed URL used as today, no rpc_overrides."""
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)
    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=AsyncMock(side_effect=AssertionError("must not open"))), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        rt = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453],
            rpc_urls={8453: "https://base-mainnet.g.alchemy.com/v2/SECRETKEY"},
        )
    assert rt._live_proxy_cfg is None
    assert start.await_args.kwargs["rpc_overrides"] is None


# ── orchestrator: live-net internal-only guard ──────────────────────────────


def test_require_internal_env_gating(monkeypatch):
    for v, expect in [("1", True), ("true", True), ("on", True), ("0", False), ("", False)]:
        monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", v)
        assert orch._require_internal_live_net() is expect
    monkeypatch.delenv("LIVE_SOLVER_REQUIRE_INTERNAL", raising=False)
    assert orch._require_internal_live_net() is False


@pytest.mark.asyncio
async def test_docker_network_is_internal_none_for_missing_net():
    """A network that can't be inspected => None ('unknown', never crash)."""
    assert await orch._docker_network_is_internal("no-such-net-xyz-123") is None


@pytest.mark.asyncio
async def test_start_docker_refuses_noninternal_live_net_when_required(monkeypatch):
    """live=True + a DEFINITIVELY non-internal net + REQUIRE=1 => refuse to start."""
    monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", "1")
    o = orch.SolverOrchestrator()
    with patch.object(orch, "_docker_network_is_internal", new=AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="refusing to start live champion"):
            await o.start_docker("ghcr.io/test/solver:latest", live=True, network="prod-bridge")


@pytest.mark.asyncio
async def test_start_docker_no_guard_for_benchmark(monkeypatch):
    """Benchmark (live=False) never triggers the live-net guard, even non-internal."""
    monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", "1")
    o = orch.SolverOrchestrator()
    check = AsyncMock(return_value=False)
    with patch.object(orch, "_docker_network_is_internal", new=check), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=RuntimeError("stop-before-docker"))):
        # live=False => guard skipped; it proceeds to the (stubbed) docker call.
        with pytest.raises(RuntimeError, match="stop-before-docker"):
            await o.start_docker("img:latest", live=False, network="bench-sandbox")
    check.assert_not_awaited()
