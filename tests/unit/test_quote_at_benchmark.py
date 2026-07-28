"""Quote-at-benchmark: populate synthetic scenarios' on-chain quote params.

Synthetic benchmark scenarios never run a quote, so their ABI-encoded
intentParams omit the CoW ``quoted_output`` field and the deployed 12-field
DexAggregator ``scoreIntent`` reverts on decode. The benchmark injects a STATIC
ZERO quote (#543) via the ONE shared mapping helper
(``map_quote_result_to_params``): scoreIntent gates its CoW fee on
quotedOutput>0, so 0 = no anchor, no fee, full output executes — the
authoritative relative scorer reads the RAW delivered output, so the quote is
not in the score. ``solver.quote()`` is never called during benchmarking.

These tests cover:
1. ``map_quote_result_to_params``: a fake QuoteResult + manifest → mapped params.
2. ``run_benchmark`` enrichment: static zero injection + the already-quoted skip.
"""
import asyncio

from minotaur_subnet.api.services.app_service import map_quote_result_to_params
from minotaur_subnet.harness.orchestrator import (
    BenchmarkConfig,
    run_benchmark,
)
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    QuoteResult,
    ScoreResult,
    SimulationResult,
    TriggerType,
)


# ── Manifest fixture: a swap intent whose params bind to the quote ────────────
def _swap_manifest() -> dict:
    return {
        "intent_functions": [
            {
                "name": "swap",
                "params": {
                    "input_token": {"type": "address"},
                    "input_amount": {"type": "uint256"},
                    "min_output_amount": {
                        "type": "uint256",
                        "source": "quote",
                        "quote_field": "suggested_min_output",
                    },
                    "quoted_output": {
                        "type": "uint256",
                        "source": "quote",
                        "quote_field": "estimated_output",
                    },
                    "platform_fee_wei": {
                        "type": "uint256",
                        "source": "quote",
                        "quote_field": "platform_fee_wei",
                    },
                },
            }
        ]
    }


# ════════════════════════════════════════════════════════════════════════════
# 1. map_quote_result_to_params
# ════════════════════════════════════════════════════════════════════════════
def test_map_quote_result_to_params_maps_quote_fields():
    qr = QuoteResult(
        estimated_output="1000000",
        platform_fee_wei="4200",
        gas_estimate=210000,
    )
    out = map_quote_result_to_params(qr, _swap_manifest(), "swap", slippage_bps=50)

    # quoted_output ← estimated_output
    assert out["quoted_output"] == "1000000"
    # platform_fee_wei ← platform_fee_wei
    assert out["platform_fee_wei"] == "4200"
    # suggested_min_output = estimated * (10000 - 50) / 10000 = 1000000*9950//10000
    assert out["min_output_amount"] == str(1000000 * 9950 // 10000)
    # Only source:"quote" params are returned (no input_token/input_amount).
    assert set(out) == {"quoted_output", "platform_fee_wei", "min_output_amount"}


def test_map_quote_result_to_params_min_output_zero_when_estimate_zero():
    qr = QuoteResult(estimated_output="0", platform_fee_wei="0")
    out = map_quote_result_to_params(qr, _swap_manifest(), "swap")
    assert out["min_output_amount"] == "0"
    assert out["quoted_output"] == "0"


def test_map_quote_result_to_params_empty_without_manifest_or_quote():
    qr = QuoteResult(estimated_output="100")
    assert map_quote_result_to_params(qr, None, "swap") == {}
    assert map_quote_result_to_params(None, _swap_manifest(), "swap") == {}
    # No matching intent_function → empty.
    assert map_quote_result_to_params(qr, _swap_manifest(), "nope") == {}


# ════════════════════════════════════════════════════════════════════════════
# 2. run_benchmark enrichment (reference + self-quote fallback)
# ════════════════════════════════════════════════════════════════════════════
def _swap_intent() -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id="dex",
        name="Dex",
        version="1.0.0",
        # Empty on purpose — mirrors the LIVE DexAggregator app (intent_type='').
        # Enrichment must NOT depend on this field; it's manifest-driven.
        intent_type="",
        js_code="//hidden",
        manifest=_swap_manifest(),
        config=AppIntentConfig(
            supported_chains=[1],
            trigger_type=TriggerType.USER_TRIGGERED,
        ),
    )


def _swap_state() -> IntentState:
    # Synthetic scenario: NO quoted_output — this is what triggers the revert.
    return IntentState(
        contract_address="0xAc1C00000000000000000000000000000000cB07",
        chain_id=1,
        nonce=0,
        owner="0x1111111111111111111111111111111111111111",
        raw_params={
            "input_token": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "output_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "input_amount": "1000000",
            "min_output_amount": "1",
        },
        control={"_scenario_name": "small_swap", "_intent_function": "swap"},
    )


