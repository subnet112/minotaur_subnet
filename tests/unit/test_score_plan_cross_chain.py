"""Cross-chain dry-run parity on POST /v1/apps/{id}/score.

A cross-chain-declaring plan gets the EXACT benchmark destination
measurement, attached to the simulation BEFORE the JS scorer runs (the same
ordering the bench uses), and surfaced as ``response.cross_chain``.
Single-chain responses are byte-identical to before (no new key, no
measurement call).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.api.routes import apps as apps_module
from minotaur_subnet.harness.orchestrator import _ANVIL_DEFAULT_ACCOUNT
from minotaur_subnet.shared.types import Interaction, SimulationResult

APP = "app_test"
WETH_BASE = "0x4200000000000000000000000000000000000006"
WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
DELIVERED = 99_400_000_000_000_000


def _ix(chain_id: int) -> dict:
    return asdict(Interaction(
        target="0x" + "11" * 20, value="0",
        call_data="0xa9059cbb" + "00" * 28, chain_id=chain_id,
    ))


def _xc_plan() -> dict:
    return {
        "intent_id": APP,
        "interactions": [],
        "metadata": {"cross_chain_plan": {
            "legs": [
                {"chain_id": 8453, "interactions": []},
                {"chain_id": 1, "interactions": [_ix(1)]},
            ],
            "bridge_requests": [{
                "token": WETH_BASE, "amount": 10**17,
                "src_chain_id": 8453, "dst_chain_id": 1,
                "recipient": _ANVIL_DEFAULT_ACCOUNT,
            }],
        }},
    }


class _FakeSim:
    """Plain simulator fake: single-chain simulate + the cross-chain walk."""

    def __init__(self):
        self.cross_chain_calls = 0

    async def simulate(self, plan, **kw):
        return SimulationResult(success=True, gas_used=100)

    async def simulate_cross_chain(self, plan, **kw):
        self.cross_chain_calls += 1
        dest = [l for l in plan.metadata["legs"] if l["type"] == "destination"]
        return SimulationResult(
            success=True,
            leg_results={dest[0]["leg_id"]: {
                "success": True,
                "token_transfers": [{
                    "token": WETH_ETH, "from": "0x" + "12" * 20,
                    "to": _ANVIL_DEFAULT_ACCOUNT, "amount": str(DELIVERED),
                }],
            }},
            bridge_estimate={"amount_source": "simulated"},
        )


class _FakeEngine:
    def __init__(self):
        self._intents = {APP: object()}
        self.seen_sim = None

    async def score(self, app_id, plan, simulation, state):
        self.seen_sim = simulation
        return SimpleNamespace(score=1.0, valid=True, reason="ok", breakdown={})


def _fake_store():
    return SimpleNamespace(
        get_app=lambda app_id: SimpleNamespace(js_code="function score(){}"),
        get_deployment=lambda app_id, chain_id=None: None,
    )


def _request():
    return SimpleNamespace(
        url=SimpleNamespace(path="/v1/apps/t/score"),
        headers={},
        client=SimpleNamespace(host="10.0.0.1"),
    )


def _call(monkeypatch, plan_dict, simulator):
    engine = _FakeEngine()
    monkeypatch.setattr(apps_module, "_store", _fake_store)
    monkeypatch.setattr(apps_module, "_simulator", simulator)
    monkeypatch.setattr(apps_module, "_js_engine", engine)
    body = apps_module.ScorePlanRequest(
        plan=plan_dict,
        params={"input_token": WETH_BASE, "input_amount": str(10**17),
                "output_token": WETH_ETH, "dest_chain_id": "1"},
        chain_id=8453,
        intent_function="swap",
    )
    resp = asyncio.run(apps_module.score_plan(APP, body, _request()))
    return resp, engine


def test_cross_chain_plan_gets_the_bench_measurement(monkeypatch):
    sim = _FakeSim()
    resp, engine = _call(monkeypatch, _xc_plan(), sim)
    assert resp["simulation_mode"] == "anvil"
    xc = resp["cross_chain"]
    assert xc["destination_delivered"] == str(DELIVERED)
    assert xc["destination_amount_source"] == "simulated"
    # The scorer saw the measurement (bench ordering), so raw_output parity
    # with a real round holds.
    assert engine.seen_sim.destination_delivered == str(DELIVERED)
    assert engine.seen_sim.destination_amount_source == "simulated"
    assert sim.cross_chain_calls == 1


def test_single_chain_response_is_unchanged(monkeypatch):
    sim = _FakeSim()
    plan = {"intent_id": APP, "interactions": [_ix(8453)], "metadata": {}}
    resp, engine = _call(monkeypatch, plan, sim)
    assert "cross_chain" not in resp
    assert sim.cross_chain_calls == 0
    assert engine.seen_sim.destination_delivered is None


def test_mock_path_never_measures(monkeypatch):
    resp, _engine = _call(monkeypatch, _xc_plan(), None)
    assert resp["simulation_mode"] == "mock"
    # Nothing real to observe — no cross_chain block rather than a fake one.
    assert "cross_chain" not in resp
