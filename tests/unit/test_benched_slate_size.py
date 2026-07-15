"""The decision window must scale with what gets BENCHED, not what got submitted.

#421 sizes the window off the slate to cover the serial benchmark (#387). #499
made only the rotation slate benchable, and #620 parked rotation's overflow in
`waitlisted` instead of `rejected` — silently defeating the autoscale's private
`status != "rejected"` filter. Never-benched submissions then inflated the window
(live: 10 + 4*19 = 86 epochs for a 3-submission slate), pushing activation past
the champion approval's lifetime -> certify() "Expired" -> merge_failed:attest_failed.
"""

from minotaur_subnet.api.routes.submissions.round_manager import (
    autoscaled_decision_window,
)
from minotaur_subnet.api.startup import _benched_slate_size
from minotaur_subnet.harness.rotation import benchable_candidate_count


class _Sub:
    def __init__(self, status):
        self.status = status


def test_uses_the_rotation_slate_not_the_submission_count():
    rot = {"applied": True, "selected": ["a", "b", "c"], "skipped": ["d"] * 16}
    assert _benched_slate_size("r", rot) == 3


def test_reproduces_the_live_window_inflation():
    """round-e29735696-n1: 19 candidates, 3 slots, 16 waitlisted -> window was 86."""
    rot = {"applied": True, "selected": ["a", "b", "c"], "skipped": ["s"] * 16}
    store_view = [_Sub("scored")] * 2 + [_Sub("waitlisted")] * 17 + [_Sub("rejected")] * 2

    # The old rule: everything not literally "rejected" counted.
    stale = len([s for s in store_view if s.status != "rejected"])
    assert stale == 19
    assert autoscaled_decision_window(
        stale, base_epochs=10, per_sub_epochs=4, floor_epochs=30
    ) == 86, "precondition: the live 86-epoch window"

    # The fix: size off the slate that actually gets benched.
    fixed = _benched_slate_size("r", rot)
    assert fixed == 3
    assert autoscaled_decision_window(
        fixed, base_epochs=10, per_sub_epochs=4, floor_epochs=30
    ) == 30


def test_waitlisted_is_not_benchable():
    """The exact status #620 introduced must not count toward the window."""
    assert benchable_candidate_count([_Sub("waitlisted")] * 17) == 0


def test_benchable_excludes_every_terminal_status():
    subs = [
        _Sub("queued"), _Sub("screening_stage_2"),   # benchable
        _Sub("rejected"), _Sub("adopted"), _Sub("waitlisted"),  # terminal
    ]
    assert benchable_candidate_count(subs) == 2


def test_falls_back_to_candidates_when_rotation_disabled(monkeypatch):
    """slots<=0 means rotation is off and EVERY candidate is benched."""
    subs = [_Sub("queued")] * 4 + [_Sub("rejected")]
    import minotaur_subnet.api.routes.submissions as _subs_mod

    class _Store:
        def list_by_round(self, _rid):
            return subs

    monkeypatch.setattr(_subs_mod, "get_store", lambda: _Store())
    assert _benched_slate_size("r", {"applied": False, "reason": "disabled"}) == 4
    assert _benched_slate_size("r", None) == 4


def test_store_failure_degrades_to_the_floor_not_a_crash():
    """A store hiccup must never block the close; the window floor covers it."""
    assert _benched_slate_size("r", {"applied": True, "selected": None}) == 0
