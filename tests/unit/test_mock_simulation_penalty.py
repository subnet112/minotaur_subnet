"""Mock-simulation anti-gaming penalty in score aggregation.

When a benchmark scenario can't run a real Anvil simulation it may fall back to
a FABRICATED mock (see test_benchmark_fail_closed.py for where the flag is set).
Those results are tagged ``BenchmarkResult.mock_simulation = True``. A solver
must NOT be able to inflate its benchmark score by producing fabricated passes:
``_compute_stage_score`` (and therefore ``_compute_avg_score``) must EXCLUDE
mock-flagged results from the numerator while STILL counting them in the
denominator — so a mock result dilutes the average rather than boosting it.

Grounded in:
  * minotaur_subnet/harness/orchestrator.py:127  (BenchmarkResult dataclass)
  * minotaur_subnet/harness/benchmark_worker.py:1469 (_compute_avg_score)
  * minotaur_subnet/harness/benchmark_worker.py:1496 (_compute_stage_score:
      skips ``r.score <= 0`` and ``r.mock_simulation`` in the numerator but
      divides by ``len(stage_results)``)
  * minotaur_subnet/harness/benchmark_worker.py:68 (_result_stage: 'historical'
      when intent_id contains ':hist:' / 'hist:ord_' / ends with ':hist')
"""
from __future__ import annotations

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _worker() -> BenchmarkWorker:
    # The real ctor wires up stores/clients/config; we only exercise the pure
    # aggregation helpers, so build a bare instance via __new__.
    return BenchmarkWorker.__new__(BenchmarkWorker)


def _real(intent_id: str, score: float) -> BenchmarkResult:
    return BenchmarkResult(intent_id=intent_id, score=score, mock_simulation=False)


def _mock(intent_id: str, score: float) -> BenchmarkResult:
    return BenchmarkResult(intent_id=intent_id, score=score, mock_simulation=True)


# ── single-stage (synthetic only) bootstrap path ───────────────────────────────


def test_mock_result_excluded_from_numerator_but_counts_in_denominator():
    """A mock pass contributes 0 to the sum but 1 to the count: it dilutes."""
    w = _worker()
    # One genuine 0.8 pass + one fabricated "1.0" pass. With no historical
    # scenarios, _compute_avg_score falls through to pure Stage 1.
    results = [_real("dex:s1", 0.8), _mock("dex:s2", 1.0)]
    avg = w._compute_avg_score(results)
    # Numerator = 0.8 (mock skipped); denominator = 2 (mock still counted).
    assert avg == pytest.approx(0.4)


def test_mock_cannot_outscore_an_all_real_solver():
    """Anti-gaming: faking the failing scenarios must never beat real passes."""
    w = _worker()
    honest = [_real("dex:s1", 0.9), _real("dex:s2", 0.0)]   # handled 1/2 for real
    gamer = [_real("dex:s1", 0.9), _mock("dex:s2", 1.0)]    # faked the second
    honest_avg = w._compute_avg_score(honest)
    gamer_avg = w._compute_avg_score(gamer)
    # The fabricated 1.0 is zeroed, so the gamer scores no higher than honest.
    assert gamer_avg <= honest_avg
    assert gamer_avg == pytest.approx(0.45)
    assert honest_avg == pytest.approx(0.45)


def test_all_mock_results_yield_zero_average():
    """A solver that fabricated EVERY scenario scores 0, not its claimed scores."""
    w = _worker()
    results = [_mock("dex:s1", 1.0), _mock("dex:s2", 0.95), _mock("dex:s3", 1.0)]
    assert w._compute_avg_score(results) == pytest.approx(0.0)


def test_no_mock_results_matches_plain_average():
    """Sanity: with zero mocks the average is the ordinary score mean."""
    w = _worker()
    results = [_real("dex:s1", 0.6), _real("dex:s2", 0.4)]
    assert w._compute_avg_score(results) == pytest.approx(0.5)


# ── two-stage (synthetic 40% + historical 60%) composite path ──────────────────


def test_mock_penalty_applies_within_each_weighted_stage():
    """Mock dilution holds independently in the 0.4*S1 + 0.6*S2 composite."""
    w = _worker()
    results = [
        # Stage 1 (synthetic): one real 0.8, one fabricated 1.0 -> 0.8/2 = 0.4
        _real("dex:syn1", 0.8),
        _mock("dex:syn2", 1.0),
        # Stage 2 (historical): one real 0.6, one fabricated 1.0 -> 0.6/2 = 0.3
        _real("dex:hist:ord_aaa", 0.6),
        _mock("dex:hist:ord_bbb", 1.0),
    ]
    avg = w._compute_avg_score(results)
    # 0.4 * 0.4 + 0.6 * 0.3 = 0.16 + 0.18 = 0.34
    assert avg == pytest.approx(0.34)


def test_historical_stage_mock_does_not_leak_into_score():
    """A fabricated historical pass must not lift the 60%-weighted stage."""
    w = _worker()
    clean = [
        _real("dex:syn1", 0.5),
        _real("dex:hist:ord_aaa", 0.6),
    ]
    gamed = [
        _real("dex:syn1", 0.5),
        _real("dex:hist:ord_aaa", 0.6),
        _mock("dex:hist:ord_bbb", 1.0),  # fabricated extra historical pass
    ]
    clean_avg = w._compute_avg_score(clean)
    gamed_avg = w._compute_avg_score(gamed)
    # The fabricated historical result can only dilute the historical stage.
    assert gamed_avg < clean_avg


def test_empty_results_is_zero():
    w = _worker()
    assert w._compute_avg_score([]) == pytest.approx(0.0)
