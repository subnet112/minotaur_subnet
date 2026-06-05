"""EpochManager._app_store wiring + the masking Stage-3 key bug.

Two bugs masked each other: _app_store was referenced in the Stage-3 regression
gate but never assigned (AttributeError if reached), and the gate read the wrong
benchmark-details key ("results" instead of "per_intent"), so it never built
candidates and never reached the _app_store access. Fixed: _app_store is a real
ctor field (defaults None -> gate degrades to "pass"), and the gate reads
"per_intent".
"""
import asyncio
import types

from minotaur_subnet.epoch.manager import EpochManager

_INCUMBENT = types.SimpleNamespace(image_tag="champ:img")
_CHALLENGER = types.SimpleNamespace(
    submission_id="chal", image_tag="chal:img",
    benchmark_details={"per_intent": [
        {"intent_id": "app_x:hist:ord_1", "score": 0, "error": "revert"},  # failed historical
        {"intent_id": "app_x:WETH_to_USDC", "score": 0.8, "error": None},   # synthetic, ignored
    ]})


class _AppStore:
    def get_order(self, oid):
        if oid == "ord_1":
            return {"block_number": 100, "chain_id": 8453, "params": {}, "app_id": "app_x"}
        return None


def _gate_mgr(app_store):
    mgr = EpochManager.__new__(EpochManager)
    mgr._app_store = app_store
    mgr._benchmark_worker = object()          # truthy: can replay
    mgr._sub_store = types.SimpleNamespace(get=lambda _sid: _INCUMBENT)
    mgr._champion = types.SimpleNamespace(submission_id="champ")
    return mgr


def test_app_store_is_a_real_ctor_field():
    assert EpochManager(owner_hotkey="x")._app_store is None
    sentinel = object()
    assert EpochManager(owner_hotkey="x", app_store=sentinel)._app_store is sentinel


def test_stage3_is_opt_in_disabled_by_default(monkeypatch):
    # Default (env unset) -> gate disabled -> passes without doing any work.
    monkeypatch.delenv("STAGE3_DISABLED", raising=False)
    mgr = _gate_mgr(_AppStore())
    assert asyncio.run(mgr._passes_regression_gate(_CHALLENGER, "round_1")) is True


def test_stage3_finds_candidates_via_per_intent_when_enabled(monkeypatch):
    # Enabled (STAGE3_DISABLED=0): the failed :hist: order is found in per_intent and
    # looked up via _app_store -> a candidate exists. No archive RPC -> fail-closed.
    # If the gate still read "results", candidates would be empty -> return True.
    monkeypatch.setenv("STAGE3_DISABLED", "0")
    monkeypatch.setattr(
        "minotaur_subnet.harness.historical_fork.archive_rpc_available", lambda _cid: False)
    mgr = _gate_mgr(_AppStore())
    assert asyncio.run(mgr._passes_regression_gate(_CHALLENGER, "round_1")) is False


def test_stage3_no_app_store_passes_gracefully_when_enabled(monkeypatch):
    # Enabled but _app_store is None -> the candidate loop skips (no AttributeError)
    # -> no candidates -> gate returns True. This is the bug that used to AttributeError.
    monkeypatch.setenv("STAGE3_DISABLED", "0")
    mgr = _gate_mgr(None)
    assert asyncio.run(mgr._passes_regression_gate(_CHALLENGER, "round_1")) is True


def test_stage3_explicit_disable_passes(monkeypatch):
    monkeypatch.setenv("STAGE3_DISABLED", "1")
    mgr = _gate_mgr(_AppStore())
    assert asyncio.run(mgr._passes_regression_gate(_CHALLENGER, "round_1")) is True
