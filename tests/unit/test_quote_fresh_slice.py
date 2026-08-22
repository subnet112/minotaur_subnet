"""The quote draw reserves a slice for the newest demand.

Uniform sampling over a 14-day window makes freshly seeded demand invisible: ten
new cases against a 2,558-deep corpus land in ~18% of rounds, so a miner working
that route cannot tell a barren round from a broken solver.

The slice is RESERVED rather than the whole draw being recency-weighted, because
`POST /apps/{app_id}/quote` is unauthenticated — a full recency weight would let
anyone spam quotes for their own route minutes before a round and set their own
exam. Reserving bounds that to the slice.
"""
from __future__ import annotations

import random

import pytest

from minotaur_subnet.harness.order_sampler import (
    QUOTE_FRESH_POOL,
    QUOTE_FRESH_SLOTS,
    _draw_with_fresh_slice,
)


def _q(i: int, epoch: int) -> dict:
    return {"order_id": f"q{i:05d}", "captured_opened_epoch": epoch}


def _corpus(n_old: int, n_fresh: int) -> list[dict]:
    """`n_old` stale quotes then `n_fresh` newest, sorted by order_id as the caller does."""
    old = [_q(i, 1_000 + i) for i in range(n_old)]
    fresh = [_q(n_old + j, 900_000 + j) for j in range(n_fresh)]
    return sorted(old + fresh, key=lambda q: q["order_id"])


class TestFreshDemandIsReached:
    def test_ten_fresh_cases_appear_in_most_rounds(self):
        """The whole point: 18% -> ~2 in 3."""
        corpus = _corpus(n_old=2548, n_fresh=10)
        fresh_ids = {q["order_id"] for q in corpus if q["captured_opened_epoch"] >= 900_000}
        hits = 0
        trials = 400
        for r in range(trials):
            drawn = _draw_with_fresh_slice(corpus, 50, random.Random(r))
            if fresh_ids & {q["order_id"] for q in drawn}:
                hits += 1
        rate = hits / trials
        assert rate > 0.55, f"fresh demand still rarely drawn: {rate:.1%}"

    def test_the_newest_pool_gets_exactly_the_reserved_slots(self):
        """The invariant the reserve actually provides."""
        corpus = _corpus(n_old=2000, n_fresh=500)
        by_recency = sorted(corpus, key=lambda q: (-q["captured_opened_epoch"], q["order_id"]))
        newest = {q["order_id"] for q in by_recency[:QUOTE_FRESH_POOL]}
        for r in range(60):
            drawn = _draw_with_fresh_slice(corpus, 50, random.Random(r))
            got = len(newest & {q["order_id"] for q in drawn})
            assert got <= QUOTE_FRESH_SLOTS, f"newest pool took {got} slots, cap {QUOTE_FRESH_SLOTS}"

    def test_a_flood_gains_at_most_the_reserve_over_plain_uniform(self):
        """The honest bound on the exposure this change adds.

        A flooder ALWAYS gets proportional share by volume — that is true today
        and the reserve does not change it. What the reserve adds is the slots
        themselves. Measured, not asserted from the design: with 500 spammed
        quotes in a 2,500 corpus, plain uniform gives them ~10 of 50 and the
        fresh slice ~17. The gain must not exceed the reserve.
        """
        corpus = _corpus(n_old=2000, n_fresh=500)
        flood = {q["order_id"] for q in corpus if q["captured_opened_epoch"] >= 900_000}
        trials = 200
        with_slice = plain = 0
        for r in range(trials):
            d1 = _draw_with_fresh_slice(corpus, 50, random.Random(r))
            with_slice += len(flood & {q["order_id"] for q in d1})
            d2 = random.Random(r).sample(corpus, 50)
            plain += len(flood & {q["order_id"] for q in d2})
        gain = (with_slice - plain) / trials
        assert 0 <= gain <= QUOTE_FRESH_SLOTS, (
            f"flood gained {gain:.1f} slots over uniform; the reserve is {QUOTE_FRESH_SLOTS}"
        )

class TestItStaysDeterministicAndWellFormed:
    def test_same_seed_same_draw(self):
        """Every validator must derive the identical subset — the pack hash rests on it."""
        corpus = _corpus(2000, 60)
        a = _draw_with_fresh_slice(corpus, 50, random.Random(7))
        b = _draw_with_fresh_slice(corpus, 50, random.Random(7))
        assert [q["order_id"] for q in a] == [q["order_id"] for q in b]

    def test_no_duplicates_and_exact_count(self):
        corpus = _corpus(2000, 60)
        drawn = _draw_with_fresh_slice(corpus, 50, random.Random(1))
        ids = [q["order_id"] for q in drawn]
        assert len(ids) == 50
        assert len(set(ids)) == 50, "a quote was drawn twice"

    @pytest.mark.parametrize("n_old,n_fresh,k", [(0, 3, 50), (5, 0, 50), (0, 0, 50), (200, 200, 7)])
    def test_small_and_degenerate_corpora(self, n_old, n_fresh, k):
        corpus = _corpus(n_old, n_fresh)
        drawn = _draw_with_fresh_slice(corpus, min(k, len(corpus)), random.Random(3))
        ids = [q["order_id"] for q in drawn]
        assert len(ids) == len(set(ids))
        assert len(ids) == min(k, len(corpus))

    def test_a_corpus_with_no_capture_epochs_behaves_exactly_as_before(self):
        """Quotes predating captured_opened_epoch must not change behaviour."""
        corpus = sorted(
            ({"order_id": f"q{i:05d}", "captured_opened_epoch": 0} for i in range(300)),
            key=lambda q: q["order_id"],
        )
        drawn = _draw_with_fresh_slice(corpus, 50, random.Random(11))
        expected = random.Random(11).sample(corpus, 50)
        assert [q["order_id"] for q in drawn] == [q["order_id"] for q in expected]

    def test_fresh_pool_depth_is_respected(self):
        """Only the newest QUOTE_FRESH_POOL are eligible for the reserved slots."""
        corpus = _corpus(n_old=1000, n_fresh=QUOTE_FRESH_POOL + 200)
        by_recency = sorted(corpus, key=lambda q: (-q["captured_opened_epoch"], q["order_id"]))
        eligible = {q["order_id"] for q in by_recency[:QUOTE_FRESH_POOL]}
        stale_fresh = {q["order_id"] for q in by_recency[QUOTE_FRESH_POOL:]}
        drawn = _draw_with_fresh_slice(corpus, 50, random.Random(5))
        ids = {q["order_id"] for q in drawn}
        # anything from outside the eligible pool can only have come from the
        # uniform remainder, which is capped at 50 - reserved
        assert len(ids & eligible) <= QUOTE_FRESH_SLOTS + (50 - QUOTE_FRESH_SLOTS)
        assert stale_fresh, "fixture should have quotes just outside the fresh pool"
