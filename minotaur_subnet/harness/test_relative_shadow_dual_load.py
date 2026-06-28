"""Dual-load of the shadow scorer in BenchmarkWorker._build_score_fn, and the
raw-output behaviour of the committed dex_aggregator_raw.js shadow scorer.

The dual-load tests use a fake engine (no Node needed). The raw-JS test loads the
real shadow scorer into a JsExecutionEngine and is skipped when Node is absent.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
    ScoreResult,
    SimulationResult,
    TokenTransfer,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.submission_store import SubmissionStore

_LIVE_JS = "function score(p,s,c){ return { score: 0.5 }; } // live padding xxxxxxxx"
_SHADOW_JS = "function score(p,s,c){ return { score: 1 }; } // shadow padding xxxxxxxx"


class _FakeEngine:
    """Records load_intent calls; returns a raw_output metadata for the shadow key."""

    def __init__(self):
        self.loaded: dict[str, str] = {}

    async def load_intent(self, app_id: str, js_code: str) -> None:
        self.loaded[app_id] = js_code

    def list_loaded_intents(self):
        return list(self.loaded.keys())

    def get_manifest(self, app_id: str):
        return {}

    async def score(self, app_id, plan, simulation, state) -> ScoreResult:
        if app_id.endswith(":shadow"):
            # Engine clamps `score` to [0,1]; the unclamped raw output lives in
            # metadata.raw_output — exactly what the dual-load path reads.
            return ScoreResult(score=1.0, valid=True, metadata={"raw_output": 2500.0})
        return ScoreResult(score=0.5, valid=True)


def _intent(app_id="app", *, shadow: str | None):
    return AppIntentDefinition(
        app_id=app_id, name="A", version="1.0.0", intent_type="swap",
        js_code=_LIVE_JS, shadow_js_code=shadow,
        config=AppIntentConfig(supported_chains=[1], trigger_type=TriggerType.USER_TRIGGERED),
    )


def _state():
    return IntentState(contract_address="0x" + "11" * 20, chain_id=1, nonce=0, owner="")


def _snap():
    return MarketSnapshot(chain_id=1, block_number=1, timestamp=int(time.time()), prices={}, dex_config={})


def _plan():
    return ExecutionPlan(
        intent_id="app",
        interactions=[Interaction(target="0x" + "aa" * 20, value="0", call_data="0x00", chain_id=1)],
        deadline=int(time.time()) + 300, nonce=0,
    )


async def test_dual_load_attaches_shadow_score_when_on(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_SHADOW", raising=False)  # default ON
    eng = _FakeEngine()
    worker = BenchmarkWorker(SubmissionStore(), js_engine=eng)
    score_fn = await worker._build_score_fn([(_intent(shadow=_SHADOW_JS), _state(), _snap())])

    # Both the live and the composite ":shadow" keys are loaded.
    assert "app" in eng.loaded
    assert "app:shadow" in eng.loaded

    res = await score_fn("app", _plan(), SimulationResult(success=True), _state())
    assert res.score == 0.5  # live score untouched
    assert getattr(res, "shadow_score", None) == 2500.0  # raw output attached


async def test_dual_load_noop_when_shadow_disabled(monkeypatch):
    monkeypatch.setenv("RELATIVE_SCORING_SHADOW", "0")  # emergency override OFF
    eng = _FakeEngine()
    worker = BenchmarkWorker(SubmissionStore(), js_engine=eng)
    score_fn = await worker._build_score_fn([(_intent(shadow=_SHADOW_JS), _state(), _snap())])

    assert "app" in eng.loaded
    assert "app:shadow" not in eng.loaded  # shadow never loaded when gate off

    res = await score_fn("app", _plan(), SimulationResult(success=True), _state())
    assert getattr(res, "shadow_score", None) is None  # additive: stays None


async def test_dual_load_noop_when_no_shadow_js(monkeypatch):
    monkeypatch.delenv("RELATIVE_SCORING_SHADOW", raising=False)  # default ON
    eng = _FakeEngine()
    worker = BenchmarkWorker(SubmissionStore(), js_engine=eng)
    score_fn = await worker._build_score_fn([(_intent(shadow=None), _state(), _snap())])

    assert "app:shadow" not in eng.loaded  # nothing to load
    res = await score_fn("app", _plan(), SimulationResult(success=True), _state())
    assert getattr(res, "shadow_score", None) is None


# ── the real raw-output shadow scorer (needs Node) ───────────────────────────

_RAW_JS_PATH = _REPO_ROOT / "minotaur_subnet" / "harness" / "scoring_shadow" / "dex_aggregator_raw.js"
_RECEIVER = "0x0000000000000000000000000000000000000001"
_TOKEN_OUT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # Base USDC


def _swap_state(min_out="0"):
    return IntentState(
        contract_address="0x" + "aa" * 20, chain_id=8453, nonce=0, owner="0xBBB",
        raw_params={
            "input_token": "0x4200000000000000000000000000000000000006",
            "output_token": _TOKEN_OUT,
            "input_amount": "1000000000000000000",
            "min_output_amount": min_out,
            "receiver": _RECEIVER,
        },
    )


def _swap_sim(output_amount):
    return SimulationResult(
        success=True, gas_used=120000,
        token_transfers=[
            TokenTransfer(token=_TOKEN_OUT, from_addr="0xpool", to_addr=_RECEIVER, amount=str(output_amount)),
        ],
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available")
async def test_raw_shadow_js_returns_raw_output():
    from minotaur_subnet.engine import JsExecutionEngine
    from minotaur_subnet.engine.context import JsContext
    from minotaur_subnet.engine.js_engine import _plan_to_dict
    from minotaur_subnet.engine.sandbox import JsSandbox

    js = _RAW_JS_PATH.read_text()
    engine = JsExecutionEngine(timeout_ms=8000)
    await engine.load_intent("dex:shadow", js)

    delivered = 2_500_000_000  # raw USDC units (>> 1, so engine clamps `score`)
    state, sim = _swap_state(), _swap_sim(delivered)

    # Through the engine: the clamped `score` is unusable, but metadata.raw_output
    # carries the exact raw delivered output — the value the dual-load path reads.
    res = await engine.score("dex:shadow", _plan(), sim, state)
    assert res.metadata.get("raw_output") == float(delivered)

    # Through the sandbox directly: the JS contract returns score == delivered and
    # valid True for a good order...
    sandbox = JsSandbox(timeout_ms=8000)
    ctx = JsContext(chain_id=8453, contract_address="0x" + "aa" * 20).build_context(sim, state)
    raw = await sandbox.execute_async(js, "score", [_plan_to_dict(_plan()), ctx["state"], ctx])
    assert raw["score"] == float(delivered)
    assert raw["valid"] is True

    # ...and score 0 / valid False when the output is below the min.
    state2, sim2 = _swap_state(min_out="100"), _swap_sim(50)
    ctx2 = JsContext(chain_id=8453, contract_address="0x" + "aa" * 20).build_context(sim2, state2)
    raw2 = await sandbox.execute_async(js, "score", [_plan_to_dict(_plan()), ctx2["state"], ctx2])
    assert raw2["score"] == 0
    assert raw2["valid"] is False
