"""Per-chain (multi-deployment) benchmarking — gated by BENCHMARK_ALL_DEPLOYMENT_CHAINS.

Covers the four seams that let an app deployed on more than one chain get every
chain's scenarios benchmarked and each chain independently promoted SOLVING→SOLVED:

1. ``_benchmark_deployments_for_app`` — primary-only (off) vs all-chains (on).
2. ``_load_benchmark_intents`` — one intent per (app, chain) when armed.
3. ``_transition_solving_apps`` — promotes the correct per-chain deployment from
   the per_intent ``chain_id``, not just the primary (the old chain-less lookup).
4. ``build_pin_blocks`` + ``set_fork_pins`` + ``_apply_round_anchored_pin`` —
   per-chain fork pins threaded end-to-end, with a missing pin failing loud.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.submission_store import SubmissionStore
from minotaur_subnet.shared.types import AppStatus


ETH = 1
BASE = 8453


def _dep(chain_id: int, status: AppStatus, addr: str = "0xabc"):
    return SimpleNamespace(
        chain_id=chain_id, status=status, contract_address=addr, app_id="app_x",
    )


def _app_store(deployments_by_chain: dict[int, object]):
    """Fake AppIntentStore. ``get_deployment`` mimics the real order-ready-preferred
    primary resolution; ``get_deployments`` returns the full per-chain map."""
    store = MagicMock()
    store.list_apps.return_value = [SimpleNamespace(app_id="app_x")]
    store.get_deployments.return_value = dict(deployments_by_chain)

    def _get_deployment(app_id, chain_id=None):
        if chain_id is not None:
            return deployments_by_chain.get(chain_id)
        # Primary: prefer order-ready, then operational, then any (real semantics).
        for d in deployments_by_chain.values():
            if d.status.is_order_ready():
                return d
        for d in deployments_by_chain.values():
            if d.status.is_operational():
                return d
        return next(iter(deployments_by_chain.values()), None)

    store.get_deployment.side_effect = _get_deployment
    return store


# ── _benchmark_deployments_for_app ────────────────────────────────────────────


def test_deployments_for_app_primary_only_when_flag_off(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "0")  # default is now ON; test explicit OFF
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: _dep(ETH, AppStatus.SOLVING)})
    worker = BenchmarkWorker(SubmissionStore(), app_store=store)
    deps = worker._benchmark_deployments_for_app("app_x")
    # Primary resolution prefers the order-ready Base deployment.
    assert [d.chain_id for d in deps] == [BASE]


def test_deployments_for_app_all_chains_when_flag_on(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "1")
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: _dep(ETH, AppStatus.SOLVING)})
    worker = BenchmarkWorker(SubmissionStore(), app_store=store)
    deps = worker._benchmark_deployments_for_app("app_x")
    assert sorted(d.chain_id for d in deps) == [ETH, BASE]


# ── _load_benchmark_intents ───────────────────────────────────────────────────


def test_load_intents_one_per_chain_when_armed(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "1")
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: _dep(ETH, AppStatus.SOLVING)})
    worker = BenchmarkWorker(SubmissionStore(), app_store=store)
    intents = worker._load_benchmark_intents()
    assert sorted(state.chain_id for _app, state, _snap in intents) == [ETH, BASE]


def test_load_intents_primary_only_when_off(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "0")  # default is now ON; test explicit OFF
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: _dep(ETH, AppStatus.SOLVING)})
    worker = BenchmarkWorker(SubmissionStore(), app_store=store)
    intents = worker._load_benchmark_intents()
    assert [state.chain_id for _app, state, _snap in intents] == [BASE]


def test_load_intents_status_filter_still_applies(monkeypatch):
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "1")
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: _dep(ETH, AppStatus.SOLVING)})
    worker = BenchmarkWorker(SubmissionStore(), app_store=store)
    intents = worker._load_benchmark_intents(deployment_statuses={AppStatus.SOLVING})
    # Only the SOLVING (Ethereum) deployment survives the status filter — this is
    # exactly the champion-bootstrap path that used to miss chain 1 entirely.
    assert [state.chain_id for _app, state, _snap in intents] == [ETH]


# ── _transition_solving_apps (per-chain) ──────────────────────────────────────


def _sub_store_with_details(per_intent):
    store = MagicMock()
    refreshed = SimpleNamespace(benchmark_details={"per_intent": per_intent})
    store.get.return_value = refreshed
    return store


def test_transition_promotes_only_scored_chain():
    """chain-1 scenario scored > 0 → the chain-1 SOLVING deployment promotes; the
    Base deployment (already SOLVED) is untouched."""
    base_dep = _dep(BASE, AppStatus.SOLVED)
    eth_dep = _dep(ETH, AppStatus.SOLVING)
    store = _app_store({BASE: base_dep, ETH: eth_dep})
    sub_store = _sub_store_with_details([
        {"intent_id": "app_x:WETH_to_USDC", "chain_id": ETH, "score": 0.9},
        {"intent_id": "app_x:USDC_to_WETH", "chain_id": BASE, "score": 0.8},
    ])
    worker = BenchmarkWorker(sub_store, app_store=store)
    worker._transition_solving_apps([SimpleNamespace(submission_id="s1")])

    store.update_deployment_status.assert_called_once_with("app_x", ETH, AppStatus.SOLVED)


def test_transition_no_promote_when_chain_scored_zero():
    eth_dep = _dep(ETH, AppStatus.SOLVING)
    store = _app_store({BASE: _dep(BASE, AppStatus.SOLVED), ETH: eth_dep})
    sub_store = _sub_store_with_details([
        {"intent_id": "app_x:WETH_to_USDC", "chain_id": ETH, "score": 0.0},
    ])
    worker = BenchmarkWorker(sub_store, app_store=store)
    worker._transition_solving_apps([SimpleNamespace(submission_id="s1")])
    store.update_deployment_status.assert_not_called()


def test_transition_legacy_none_chain_falls_back_to_primary():
    """A legacy row with no chain_id resolves to the app's primary deployment."""
    solving_primary = _dep(BASE, AppStatus.SOLVING)
    store = _app_store({BASE: solving_primary})
    sub_store = _sub_store_with_details([
        {"intent_id": "app_x:scenario", "chain_id": None, "score": 0.7},
    ])
    worker = BenchmarkWorker(sub_store, app_store=store)
    worker._transition_solving_apps([SimpleNamespace(submission_id="s1")])
    store.update_deployment_status.assert_called_once_with("app_x", BASE, AppStatus.SOLVED)


