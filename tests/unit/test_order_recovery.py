"""Phase 5b prototype — chain-derived order corpus (order_recovery).

Tests the two load-bearing properties WITHOUT a live chain: (1) executeIntent
calldata round-trips back to the IntentOrder for ANY app (generic decode), and
(2) the canonical record + corpus hash are byte-deterministic and discovery-order
independent — so every honest validator converges on the same Stage-2 corpus hash.
"""
from web3 import Web3

from minotaur_subnet.harness.scoring_lab.order_recovery import (
    APPINTENT_ABI,
    canonical_order,
    corpus_hash,
)


def _mk_order(nonce=7, params=b"\xde\xad\xbe\xef"):
    # positional tuple matching IntentOrder components
    return (
        b"\x11" * 32,
        Web3.to_checksum_address("0x" + "22" * 20),
        b"\x51\xe0\x2c\x64",                       # intentSelector (scoreIntent-ish)
        params,
        Web3.to_checksum_address("0x" + "33" * 20),
        8453, 1999999999, nonce, False, 1, 0,
    )


def _roundtrip(order_tuple):
    c = Web3().eth.contract(abi=APPINTENT_ABI)
    plan = ([], 1999999999, 7, b"")
    data = c.encode_abi("executeIntent", [order_tuple, plan, b"\x01" * 65, [b"\x02" * 65]])
    _func, params = c.decode_function_input(data)
    return params["order"]


def test_executeintent_roundtrip_recovers_order_generically():
    order = _roundtrip(_mk_order())
    rec = canonical_order(order, score=5027, block_number=46904887,
                          tx_hash=b"\xab" * 32, chain_id=8453)
    assert rec["intent_selector"] == "0x51e02c64"
    assert rec["intent_params_hex"] == "0xdeadbeef"
    assert rec["user_nonce"] == "7"
    assert rec["on_chain_score"] == 5027
    assert rec["status"] == "filled"
    # PII stripped: the user is never in the canonical record
    assert "submitted_by" not in rec and "submittedBy" not in rec


def test_canonical_is_deterministic_across_nodes():
    o = _roundtrip(_mk_order())
    a = canonical_order(o, score=5027, block_number=100, tx_hash=b"\xab" * 32, chain_id=8453)
    b = canonical_order(o, score=5027, block_number=100, tx_hash=b"\xab" * 32, chain_id=8453)
    assert a == b
    assert corpus_hash([a]) == corpus_hash([b])  # two validators → identical hash


def test_corpus_hash_is_discovery_order_independent():
    o1 = canonical_order(_roundtrip(_mk_order(nonce=1)), score=5001, block_number=10,
                         tx_hash=b"\x01" * 32, chain_id=8453)
    o2 = canonical_order(_roundtrip(_mk_order(nonce=2)), score=5002, block_number=20,
                         tx_hash=b"\x02" * 32, chain_id=8453)
    assert corpus_hash([o1, o2]) == corpus_hash([o2, o1])


def test_different_params_change_the_hash():
    a = canonical_order(_roundtrip(_mk_order(params=b"\xaa")), score=5000, block_number=1,
                        tx_hash=b"\x01" * 32, chain_id=8453)
    b = canonical_order(_roundtrip(_mk_order(params=b"\xbb")), score=5000, block_number=1,
                        tx_hash=b"\x01" * 32, chain_id=8453)
    assert corpus_hash([a]) != corpus_hash([b])
