"""Explicit-order benching (distributed-veto Phase 0 worker primitive).

Pins the strict/lenient split: the canonical historical draw SKIPS
unresolvable orders (fleet-uniform), while an EXPLICIT list (veto slice /
dissent re-verify / audit) must REFUSE loudly — a silently short slice would
return a vacuous OK.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from minotaur_subnet.harness.benchmark_worker import (
    BenchmarkWorker,
    ExplicitOrderUnavailable,
)


def _order(order_id: str, app_id: str = "app_dex", chain_id: int = 8453) -> dict:
    return {
        "order_id": order_id,
        "app_id": app_id,
        "chain_id": chain_id,
        "intent_function": "swap",
        "block_number": 28000000,
        "tx_hash": f"0x{order_id[-4:]}",
        "params": {
            "input_token": "0xWETH",
            "output_token": f"0xOUT_{order_id}",
            "input_amount": "1000000000000000000",
        },
    }


def _app_store(app_ids: list[str]):
    store = MagicMock()
    store.list_apps.return_value = [
        SimpleNamespace(app_id=a, manifest={}) for a in app_ids
    ]
    store.get_deployment.return_value = SimpleNamespace(
        contract_address="0xC0FFEE0000000000000000000000000000000001",
    )
    return store


def _worker(app_ids: list[str]) -> BenchmarkWorker:
    w = BenchmarkWorker.__new__(BenchmarkWorker)
    w._app_store = _app_store(app_ids)
    w._use_docker = True
    w._simulator = object()  # a real simulator is wired (the guard's requirement)
    return w


class TestBuildExplicitScenarios:
    def test_builds_tuples_with_hist_labels(self):
        w = _worker(["app_dex"])
        scenarios = w.build_explicit_scenarios([_order("ord_1"), _order("ord_2")])
        assert len(scenarios) == 2
        app_def, state, snapshot = scenarios[0]
        assert app_def.app_id == "app_dex"
        assert state.control_view()["_scenario_name"] == "hist:ord_1"
        assert state.control_view()["_intent_function"] == "swap"
        assert state.chain_id == 8453
        assert state.raw_params_view()["output_token"] == "0xOUT_ord_1"
        assert snapshot is scenarios[1][2], "snapshot cached per chain"

    def test_missing_app_refuses(self):
        w = _worker(["app_dex"])
        with pytest.raises(ExplicitOrderUnavailable) as exc:
            w.build_explicit_scenarios([_order("ord_1"), _order("ord_2", app_id="app_gone")])
        assert exc.value.order_id == "ord_2"
        assert "missing_app:app_gone" in exc.value.reason

    def test_malformed_order_refuses(self):
        w = _worker(["app_dex"])
        broken = _order("ord_1")
        broken["chain_id"] = None
        with pytest.raises(ExplicitOrderUnavailable):
            w.build_explicit_scenarios([broken])

    def test_params_none_refuses_not_typeerror(self):
        # params=None used to escape as a bare TypeError (dict(None)) — the
        # strict path must convert EVERY builder failure to REFUSED semantics.
        w = _worker(["app_dex"])
        broken = _order("ord_1")
        broken["params"] = None
        with pytest.raises(ExplicitOrderUnavailable) as exc:
            w.build_explicit_scenarios([broken])
        assert exc.value.order_id == "ord_1"

    def test_no_app_store_refuses(self):
        w = BenchmarkWorker.__new__(BenchmarkWorker)
        w._app_store = None
        with pytest.raises(ExplicitOrderUnavailable):
            w.build_explicit_scenarios([_order("ord_1")])


class TestCanonicalPathStaysLenient:
    def test_historical_draw_skips_missing_app_silently(self, monkeypatch):
        # Regression parity: the canonical corpus path must keep its
        # fleet-uniform silent skip — strictness is explicit-list-only.
        w = _worker(["app_dex"])
        w._round_store = None
        w._epoch_block_number = None
        orders = [_order("ord_1"), _order("ord_2", app_id="app_gone")]
        monkeypatch.setattr(
            "minotaur_subnet.harness.order_sampler.sample_historical_orders",
            lambda *a, **k: orders,
        )
        scenarios = w._load_historical_scenarios("round-e1-n1")
        assert [s[1].control_view()["_scenario_name"] for s in scenarios] == [
            "hist:ord_1",
        ]


class TestBenchmarkExplicitOrders:
    @pytest.mark.asyncio
    async def test_benches_built_scenarios(self):
        w = _worker(["app_dex"])
        w._build_score_fn = AsyncMock(return_value="score_fn")
        seen = {}

        async def _bench(image_tag, scenarios, score_fn):
            seen["image"] = image_tag
            seen["scenarios"] = scenarios
            seen["score_fn"] = score_fn
            return ["R1", "R2"]

        w._benchmark_submission = AsyncMock(side_effect=_bench)
        out = await w.benchmark_explicit_orders(
            "ghcr.io/x@sha256:" + "a" * 64, [_order("ord_1"), _order("ord_2")],
        )
        assert out == ["R1", "R2"]
        assert seen["image"].endswith("a" * 64)
        assert [s[1].control_view()["_scenario_name"] for s in seen["scenarios"]] \
            == ["hist:ord_1", "hist:ord_2"]
        assert seen["score_fn"] == "score_fn"
        w._build_score_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_list_refuses(self):
        w = _worker(["app_dex"])
        with pytest.raises(ExplicitOrderUnavailable):
            await w.benchmark_explicit_orders("img", [])

    @pytest.mark.asyncio
    async def test_no_simulator_refuses_regardless_of_env(self):
        # Fail-closed even where BENCHMARK_REQUIRE_REAL_SIM (a per-validator
        # env) is off: a mock-sim slice bench fabricates rows -> false veto
        # claims -> discarded veto + an honest-validator strike.
        w = _worker(["app_dex"])
        w._simulator = None
        with pytest.raises(ExplicitOrderUnavailable) as exc:
            await w.benchmark_explicit_orders("img", [_order("ord_1")])
        assert "simulator" in exc.value.reason

    @pytest.mark.asyncio
    async def test_refusal_propagates_before_any_bench(self):
        w = _worker(["app_dex"])
        w._build_score_fn = AsyncMock()
        w._benchmark_submission = AsyncMock()
        with pytest.raises(ExplicitOrderUnavailable):
            await w.benchmark_explicit_orders("img", [_order("o", app_id="nope")])
        w._build_score_fn.assert_not_awaited()
        w._benchmark_submission.assert_not_awaited()
