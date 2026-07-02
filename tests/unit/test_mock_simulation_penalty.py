"""Mock-simulation anti-gaming tracking in the benchmark scorecard.

When a benchmark scenario can't run a real Anvil simulation it may fall back to a
FABRICATED mock (see test_benchmark_fail_closed.py for where the flag is set).
Those results are tagged ``BenchmarkResult.mock_simulation = True``.

The scalar two-stage score composite that once *diluted* a solver's average with
mock results (``_compute_avg_score`` / ``_compute_stage_score``, weighting a
synthetic Stage 1 at 0.4 and a historical Stage 2 at 0.6) was removed in the
single-stage / relative-scoring cutover. Adoption now compares per-order RAW
delivered output (``relative_scoring.evaluate_relative_adoption``) and mock
fabrication is prevented AT THE SOURCE by the fail-closed ``require_real_sim``
switch (exercised by test_benchmark_fail_closed.py) — it is no longer diluted after
the fact inside a scalar average.

What survives — and what this file now pins — is the DIAGNOSTIC tracking that keeps
fabricated passes VISIBLE so they can never be silently credited as real value:
``BenchmarkWorker._build_scorecard`` counts every mock result in the denominator
(``total``) and flags it separately via ``mock_simulation_count`` /
``mock_simulation_ratio``. A mock result is never dropped from the denominator, an
all-real run reports a 0.0 ratio, and an all-mock run is reported as fully (ratio
1.0) fabricated.

Grounded in:
  * minotaur_subnet/harness/orchestrator.py       (BenchmarkResult.mock_simulation)
  * minotaur_subnet/harness/benchmark_worker.py:_build_scorecard
      (mock_simulation_count; every result — mock included — counts in ``total``)
  * minotaur_subnet/harness/benchmark_worker.py:BenchmarkScorecard.mock_simulation_ratio
"""
from __future__ import annotations

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult


def _worker() -> BenchmarkWorker:
    # We only exercise the pure ``_build_scorecard`` aggregation helper (it reads
    # its ``results`` arg and no instance state), so build a bare instance via
    # __new__ — the real ctor wires up stores/clients/config we don't need here.
    return BenchmarkWorker.__new__(BenchmarkWorker)


def _real(intent_id: str, score: float) -> BenchmarkResult:
    return BenchmarkResult(intent_id=intent_id, score=score, mock_simulation=False)


def _mock(intent_id: str, score: float) -> BenchmarkResult:
    return BenchmarkResult(intent_id=intent_id, score=score, mock_simulation=True)


def test_mock_result_counts_in_denominator_and_is_flagged():
    """A mock result stays in the denominator (``total``) AND is flagged.

    The retired scalar composite excluded the mock from the numerator while keeping
    it in the denominator so a fabricated pass *diluted* the average rather than
    boosting it. The numerator is gone, but the load-bearing half of that invariant
    lives on: a mock is counted-but-flagged, never silently dropped so as to make a
    partial-coverage run look complete.
    """
    w = _worker()
    card = w._build_scorecard([_real("dex:s1", 0.8), _mock("dex:s2", 1.0)])
    assert card.total == 2                    # mock still counted in the denominator
    assert card.mock_simulation_count == 1    # ...but flagged, not credited as real
    assert card.mock_simulation_ratio == pytest.approx(0.5)


def test_mock_run_is_flagged_where_an_all_real_run_is_clean():
    """Anti-gaming: fabricating a failing scenario can't masquerade as an all-real run.

    With the scalar score gone a fabricated pass can no longer inflate an average —
    and, just as importantly, it cannot hide: the gamer's scorecard carries a
    nonzero ``mock_simulation_ratio`` while the honest all-real solver's is 0.0, so a
    downstream gate can always tell the fabrication apart from genuine delivery.
    """
    w = _worker()
    honest = [_real("dex:s1", 0.9), _real("dex:s2", 0.0)]   # handled 1/2 for real
    gamer = [_real("dex:s1", 0.9), _mock("dex:s2", 1.0)]    # faked the second
    honest_card = w._build_scorecard(honest)
    gamer_card = w._build_scorecard(gamer)
    assert honest_card.mock_simulation_count == 0
    assert honest_card.mock_simulation_ratio == pytest.approx(0.0)
    assert gamer_card.mock_simulation_count == 1
    assert gamer_card.mock_simulation_ratio > honest_card.mock_simulation_ratio


def test_all_mock_run_is_fully_flagged():
    """A solver that fabricated EVERY scenario is reported as 100% mock, not clean."""
    w = _worker()
    results = [_mock("dex:s1", 1.0), _mock("dex:s2", 0.95), _mock("dex:s3", 1.0)]
    card = w._build_scorecard(results)
    assert card.total == 3
    assert card.mock_simulation_count == 3
    assert card.mock_simulation_ratio == pytest.approx(1.0)


def test_no_mock_run_is_reported_clean():
    """Sanity: with zero mocks the scorecard reports a 0.0 mock ratio."""
    w = _worker()
    card = w._build_scorecard([_real("dex:s1", 0.6), _real("dex:s2", 0.4)])
    assert card.total == 2
    assert card.mock_simulation_count == 0
    assert card.mock_simulation_ratio == pytest.approx(0.0)


def test_empty_results_scorecard_is_empty():
    w = _worker()
    card = w._build_scorecard([])
    assert card.total == 0
    assert card.mock_simulation_count == 0
    assert card.mock_simulation_ratio == pytest.approx(0.0)
