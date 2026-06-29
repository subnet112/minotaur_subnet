"""Stale-bar abstain guard in EpochManager._should_adopt (#242/#244).

Locks in the fail-closed guard at manager.py:866 — the leader must ABSTAIN
(return False) on a winning challenger when an incumbent champion EXISTS but its
bar could NOT be freshly re-benchmarked this round (``_incumbent_refresh_failed``
is True). This mirrors the follower's conservative REJECT so the leader and
fleet never diverge on a stale champion bar.

These cases complement test_adoption_gate_p0.py::test_abstains_when_incumbent_bar_is_stale,
which covers stale+incumbent and fresh+incumbent but NOT the bootstrap
(no-incumbent) branch the guard explicitly carves out. Here we monkeypatch the
relative per-order verdict (``_evaluate_per_order_adoption``) to a constant ADOPT
so the stale-bar guard is the *only* thing that can return False — isolating the
guard under test.
"""

import types

import pytest

from minotaur_subnet.epoch import manager as manager_mod
from minotaur_subnet.epoch.manager import EpochManager


def _mgr(*, champion_id, incumbent_refresh_failed: bool) -> EpochManager:
    """Build a bare manager via __new__ (bypass ctor) with only the attributes
    ``_should_adopt`` touches before/at the stale-bar guard."""
    mgr = EpochManager.__new__(EpochManager)
    mgr._champion = types.SimpleNamespace(
        submission_id=champion_id, benchmark_score=0.6
    )
    mgr._incumbent_refresh_failed = incumbent_refresh_failed
    mgr._dethrone_margin = 0.005
    # Scorecard hooks: real callables so the (default-on) observability path in
    # _record_would_be_vote doesn't trip, though it is try/except-guarded anyway.
    mgr._get_scorecard = lambda sub: {"app_scores": {"app_A": 0.7}}
    mgr._get_incumbent_scorecard = lambda: {"app_scores": {"app_A": 0.6}}
    # Disable the would-be-vote side channel so nothing external is needed.
    mgr._vote_recorder = None
    mgr._record_would_be_vote = lambda challenger: None
    return mgr


def _challenger(score: float = 0.9):
    # A clear winner: anything that blocks adoption here is the stale-bar guard,
    # not the rule (which we force to ADOPT below).
    return types.SimpleNamespace(submission_id="chal", benchmark_score=score)


@pytest.fixture
def force_adopt(monkeypatch):
    """Force the relative per-order verdict to ADOPT so the ONLY thing that can
    return False from _should_adopt is the stale-bar guard under test."""
    monkeypatch.setattr(
        EpochManager, "_evaluate_per_order_adoption",
        lambda self, challenger: {
            "adopt": True, "reason": "forced-adopt-for-test",
            "n_wins": 1, "n_regressions": 0, "n_blind_spots": 0,
            "n_matched": 0, "scenarios_compared": 1,
        },
    )


@pytest.fixture(autouse=True)
def _no_freeze(monkeypatch):
    """Ensure the DISABLE_CHAMPION_ADOPTION freeze isn't what returns False."""
    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)


def test_stale_incumbent_abstains_even_for_a_winner(force_adopt):
    # Incumbent EXISTS + refresh FAILED -> stale bar -> ABSTAIN (False), even
    # though the rule would adopt this clear winner.
    mgr = _mgr(champion_id="champ", incumbent_refresh_failed=True)
    assert mgr._should_adopt(_challenger(0.9)) is False


def test_fresh_incumbent_lets_adopt_verdict_pass_through(force_adopt):
    # Incumbent EXISTS but bar is FRESH -> guard does not fire -> the (forced)
    # adopt verdict passes through.
    mgr = _mgr(champion_id="champ", incumbent_refresh_failed=False)
    assert mgr._should_adopt(_challenger(0.9)) is True


def test_no_incumbent_is_not_blocked_by_stale_flag(force_adopt):
    # Bootstrap: NO incumbent (submission_id falsy). Even with the stale flag set
    # the guard MUST NOT block — there is no bar to be stale against, so the
    # (forced) adopt verdict must pass through. This is the branch the existing
    # adoption_gate_p0 test does not exercise.
    mgr = _mgr(champion_id="", incumbent_refresh_failed=True)
    assert mgr._should_adopt(_challenger(0.9)) is True


def test_no_incumbent_with_none_submission_id_not_blocked(force_adopt):
    # Same bootstrap carve-out, but with submission_id=None (the seeded-but-empty
    # shape) rather than "" — still falsy, still must not be blocked.
    mgr = _mgr(champion_id=None, incumbent_refresh_failed=True)
    assert mgr._should_adopt(_challenger(0.9)) is True


def test_stale_flag_missing_defaults_to_not_stale(force_adopt):
    # A manager built via __new__ that never ran a refresh has no
    # _incumbent_refresh_failed attribute; the guard's getattr(..., False)
    # default means it is treated as NOT stale -> adopt verdict passes through.
    mgr = _mgr(champion_id="champ", incumbent_refresh_failed=False)
    del mgr._incumbent_refresh_failed
    assert mgr._should_adopt(_challenger(0.9)) is True
