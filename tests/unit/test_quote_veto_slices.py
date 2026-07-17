"""Quote-veto slice partition: merge the quote remainder into follower veto slices
so the distributed veto cross-checks quote scenarios too (quorum>1 hardening).

Gated on BENCHMARK_QUOTE_CORPUS — INERT while off (slices byte-identical to the
order-only partition). Quote cases (content-addressed q_ ids) drop into the same
slice pool; the wire/protocol/reverify are unchanged (Phase-2 defensive fixes).
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.harness.order_sampler import (
    partition_follower_slices,
    sample_historical_quotes,
    VETO_SLICE_SIZE,
)

_D = 40000  # drawing round opened_epoch
_ROUND = f"round-e{_D}-n1"


def _order(oid, out):
    return {"order_id": oid, "app_id": "app_test", "chain_id": 8453, "status": "filled",
            "params": {"input_token": "0xW", "output_token": out, "input_amount": "1000000000000000000"}}


def _quote(qid, out, epoch=_D - 100):
    return {"quote_id": qid, "app_id": "app_test", "chain_id": 8453, "intent_function": "swap",
            "captured_opened_epoch": epoch,
            "params": {"input_token": "0xW", "output_token": out, "input_amount": "1000000000000000000"}}


class _DualStore:
    def __init__(self, orders, quotes):
        self._orders, self._quotes = orders, quotes

    def list_orders(self): return list(self._orders)
    def list_quotes(self): return list(self._quotes)
    def list_apps(self): return []


def _store():
    # 70 orders + 60 distinct-shape quotes → the canonical draws (cap 50/chain) leave
    # an ORDER remainder (20) AND a QUOTE remainder (10) for the slices.
    orders = [_order(f"ord_{i:03d}", f"0xO{i}") for i in range(70)]
    quotes = [_quote(f"q_{i:03d}", f"0xQ{i}") for i in range(60)]
    return _DualStore(orders, quotes)


def _all_ids(slices):
    return {o["order_id"] for s in slices for o in s}


class TestQuoteVetoMerge:
    def test_inert_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("BENCHMARK_QUOTE_CORPUS", raising=False)
        s = _store()
        off = partition_follower_slices(s, _ROUND, chain_ids=[8453])
        ids = _all_ids(off)
        assert ids and not any(i.startswith("q_") for i in ids)  # NO quote ids when off

    def test_quotes_merged_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        s = _store()
        on = partition_follower_slices(s, _ROUND, chain_ids=[8453])
        ids = _all_ids(on)
        assert any(i.startswith("q_") for i in ids)      # quote ids present
        assert any(i.startswith("ord_") for i in ids)    # order ids still present

    def test_flag_on_is_superset_of_flag_off_orders(self, monkeypatch):
        # Merging quotes must NOT drop or change the order slices' membership.
        s = _store()
        monkeypatch.delenv("BENCHMARK_QUOTE_CORPUS", raising=False)
        off_orders = {i for i in _all_ids(partition_follower_slices(s, _ROUND, chain_ids=[8453]))}
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        on_ids = _all_ids(partition_follower_slices(s, _ROUND, chain_ids=[8453]))
        assert off_orders <= on_ids                       # every order-remainder id survives

    def test_quote_slices_disjoint_from_canonical_draw(self, monkeypatch):
        # The quote ids in slices must NEVER overlap the scored quote corpus (the
        # canonical draw), exactly like the order path — else the veto would re-check
        # the same rows the adoption quorum already scored.
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        s = _store()
        slice_q = {i for i in _all_ids(partition_follower_slices(s, _ROUND, chain_ids=[8453]))
                   if i.startswith("q_")}
        canonical_q = {q["order_id"] for q in sample_historical_quotes(s, _ROUND)}
        assert slice_q and not (slice_q & canonical_q)

    def test_deterministic(self, monkeypatch):
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        s = _store()
        a = partition_follower_slices(s, _ROUND, chain_ids=[8453])
        b = partition_follower_slices(s, _ROUND, chain_ids=[8453])
        assert [[o["order_id"] for o in sl] for sl in a] == [[o["order_id"] for o in sl] for sl in b]

    def test_cutoff_ineligible_quotes_never_in_slices(self, monkeypatch):
        # A quote captured THIS round (>= draw_epoch) or unanchored must appear in
        # NEITHER the canonical draw NOR a veto slice (it has no leader row to verify).
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        quotes = ([_quote(f"q_ok_{i}", f"0xQ{i}", epoch=_D - 50) for i in range(60)]
                  + [_quote("q_thisround", "0xNOW", epoch=_D)         # captured this round
                     , _quote("q_unanchored", "0xNULL", epoch=None)])  # unanchored
        s = _DualStore([], quotes)
        ids = _all_ids(partition_follower_slices(s, _ROUND, chain_ids=[8453]))
        canonical = {q["order_id"] for q in sample_historical_quotes(s, _ROUND)}
        assert "q_thisround" not in ids and "q_thisround" not in canonical
        assert "q_unanchored" not in ids and "q_unanchored" not in canonical

    def test_single_chain_only(self, monkeypatch):
        # Quotes on a non-anchor chain must not enter slices (harness has one scalar
        # fork_block; run_slice_bench refuses multi-chain).
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        quotes = ([_quote(f"q_{i}", f"0xQ{i}") for i in range(60)]
                  + [{**_quote("q_offchain", "0xX"), "chain_id": 1}])
        s = _DualStore([], quotes)
        ids = _all_ids(partition_follower_slices(s, _ROUND, chain_ids=[8453]))
        assert "q_offchain" not in ids
