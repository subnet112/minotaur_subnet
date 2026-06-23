"""Port B (5b) — chain-derived Stage-2 corpus (chain_corpus + order_sampler seam).

The load-bearing test is the decode round-trip: decode_intent_params_hex MUST be the
exact inverse of build_intent_params_hex_from_manifest, or every Stage-2 scenario
silently corrupts.
"""
import types

from web3 import Web3

from minotaur_subnet.api.services.app_service import build_intent_params_hex_from_manifest
from minotaur_subnet.harness.chain_corpus import (
    chain_corpus_enabled,
    decode_intent_params_hex,
    _corpus_to_block,
    _function_for_selector,
)
from minotaur_subnet.harness.order_sampler import sample_historical_orders
from minotaur_subnet.v3.manifest import (
    IntentFieldSpec,
    IntentFunctionSpec,
    IntentManifest,
    compute_selector_from_manifest,
)


def _manifest():
    return IntentManifest(app_name="dex", intent_functions=[
        IntentFunctionSpec(name="swap", params=[
            IntentFieldSpec(name="input_token", value_type="address"),
            IntentFieldSpec(name="output_token", value_type="address"),
            IntentFieldSpec(name="input_amount", value_type="uint256"),
            IntentFieldSpec(name="min_output_amount", value_type="uint256"),
            IntentFieldSpec(name="receiver", value_type="address"),
            IntentFieldSpec(name="quoted_output", value_type="uint256"),
            IntentFieldSpec(name="unwrap_output", value_type="bool"),
        ])])


class _App:
    def __init__(self, m):
        self.manifest = m


class _Store:
    def __init__(self, m):
        self._m = m

    def get_app(self, _app_id):
        return _App(self._m)


def test_decode_is_exact_inverse_of_encode():
    m = _manifest()
    params = {
        "input_token": Web3.to_checksum_address("0x" + "a0" * 20),
        "output_token": Web3.to_checksum_address("0x" + "42" * 20),
        "input_amount": "1000000",
        "min_output_amount": "990000000000000",
        "receiver": Web3.to_checksum_address("0x" + "11" * 20),
        "quoted_output": "995000000000000",
        "unwrap_output": False,
    }
    hexs = build_intent_params_hex_from_manifest(_Store(m), None, "dex", "swap", params, params["receiver"])
    assert hexs is not None
    assert decode_intent_params_hex(m, "swap", hexs) == params  # exact round-trip


def test_function_for_selector_maps_back():
    m = _manifest()
    sel = compute_selector_from_manifest(m, "swap")
    assert _function_for_selector(m, sel) == "swap"
    assert _function_for_selector(m, "0xdeadbeef") is None


def test_sample_records_path_is_deterministic_and_store_free():
    recs = [{"order_id": f"o{i:02d}", "app_id": "app_x", "chain_id": 8453,
             "status": "filled", "block_number": 100 + i,
             "params": {"input_token": "0x1"}} for i in range(20)]
    # app_store=None proves the records path never calls list_orders()
    s1 = sample_historical_orders(app_store=None, round_id="r1", n_per_chain=5, records=recs)
    s2 = sample_historical_orders(app_store=None, round_id="r1", n_per_chain=5, records=recs)
    assert [o["order_id"] for o in s1] == [o["order_id"] for o in s2]  # deterministic per round
    assert len(s1) == 5


def test_sample_records_filters_inflight_keeps_unfilled_demand():
    # #228: terminal demand (filled/rejected/expired) is kept regardless of
    # block_number (benchmark forks at the round/live-head pin, not the order's
    # block); only in-flight (open/assigned) demand is filtered.
    recs = [
        {"order_id": "ok", "app_id": "a", "chain_id": 1, "status": "filled", "block_number": 5,
         "params": {}},
        {"order_id": "pending", "app_id": "a", "chain_id": 1, "status": "open", "block_number": 6,
         "params": {}},
        {"order_id": "noblock", "app_id": "a", "chain_id": 1, "status": "rejected", "block_number": None,
         "params": {}},
    ]
    sampled = sample_historical_orders(app_store=None, round_id="r", n_per_chain=10, records=recs)
    assert {o["order_id"] for o in sampled} == {"ok", "noblock"}  # in-flight 'pending' dropped


def test_chain_corpus_gate_hardcoded_off(monkeypatch):
    # The chain-derived Stage-2 corpus source is HARDCODED OFF fleet-wide — it is
    # consensus-relevant and not yet cross-machine deterministic, so it is no longer
    # a per-validator env (BENCHMARK_CHAIN_CORPUS) a 3rd party could flip to split
    # the fleet. Even with the legacy env set, the gate stays off.
    monkeypatch.delenv("BENCHMARK_CHAIN_CORPUS", raising=False)
    assert chain_corpus_enabled() is False
    monkeypatch.setenv("BENCHMARK_CHAIN_CORPUS", "1")
    assert chain_corpus_enabled() is False


# ── to_block pin (cross-validator corpus parity) ──────────────────────────────
# The live cutoff is head-derived per node, so two validators scanning minutes
# apart build different corpora despite the shared round_id seed. The pin makes
# the scan range an explicit shared input.

_W3 = types.SimpleNamespace(eth=types.SimpleNamespace(block_number=1000))


def test_corpus_to_block_defaults_to_live_head(monkeypatch):
    monkeypatch.delenv("BENCHMARK_CORPUS_TO_BLOCK", raising=False)
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    assert _corpus_to_block(_W3, 1) == 999  # head - confirmations, unchanged


def test_corpus_to_block_falls_back_to_epoch_pin(monkeypatch):
    # One knob pins both the fork AND the corpus: the round's fork-pin is the
    # default cutoff when no explicit corpus pin is set. Legacy dev/test path —
    # the BENCHMARK_* knobs are only honored with the round-anchored gate OFF.
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # default is on; demote env knobs
    monkeypatch.delenv("BENCHMARK_CORPUS_TO_BLOCK", raising=False)
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "46904887")
    assert _corpus_to_block(_W3, 1) == 46904887


def test_corpus_to_block_explicit_pin_wins(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # default is on; demote env knobs
    monkeypatch.setenv("BENCHMARK_CORPUS_TO_BLOCK", "46900000")
    monkeypatch.setenv("BENCHMARK_EPOCH_BLOCK", "46904887")
    assert _corpus_to_block(_W3, 1) == 46900000


def test_corpus_to_block_invalid_pin_ignored(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # default is on; demote env knobs
    monkeypatch.setenv("BENCHMARK_CORPUS_TO_BLOCK", "not-an-int")
    monkeypatch.delenv("BENCHMARK_EPOCH_BLOCK", raising=False)
    assert _corpus_to_block(_W3, 1) == 999  # falls through to live head