def _make_snapshot():
    from minotaur_subnet.sdk.intent_solver import MarketSnapshot

    return MarketSnapshot(
        chain_id=1, block_number=1, timestamp=1, prices={}, pool_states={}, dex_config={}
    )


class _FakeSession:
    """Records the state it was handed at score time + serves a self-quote."""

    def __init__(self, self_quote=None):
        self._self_quote = self_quote
        self.scored_states = []
        self.quote_calls = 0

    async def initialize(self, config):
        return None

    async def metadata(self):
        return {}

    async def on_benchmark_start(self, n):
        return None

    async def quote(self, intent, state, snapshot):
        self.quote_calls += 1
        return self._self_quote

    async def generate_plan(self, intent, state, snapshot):
        return ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)

    async def on_benchmark_end(self, summary):
        return None


def _run(session):
    intent, state, snapshot = _swap_intent(), _swap_state(), _make_snapshot()
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)

    captured = {}

    class _Sim:
        async def simulate(self, plan, **kwargs):
            # Capture the intent_order the benchmark built (proves enrichment).
            captured["intent_order"] = kwargs.get("intent_order")
            return SimulationResult(success=True, gas_used=100_000)

    async def score_fn(app_id, p, simulation, st):
        # st is the (possibly enriched) state handed to the scorer.
        session.scored_states.append(dict(st.raw_params_view()))
        return ScoreResult(score=0.5)

    async def _go():
        return await run_benchmark(
            session,
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[1]),
            score_fn=score_fn,
            simulator=_Sim(),
        )

    results = asyncio.run(_go())
    return results, session, captured


def test_run_benchmark_injects_static_zero_quote():
    # The scoring definition: a scenario without quoted_output gets the static
    # ZERO quote — solver.quote() is NEVER called, quoted_output=0 (no CoW fee,
    # full output executes) and min_output_amount=0 (no stale static floor).
    session = _FakeSession(self_quote=QuoteResult(estimated_output="2000000"))
    results, sess, captured = _run(session)

    assert len(results) == 1
    assert sess.quote_calls == 0, "benchmark must NOT call solver.quote()"
    scored = sess.scored_states[0]
    assert scored["quoted_output"] == "0"
    assert scored["min_output_amount"] == "0"
    # Order still built → 12-field ABI stays valid (0 present, not omitted).
    assert captured["intent_order"] is not None


def test_run_benchmark_skips_quote_when_already_quoted():
    # A real/historical order already carries quoted_output → leave it alone.
    intent, state, snapshot = _swap_intent(), _swap_state(), _make_snapshot()
    state.raw_params["quoted_output"] = "12345"
    state.sync_extra()
    session = _FakeSession(self_quote=QuoteResult(estimated_output="9"))
    plan = ExecutionPlan(intent_id=intent.app_id, interactions=[], deadline=0, nonce=0)

    async def score_fn(app_id, p, simulation, st):
        session.scored_states.append(dict(st.raw_params_view()))
        return ScoreResult(score=0.5)

    async def _go():
        return await run_benchmark(
            session,
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[1]),
            score_fn=score_fn,
        )

    asyncio.run(_go())
    assert session.quote_calls == 0, "already quoted → must NOT re-quote"
    assert session.scored_states[0]["quoted_output"] == "12345"


def test_run_benchmark_fails_loud_without_rpc_when_real_sim_required(monkeypatch):
    # Fail loud, not silent: require_real_sim + no live RPC for the benchmark
    # chain → REFUSE. Without RPC the solver falls back to an incomplete on-chain
    # snapshot (missing pools → false "No route" → corrupt scores); we must not
    # silently degrade benchmarking.
    import pytest
    from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable

    for v in ("ANVIL_RPC_URL", "BENCHMARK_ANVIL_RPC_ETH"):
        monkeypatch.delenv(v, raising=False)
    intent, state, snapshot = _swap_intent(), _swap_state(), _make_snapshot()
    session = _FakeSession(self_quote=None)

    class _Sim:
        async def simulate(self, plan, **kwargs):
            return SimulationResult(success=True, gas_used=1)

    async def score_fn(app_id, p, simulation, st):
        return ScoreResult(score=0.5)

    async def _go():
        return await run_benchmark(
            session,
            [(intent, state, snapshot)],
            config=BenchmarkConfig(chain_ids=[1]),
            score_fn=score_fn,
            simulator=_Sim(),
            require_real_sim=True,
        )

    with pytest.raises(RealSimulationUnavailable):
        asyncio.run(_go())
