"""
Tests for the JsExecutionEngine.

Run with:
    python -m minotaur_subnet.engine.test_engine
    # or
    python minotaur_subnet/engine/test_engine.py
"""

import asyncio
import sys
import os

import pytest

pytestmark = pytest.mark.asyncio

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from minotaur_subnet.engine import JsExecutionEngine, IntentNotLoadedError, JsRuntimeError
from minotaur_subnet.engine.context import _state_to_dict
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    IntentState,
    SimulationResult,
    ScoreResult,
)
from minotaur_subnet.v3.contexts import SwapIntentContext


# ─── Test JS Scoring Modules ───────────────────────────────────────────────

SWAP_SCORING_JS = '''
module.exports = {
    config: {
        name: "TestSwap",
        version: "1.0.0",
        type: "swap",
        supportedChains: [1, 8453],
        scoring: { minScore: 0, maxScore: 1, threshold: 0.5 }
    },

    async score(plan, state, context) {
        // Simple: base score 0.5, bonus for simulation success and low gas
        let score = 0.5;
        if (context.simulation.success) score += 0.3;
        if (context.simulation.gas_used < 200000) score += 0.1;
        if (context.simulation.gas_used < 100000) score += 0.1;
        return {
            score: Math.min(1, score),
            breakdown: { base: 0.5, simulation_bonus: score - 0.5 },
            metadata: { interactions_count: plan.interactions.length }
        };
    },

    async validate(plan, state, context) {
        if (plan.interactions.length === 0) {
            return { valid: false, reason: "No interactions in plan" };
        }
        return { valid: true };
    },

    async shouldTrigger(state, context) {
        // Auto-trigger when nonce is even
        return state.nonce % 2 === 0;
    }
};
'''

MINIMAL_SCORING_JS = '''
module.exports = {
    config: { name: "Minimal", version: "0.1.0", type: "test" },

    async score(plan, state, context) {
        return { score: 0.75, breakdown: {}, metadata: {} };
    }
};
'''

FAILING_JS = '''
module.exports = {
    config: { name: "Failing", version: "1.0.0", type: "test" },

    async score(plan, state, context) {
        throw new Error("Intentional scoring failure");
    },

    async validate(plan, state, context) {
        return { valid: true };
    }
};
'''

SYNC_SCORING_JS = '''
module.exports = {
    config: { name: "SyncScorer", version: "1.0.0", type: "test" },

    score(plan, state, context) {
        // Sync function (no async)
        return { score: 0.42, breakdown: { sync: 0.42 }, metadata: {} };
    },

    validate(plan, state, context) {
        return { valid: true };
    }
};
'''


# ─── Test Fixtures ──────────────────────────────────────────────────────────

def make_plan(num_interactions=1):
    """Create a test execution plan."""
    interactions = [
        Interaction(
            target=f"0x{'1234' * 10}",
            value="0",
            call_data="0xabcdef",
            chain_id=1,
        )
        for _ in range(num_interactions)
    ]
    return ExecutionPlan(
        intent_id="test-intent-1",
        interactions=interactions,
        deadline=9999999999,
        nonce=0,
    )


def make_simulation(success=True, gas_used=150000):
    """Create a test simulation result."""
    return SimulationResult(success=success, gas_used=gas_used)


def make_state(nonce=0):
    """Create a test intent state."""
    return IntentState(
        contract_address="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        chain_id=1,
        nonce=nonce,
        owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    )


