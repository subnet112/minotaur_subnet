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


def test_sample_records_filters_unfilled_and_unreplayable():
    recs = [
        {"order_id": "ok", "app_id": "a", "chain_id": 1, "status": "filled", "block_number": 5,
         "params": {}},
        {"order_id": "pending", "app_id": "a", "chain_id": 1, "status": "open", "block_number": 6,
         "params": {}},
        {"order_id": "noblock", "app_id": "a", "chain_id": 1, "status": "filled", "block_number": None,
         "params": {}},
    ]
    sampled = sample_historical_orders(app_store=None, round_id="r", n_per_chain=10, records=recs)
    assert [o["order_id"] for o in sampled] == ["ok"]


def test_chain_corpus_gate_default_off(monkeypatch):
    monkeypatch.delenv("BENCHMARK_CHAIN_CORPUS", raising=False)
    assert chain_corpus_enabled() is False
    monkeypatch.setenv("BENCHMARK_CHAIN_CORPUS", "1")
    assert chain_corpus_enabled() is True
