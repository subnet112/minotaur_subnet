"""The champion approval deadline must outlive the attest that spends it.

The signature is spent by certify() at ADOPTION, which fires at the round's
activation (effective_epoch). A fixed now+TTL deadline expires mid-wait once the
autoscaled decision window (#421) pushes activation more than TTL past the mint
-> certify() reverts "Expired" -> merge_failed:attest_failed.

Live evidence these reproduce (leader, 2026-07-15): every round whose
(deadline - activation) was negative aborted attest_failed; every round with a
positive margin activated. 14/14, no exceptions.
"""

from minotaur_subnet.api.routes.submissions.champion_consensus import (
    _champion_approval_deadline,
)
from minotaur_subnet.consensus.champion_manager import (
    CHAMPION_APPROVAL_ACTIVATION_MARGIN_SECONDS,
    CHAMPION_APPROVAL_DEADLINE_SECONDS,
)
from minotaur_subnet.epoch.clock import EPOCH_SECONDS

# round-e29735224-n1: certified 11:30:08, activation 12:48:00, old deadline
# 12:29:11 -> attest fired 18.8 min after its own signature died.
LIVE_FAIL_CERTIFIED_AT = 1784115008
LIVE_FAIL_EFFECTIVE_EPOCH = 29735328


def _activation_ts(epoch: int) -> int:
    return int(epoch) * EPOCH_SECONDS


def test_reproduces_the_live_attest_failure_and_fixes_it():
    """The exact round that aborted merge_failed:attest_failed now survives."""
    activation = _activation_ts(LIVE_FAIL_EFFECTIVE_EPOCH)

    legacy = LIVE_FAIL_CERTIFIED_AT + CHAMPION_APPROVAL_DEADLINE_SECONDS
    assert legacy < activation, "precondition: the legacy TTL died before activation"

    fixed = _champion_approval_deadline(
        LIVE_FAIL_EFFECTIVE_EPOCH, now=LIVE_FAIL_CERTIFIED_AT
    )
    assert fixed > activation, "the signature must still be alive at the attest"
    assert fixed >= activation + CHAMPION_APPROVAL_ACTIVATION_MARGIN_SECONDS


def test_covers_activation_for_any_decision_window():
    """Arbitrarily large windows (#421 scales 4 epochs per submission) stay covered."""
    now = 1_700_000_000
    for n_submissions in (0, 3, 13, 20, 50, 500):
        window = 10 + 4 * n_submissions
        activation_epoch = (now // EPOCH_SECONDS) + window + 2
        deadline = _champion_approval_deadline(activation_epoch, now=now)
        assert deadline > _activation_ts(activation_epoch), (
            f"expired before activation at n={n_submissions} (window={window})"
        )


def test_never_shortens_an_existing_deadline():
    """Strictly widening: a near activation keeps the legacy TTL floor."""
    now = 1_700_000_000
    soon = (now // EPOCH_SECONDS) + 1  # activation a minute out
    assert _champion_approval_deadline(soon, now=now) == (
        now + CHAMPION_APPROVAL_DEADLINE_SECONDS
    )


def test_past_activation_reattest_keeps_the_legacy_floor():
    """Re-attesting a standing champion (activation long elapsed) is unaffected."""
    now = 1_700_000_000
    stale = (now // EPOCH_SECONDS) - 5000
    assert _champion_approval_deadline(stale, now=now) == (
        now + CHAMPION_APPROVAL_DEADLINE_SECONDS
    )


def test_missing_effective_epoch_falls_back_to_the_floor():
    now = 1_700_000_000
    assert _champion_approval_deadline(None, now=now) == (
        now + CHAMPION_APPROVAL_DEADLINE_SECONDS
    )


def test_monotonic_in_activation():
    """Later activation never yields an earlier deadline."""
    now = 1_700_000_000
    base = now // EPOCH_SECONDS
    deadlines = [_champion_approval_deadline(base + w, now=now) for w in range(0, 400, 20)]
    assert deadlines == sorted(deadlines)