# ── build_pin_blocks (per-chain map) ──────────────────────────────────────────


def test_build_pin_blocks_scalar_unchanged():
    import minotaur_subnet.harness.solver_read_proxy as srp
    cfg = srp.ReadProxyConfig(url="u", control_url="c", token="", chain_ids=(BASE,))
    assert srp.build_pin_blocks(cfg, {BASE: "u_base"}, 12345) == {"base": 12345}


def test_build_pin_blocks_map_per_chain():
    import minotaur_subnet.harness.solver_read_proxy as srp
    cfg = srp.ReadProxyConfig(url="u", control_url="c", token="", chain_ids=(BASE, ETH))
    out = srp.build_pin_blocks(cfg, {BASE: "u_base", ETH: "u_eth"}, {BASE: 100, ETH: 200})
    assert out == {"base": 100, "eth": 200}


def test_build_pin_blocks_map_missing_chain_raises():
    import minotaur_subnet.harness.solver_read_proxy as srp
    cfg = srp.ReadProxyConfig(url="u", control_url="c", token="", chain_ids=(BASE, ETH))
    with pytest.raises(ValueError):
        srp.build_pin_blocks(cfg, {BASE: "u_base", ETH: "u_eth"}, {BASE: 100})  # no eth pin


# ── set_fork_pins / _apply_round_anchored_pin ─────────────────────────────────


def test_set_fork_pins_updates_primary_scalar():
    worker = BenchmarkWorker(SubmissionStore())
    worker.set_fork_pins({BASE: 500, ETH: 900})
    assert worker._fork_pins == {BASE: 500, ETH: 900}
    # Primary anchor chain (Base) also updates the scalar for the legacy path.
    assert worker._epoch_block_number == 500


def test_set_fork_pins_none_clears():
    worker = BenchmarkWorker(SubmissionStore())
    worker.set_fork_pins({BASE: 500})
    worker.set_fork_pins(None)
    assert worker._fork_pins is None


def test_apply_round_anchored_pin_accepts_map(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    worker = BenchmarkWorker(
        SubmissionStore(), pin_resolver=lambda rid: {BASE: 500, ETH: 900},
    )
    worker._apply_round_anchored_pin("r1")
    assert worker._fork_pins == {BASE: 500, ETH: 900}
    assert worker._epoch_block_number == 500


def test_apply_round_anchored_pin_accepts_scalar(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    worker = BenchmarkWorker(SubmissionStore(), pin_resolver=lambda rid: 777)
    worker._apply_round_anchored_pin("r1")
    assert worker._fork_pins is None
    assert worker._epoch_block_number == 777


# ── startup: pin-chain set + leader resolver map form ─────────────────────────


def test_benchmark_pin_chains_base_only_when_off(monkeypatch):
    from minotaur_subnet.api import startup
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "0")  # default now ON
    with patch.object(startup, "_deployment_chains", return_value=[ETH, BASE]):
        # Off → the deployment chains are ignored; anchor set (Base) only.
        assert startup._benchmark_pin_chains() == [BASE]


def test_benchmark_pin_chains_union_when_on(monkeypatch):
    from minotaur_subnet.api import startup
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "1")
    with patch.object(startup, "_deployment_chains", return_value=[ETH, BASE]):
        assert startup._benchmark_pin_chains() == [ETH, BASE]


def test_leader_resolver_returns_map_when_on(monkeypatch):
    from minotaur_subnet.api import startup
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "1")
    with patch.object(startup, "_resolve_round_fork_pins", return_value={BASE: 500, ETH: 900}):
        assert startup._leader_fork_pin_resolver("r1") == {BASE: 500, ETH: 900}


def test_leader_resolver_returns_scalar_when_off(monkeypatch):
    from minotaur_subnet.api import startup
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "0")  # default now ON
    with patch.object(startup, "_resolve_round_fork_pins", return_value={BASE: 500, ETH: 900}):
        # Off → the bare primary-chain (Base) block, byte-identical to before.
        assert startup._leader_fork_pin_resolver("r1") == 500


def test_flag_default_is_on(monkeypatch):
    """2026-07-08 operator decision: BENCHMARK_ALL_DEPLOYMENT_CHAINS default ON.
    Unset ⇒ enabled (multi-chain corpus); explicit {0,false,no,off} disables."""
    from minotaur_subnet.consensus.round_anchor import benchmark_all_deployment_chains_enabled
    monkeypatch.delenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", raising=False)
    assert benchmark_all_deployment_chains_enabled() is True
    monkeypatch.setenv("BENCHMARK_ALL_DEPLOYMENT_CHAINS", "off")
    assert benchmark_all_deployment_chains_enabled() is False