async def test_state_to_dict_includes_typed_context():
    state = IntentState(
        contract_address="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        chain_id=1,
        nonce=7,
        owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        raw_params={"input_token": "0x1111111111111111111111111111111111111111"},
        typed_context=SwapIntentContext(
            app_id="test-swap-001",
            intent_function="swap",
            chain_id=1,
            owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            contract_address="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            nonce=7,
            raw_params={"input_token": "0x1111111111111111111111111111111111111111"},
            input_token="0x1111111111111111111111111111111111111111",
            output_token="0x2222222222222222222222222222222222222222",
            input_amount=100,
            min_output_amount=90,
            receiver="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
    )

    result = _state_to_dict(state)
    assert result["raw_params"]["input_token"] == "0x1111111111111111111111111111111111111111"
    assert result["control"] == {}
    assert result["typed_context"]["intent_function"] == "swap"
    assert result["typed_context"]["input_amount"] == 100
    assert "input_token" not in result


# ─── Tests ──────────────────────────────────────────────────────────────────

async def test_load_and_score():
    """Test basic load + score flow."""
    print("test_load_and_score... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    assert "test-swap" in engine.list_loaded_intents()

    plan = make_plan()
    sim = make_simulation(success=True, gas_used=150000)
    state = make_state()

    result = await engine.score("test-swap", plan, sim, state)
    assert isinstance(result, ScoreResult)
    assert result.valid is True
    # success=True gives +0.3, gas<200k gives +0.1 -> 0.9
    assert result.score == 0.9, f"Expected 0.9, got {result.score}"
    assert "base" in result.breakdown
    assert result.metadata.get("interactions_count") == 1
    print(f"PASSED (score={result.score})")


async def test_score_with_high_gas():
    """Test scoring with high gas (no gas bonus)."""
    print("test_score_with_high_gas... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    result = await engine.score(
        "test-swap",
        make_plan(),
        make_simulation(success=True, gas_used=500000),
        make_state(),
    )
    # success=True gives +0.3, gas>=200k gives nothing -> 0.8
    assert result.score == 0.8, f"Expected 0.8, got {result.score}"
    print(f"PASSED (score={result.score})")


async def test_score_with_very_low_gas():
    """Test scoring with very low gas (both gas bonuses)."""
    print("test_score_with_very_low_gas... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    result = await engine.score(
        "test-swap",
        make_plan(),
        make_simulation(success=True, gas_used=50000),
        make_state(),
    )
    # success=True gives +0.3, gas<200k gives +0.1, gas<100k gives +0.1 -> 1.0
    assert result.score == 1.0, f"Expected 1.0, got {result.score}"
    print(f"PASSED (score={result.score})")


async def test_score_failed_simulation():
    """Test scoring with failed simulation."""
    print("test_score_failed_simulation... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    result = await engine.score(
        "test-swap",
        make_plan(),
        make_simulation(success=False, gas_used=150000),
        make_state(),
    )
    # success=False gives no bonus, gas<200k gives +0.1 -> 0.6
    assert result.score == 0.6, f"Expected 0.6, got {result.score}"
    print(f"PASSED (score={result.score})")


async def test_validate_valid_plan():
    """Test validation with a valid plan."""
    print("test_validate_valid_plan... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    result = await engine.validate(
        "test-swap",
        make_plan(num_interactions=2),
        make_simulation(),
        make_state(),
    )
    assert result.valid is True
    assert result.score == 1.0
    print("PASSED")


async def test_validate_empty_plan():
    """Test validation with no interactions (should fail)."""
    print("test_validate_empty_plan... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    empty_plan = ExecutionPlan(
        intent_id="test-intent-1",
        interactions=[],
        deadline=9999999999,
        nonce=0,
    )
    result = await engine.validate(
        "test-swap",
        empty_plan,
        make_simulation(),
        make_state(),
    )
    assert result.valid is False
    assert "No interactions" in result.reason
    print(f"PASSED (reason='{result.reason}')")


async def test_validate_missing_function():
    """Test validation when validate() is not exported (should default to valid)."""
    print("test_validate_missing_function... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("minimal", MINIMAL_SCORING_JS)

    result = await engine.validate(
        "minimal",
        make_plan(),
        make_simulation(),
        make_state(),
    )
    assert result.valid is True
    assert result.score == 1.0
    print("PASSED")


async def test_should_trigger():
    """Test shouldTrigger with even and odd nonce."""
    print("test_should_trigger... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    # Even nonce -> should trigger
    assert await engine.should_trigger("test-swap", make_state(nonce=0)) is True
    assert await engine.should_trigger("test-swap", make_state(nonce=2)) is True
    # Odd nonce -> should not trigger
    assert await engine.should_trigger("test-swap", make_state(nonce=1)) is False
    assert await engine.should_trigger("test-swap", make_state(nonce=3)) is False
    print("PASSED")


async def test_should_trigger_missing():
    """Test shouldTrigger when function doesn't exist (should return False)."""
    print("test_should_trigger_missing... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("minimal", MINIMAL_SCORING_JS)

    result = await engine.should_trigger("minimal", make_state())
    assert result is False
    print("PASSED")


async def test_unload_intent():
    """Test loading and unloading intents."""
    print("test_unload_intent... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)
    assert "test-swap" in engine.list_loaded_intents()

    await engine.unload_intent("test-swap")
    assert "test-swap" not in engine.list_loaded_intents()

    # Scoring should now fail
    try:
        await engine.score("test-swap", make_plan(), make_simulation(), make_state())
        assert False, "Should have raised IntentNotLoadedError"
    except IntentNotLoadedError:
        pass
    print("PASSED")


async def test_intent_not_loaded():
    """Test scoring an unknown intent raises IntentNotLoadedError."""
    print("test_intent_not_loaded... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)

    try:
        await engine.score("nonexistent", make_plan(), make_simulation(), make_state())
        assert False, "Should have raised IntentNotLoadedError"
    except IntentNotLoadedError:
        pass
    print("PASSED")


async def test_js_runtime_error():
    """Test that a JS error results in score=0, valid=False."""
    print("test_js_runtime_error... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("failing", FAILING_JS)

    result = await engine.score(
        "failing",
        make_plan(),
        make_simulation(),
        make_state(),
    )
    assert result.valid is False
    assert result.score == 0.0
    assert "error" in result.reason.lower() or "scoring" in result.reason.lower()
    print(f"PASSED (reason='{result.reason}')")


async def test_sync_scoring_function():
    """Test that sync (non-async) JS functions also work."""
    print("test_sync_scoring_function... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("sync", SYNC_SCORING_JS)

    result = await engine.score(
        "sync",
        make_plan(),
        make_simulation(),
        make_state(),
    )
    assert result.valid is True
    assert result.score == 0.42, f"Expected 0.42, got {result.score}"
    print(f"PASSED (score={result.score})")


async def test_multiple_intents():
    """Test loading and scoring multiple intents."""
    print("test_multiple_intents... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("swap", SWAP_SCORING_JS)
    await engine.load_intent("minimal", MINIMAL_SCORING_JS)
    await engine.load_intent("sync", SYNC_SCORING_JS)

    assert len(engine.list_loaded_intents()) == 3

    plan = make_plan()
    sim = make_simulation(success=True, gas_used=150000)
    state = make_state()

    r1 = await engine.score("swap", plan, sim, state)
    r2 = await engine.score("minimal", plan, sim, state)
    r3 = await engine.score("sync", plan, sim, state)

    assert r1.score == 0.9
    assert r2.score == 0.75
    assert r3.score == 0.42
    print(f"PASSED (scores={r1.score}, {r2.score}, {r3.score})")


async def test_intent_config_extraction():
    """Test that intent config is extracted on load."""
    print("test_intent_config_extraction... ", end="", flush=True)
    engine = JsExecutionEngine(timeout_ms=10000)
    await engine.load_intent("test-swap", SWAP_SCORING_JS)

    config = engine.get_intent_config("test-swap")
    assert config is not None
    assert config.get("name") == "TestSwap"
    assert config.get("version") == "1.0.0"
    assert config.get("type") == "swap"
    print(f"PASSED (config={config})")


# ─── Runner ─────────────────────────────────────────────────────────────────

async def run_all_tests():
    """Run all tests and report results."""
    tests = [
        test_load_and_score,
        test_score_with_high_gas,
        test_score_with_very_low_gas,
        test_score_failed_simulation,
        test_validate_valid_plan,
        test_validate_empty_plan,
        test_validate_missing_function,
        test_should_trigger,
        test_should_trigger_missing,
        test_unload_intent,
        test_intent_not_loaded,
        test_js_runtime_error,
        test_sync_scoring_function,
        test_multiple_intents,
        test_intent_config_extraction,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\nRunning {len(tests)} tests...\n")

    for test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            name = test_fn.__name__
            errors.append((name, exc))
            print(f"FAILED: {exc}")

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    if errors:
        print(f"\nFailed tests:")
        for name, exc in errors:
            print(f"  - {name}: {exc}")
    print(f"{'='*60}\n")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
