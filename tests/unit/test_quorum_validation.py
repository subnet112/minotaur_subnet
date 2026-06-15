"""Diverse-subset quorum validation — the model's core value proposition.

Each validator votes on its OWN diverse subset of real orders (per-validator seed)
and applies the shared `evaluate_adoption` rule. These tests demonstrate, using the
REAL sampler + REAL rule:

1. a genuinely-better challenger is adopted by quorum;
2. a challenger that REGRESSES on a subset of orders (overfit to common cases) is
   caught by the validators whose diverse subset includes the regressing orders ->
   quorum is NOT reached;
3. the contrast: a SHARED subset (legacy) can miss the regression entirely and let
   the overfit through — the exact failure mode diversity fixes.
"""
from __future__ import annotations

import math

from minotaur_subnet.epoch.adopt_rule import evaluate_adoption
from minotaur_subnet.harness.order_sampler import sample_historical_orders


class _Store:
    def __init__(self, orders):
        self._orders = orders

    def list_orders(self):
        return list(self._orders)


def _order(oid):
    return {
        "order_id": oid, "chain_id": 8453, "status": "filled",
        "block_number": 28_000_000, "params": {},
    }


POOL = [_order(f"ord_{i:03d}") for i in range(60)]
STORE = _Store(POOL)
N_VALIDATORS = 7
QUORUM = math.ceil(N_VALIDATORS * 0.6)  # 5-of-7


def _vote(subset_ids: set[str], hard_ids: set[str]) -> bool:
    """One validator's vote given which orders it sampled. The challenger regresses
    hard whenever its subset includes a 'hard' order (overfit); otherwise it beats
    the champion. Uses the real shared adoption rule."""
    hit_hard = bool(subset_ids & hard_ids)
    chal = 0.40 if hit_hard else 0.90
    champ = 0.80
    adopt, _ = evaluate_adoption(
        challenger_score=chal, champion_score=champ,
        challenger_scorecard={"app_scores": {"dex": chal}, "app_onchain": {}},
        champion_scorecard={"app_scores": {"dex": champ}, "app_onchain": {}},
        dethrone_margin=0.005, has_champion=True,
    )
    return adopt


def _clean_env(monkeypatch):
    for k in (
        "ADOPT_RULE", "MIN_CHAMPION_SCORE", "PER_APP_MIN_SCORE", "MAX_APP_REGRESSION",
        "ONCHAIN_FLOOR_BPS",
    ):
        monkeypatch.delenv(k, raising=False)


def _diverse_tally(round_id: str, hard_ids: set[str]) -> int:
    adopts = 0
    for v in range(N_VALIDATORS):
        sub = sample_historical_orders(
            STORE, round_id, n_per_chain=10, validator_seed=f"validator-{v}"
        )
        ids = {o["order_id"] for o in sub}
        if _vote(ids, hard_ids):
            adopts += 1
    return adopts


def test_good_challenger_reaches_quorum(monkeypatch):
    _clean_env(monkeypatch)
    # No regressing orders -> every validator's subset shows a clean win -> ADOPT.
    adopts = _diverse_tally("round-good", hard_ids=set())
    assert adopts == N_VALIDATORS
    assert adopts >= QUORUM  # adopted


def test_diverse_subsets_reject_overfit_challenger(monkeypatch):
    _clean_env(monkeypatch)
    # The challenger regresses on 20 of 60 orders (overfit to the other 40). Diverse
    # validators collectively cover the regressing orders -> too few ADOPT for quorum.
    hard = {f"ord_{i:03d}" for i in range(40, 60)}
    adopts = _diverse_tally("round-overfit", hard_ids=hard)
    assert adopts < QUORUM, f"diverse quorum must reject the overfit challenger; got {adopts} ADOPT"


def test_shared_subset_can_miss_overfit(monkeypatch):
    _clean_env(monkeypatch)
    # Contrast: with the legacy SHARED draw (validator_seed=None) every validator
    # samples the SAME orders. If the regressing orders are exactly the ones the
    # shared draw misses, ALL validators see a clean win and adopt the overfit — the
    # failure mode per-validator diversity exists to prevent.
    shared = sample_historical_orders(STORE, "round-shared", n_per_chain=10, validator_seed=None)
    shared_ids = {o["order_id"] for o in shared}
    hard = {o["order_id"] for o in POOL if o["order_id"] not in shared_ids}  # everything NOT drawn
    adopts = sum(_vote(shared_ids, hard) for _ in range(N_VALIDATORS))
    assert adopts == N_VALIDATORS  # overfit slips through the shared subset
