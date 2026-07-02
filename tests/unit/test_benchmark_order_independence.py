"""Golden order/K-independence tests for benchmark parallelization.

These pin the determinism the planned K-runtime benchmark pool relies on: running
scenarios in ANY execution order (or sharded across K workers) must yield a
byte-identical pack hash AND byte-identical consensus-driving scores. Land this
BEFORE the pool refactor — it is the regression guard that catches any accidental
execution-order or per-worker-session leakage into the consensus hash or the
vote-driving scorecard.

Two surfaces, two guarantees:

1. ``compute_pack_hash`` (the benchmark *inputs*) — sorts scenarios by
   ``(app_id, name)`` and historical ids alphabetically, so the hash is invariant
   to input order. The pool never reorders inputs, but this pins the property so a
   future edit that drops the ``sorted()`` is caught.

2. ``BenchmarkWorker._build_scorecard`` (the benchmark *outputs*) — the
   order-free diagnostic scalars (``app_scores`` per-app means, ``scenario_scores``,
   ``failures``/``total``) are order-independent, BUT ``app_onchain`` is a per-app
   LIST in results order. That
   asymmetry is the load-bearing constraint for the pool: workers MUST write
   results back by input index (``results[idx] = br``), NOT append in completion
   order — otherwise ``app_onchain`` (which the on-chain adoption rule ranks on)
   reorders and the scorecard diverges across hosts.

See: the benchmark-concurrency investigation (orchestrator.py:1174 scenario loop;
the "three hard serializers" → K-runtime pool design).
"""

import random

from minotaur_subnet.harness.benchmark_pack import compute_pack_hash
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult

_SEEDS = range(8)


def _scenarios() -> list[dict]:
    # Two apps, interleaved, several scenarios each — enough that order would
    # matter if the internal sort were dropped.
    return [
        {"app_id": "app_bbb", "name": "swap_large", "params": {"amount": 100}, "chains": [8453]},
        {"app_id": "app_aaa", "name": "swap_small", "params": {"amount": 1}, "chains": [8453]},
        {"app_id": "app_bbb", "name": "swap_small", "params": {"amount": 2}, "chains": [8453]},
        {"app_id": "app_aaa", "name": "swap_large", "params": {"amount": 99}, "chains": [8453]},
        {"app_id": "app_aaa", "name": "swap_mid", "params": {"amount": 50}, "chains": [8453]},
    ]


def _historical() -> list[str]:
    return ["ord_0007", "ord_0001", "ord_0042", "ord_0013"]


def _results() -> list[BenchmarkResult]:
    # Mixed apps + stages (synthetic + historical), a zero/failure for dilution,
    # and distinct on_chain_scores so list reordering is observable.
    return [
        BenchmarkResult(intent_id="app_aaa:swap_small", score=0.80, on_chain_score=7600),
        BenchmarkResult(intent_id="app_bbb:swap_large", score=0.40, on_chain_score=4300),
        BenchmarkResult(intent_id="app_aaa:swap_large", score=0.0, on_chain_score=None, error="revert"),
        BenchmarkResult(intent_id="app_bbb:swap_small", score=0.65, on_chain_score=6900),
        BenchmarkResult(intent_id="app_aaa:swap_mid", score=0.55, on_chain_score=6100),
        BenchmarkResult(intent_id="app_aaa:hist:ord_0042", score=0.70, on_chain_score=7100),
        BenchmarkResult(intent_id="app_bbb:hist:ord_0007", score=0.50, on_chain_score=5200),
    ]


def _shuffled(items: list, seed: int) -> list:
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


def _scorecard(results: list[BenchmarkResult]):
    # The scoring methods are pure over `results` (no instance state), so a
    # bare instance is enough to exercise the real aggregation.
    worker = object.__new__(BenchmarkWorker)
    return worker._build_scorecard(results)


# ── compute_pack_hash: inputs are order-independent ──────────────────────────


def test_pack_hash_invariant_to_synthetic_scenario_order():
    base = compute_pack_hash("round-1", _scenarios(), _historical())
    for seed in _SEEDS:
        assert compute_pack_hash("round-1", _shuffled(_scenarios(), seed), _historical()) == base


def test_pack_hash_invariant_to_historical_order():
    base = compute_pack_hash("round-1", _scenarios(), _historical())
    for seed in _SEEDS:
        assert compute_pack_hash("round-1", _scenarios(), _shuffled(_historical(), seed)) == base


def test_pack_hash_invariant_when_both_shuffled_together():
    base = compute_pack_hash("round-1", _scenarios(), _historical())
    for seed in _SEEDS:
        h = compute_pack_hash("round-1", _shuffled(_scenarios(), seed), _shuffled(_historical(), seed))
        assert h == base


def test_pack_hash_still_binds_round_id():
    # Sanity: the hash is content-sensitive, not a constant the order tests pass by accident.
    assert compute_pack_hash("round-1", _scenarios(), _historical()) != \
        compute_pack_hash("round-2", _scenarios(), _historical())


# ── _build_scorecard: consensus-driving scores are order-independent ─────────


def test_scorecard_scalar_scores_invariant_to_result_order():
    base = _scorecard(_results())
    for seed in _SEEDS:
        sc = _scorecard(_shuffled(_results(), seed))
        # The order-free diagnostic scalars — must be byte-identical regardless of
        # which worker finished which scenario first. (The retired ``global_score``
        # composite is gone; adoption/ranking now go through the relative rule over
        # per_intent raw_output, but these scorecard scalars still MUST NOT drift
        # with execution order, which is what the pool refactor relies on.)
        assert sc.app_scores == base.app_scores
        assert sc.scenario_scores == base.scenario_scores
        assert sc.failures == base.failures
        assert sc.total == base.total


def test_scorecard_app_onchain_is_order_preserving_so_pool_must_write_by_index():
    """``app_onchain`` is a per-app LIST in results order — NOT order-free.

    Load-bearing for the K-runtime pool: workers MUST write ``results[idx] = br``
    (by input index), never append in completion order. If they appended out of
    order, ``app_onchain`` (the on-chain adoption-rule signal) would reorder and
    the scorecard would diverge across hosts. This test pins that asymmetry:
    same-order is byte-identical (incl. ``app_onchain``); a reordered results list
    reorders ``app_onchain`` while the scalar ``app_scores`` stay identical. If
    ``_build_scorecard`` is ever changed to sort ``app_onchain``, relax this test
    AND the by-index requirement together.
    """
    base = _scorecard(_results())

    # Same (input) order → byte-identical, including the order-sensitive lists.
    assert _scorecard(_results()).app_onchain == base.app_onchain

    # A reordered results list reorders app_onchain for at least one app...
    reordered = list(reversed(_results()))
    assert _scorecard(reordered).app_onchain != base.app_onchain
    # ...while the scalar per-app means are unchanged (means are order-free).
    assert _scorecard(reordered).app_scores == base.app_scores


def test_scorecard_is_deterministic_for_identical_input():
    # Pure-function sanity: identical input → byte-identical full scorecard dict.
    assert _scorecard(_results()).to_dict() == _scorecard(_results()).to_dict()
