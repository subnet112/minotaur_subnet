"""Cross-actor identical-code reject (screening_pipeline.evaluate_fingerprint_ownership).

The EARLIEST submitter of a normalized fingerprint owns it; copies by other
actors reject pre-build, and in-flight copies that raced past their own check
are swept retroactively. Three review findings anchor this file's regression
cases: (1) an intermediate rejected copy must NOT block the owner's resubmits
(griefing primitive), (2) degraded coldkey attribution must stand down rather
than reject, (3) the concurrent-copy race must close via the sweep.
"""

import pytest

from minotaur_subnet.api.routes.submissions.screening_pipeline import (
    evaluate_fingerprint_ownership,
)
from minotaur_subnet.harness.actor import ActorResolver

# O = owner (coldkey CK_O); A1/A2 = a fleet (CK_A); X = unmapped (deregistered).
RESOLVER = ActorResolver(
    {"O": "CK_O", "A1": "CK_A", "A2": "CK_A", "B": "CK_B"}, source="test",
)


def _eval(entries, *, sid, hotkey, ts, resolver=RESOLVER):
    return evaluate_fingerprint_ownership(
        entries, submission_id=sid, hotkey=hotkey, created_at=ts,
        resolver=resolver,
    )


def test_copy_from_other_actor_is_rejected():
    entries = [("O", 100.0, "sub_orig", "waitlisted")]
    owner, sweep = _eval(entries, sid="sub_copy", hotkey="A1", ts=200.0)
    assert owner == ("O", 100.0, "sub_orig", "waitlisted")
    assert sweep == []


def test_first_submitter_owns_and_sweeps_inflight_copies():
    entries = [
        ("A1", 200.0, "sub_copy1", "screening_stage_1"),   # sweepable
        ("B", 300.0, "sub_copy2", "queued"),               # sweepable
        ("A2", 400.0, "sub_copy3", "rejected"),            # terminal — leave
        ("B", 500.0, "sub_copy4", "scored"),               # benched — leave
    ]
    owner, sweep = _eval(entries, sid="sub_orig", hotkey="O", ts=100.0)
    assert owner is None
    assert sorted(sweep) == ["sub_copy1", "sub_copy2"]


def test_owner_resubmit_passes_despite_intermediate_rejected_copy():
    # Review finding 1 (griefing): C's rejected copy sits between the owner's
    # original and the owner's designed waitlist resubmit — the resubmit must
    # pass because the EARLIEST submitter is my own actor.
    entries = [
        ("O", 100.0, "sub_orig", "waitlisted"),
        ("A1", 200.0, "sub_copy", "rejected"),
    ]
    owner, sweep = _eval(entries, sid="sub_resub", hotkey="O", ts=300.0)
    assert owner is None
    assert sweep == []  # the copy is already terminal — nothing to sweep


def test_fleet_sibling_resubmit_passes_and_sweeps_foreign_copies():
    entries = [
        ("A1", 100.0, "sub_orig", "waitlisted"),        # my sibling coined it
        ("B", 150.0, "sub_copy", "pending_selection"),  # foreign in-flight copy
    ]
    owner, sweep = _eval(entries, sid="sub_resub", hotkey="A2", ts=300.0)
    assert owner is None
    assert sweep == ["sub_copy"]


def test_unmapped_owner_is_indeterminate_never_rejects():
    # Review finding 2: the original hotkey deregistered (absent from the
    # coldkey map). Attribution is indeterminate — stand down, don't reject.
    entries = [("X", 100.0, "sub_orig", "waitlisted")]
    owner, sweep = _eval(entries, sid="sub_mine", hotkey="A1", ts=200.0)
    assert owner is None
    assert sweep == []


def test_unmapped_copier_is_never_swept():
    entries = [("X", 200.0, "sub_maybe", "queued")]
    owner, sweep = _eval(entries, sid="sub_orig", hotkey="O", ts=100.0)
    assert owner is None
    assert sweep == []


def test_same_hotkey_is_same_actor_even_unmapped():
    entries = [("X", 100.0, "sub_orig", "waitlisted")]
    owner, sweep = _eval(entries, sid="sub_resub", hotkey="X", ts=200.0)
    assert owner is None and sweep == []


def test_race_escapee_is_swept_by_late_running_owner_check():
    # Review finding 3: the later-created copy checked FIRST (owner's
    # fingerprint not yet persisted) and escaped into stage 2. The owner's
    # check runs last, sees the full picture, and sweeps it.
    entries = [("B", 101.0, "sub_late", "screening_stage_2")]
    owner, sweep = _eval(entries, sid="sub_early", hotkey="O", ts=100.0)
    assert owner is None
    assert sweep == ["sub_late"]


def test_created_at_tie_breaks_on_submission_id():
    rows = [("B", 100.0, "sub_a", "screening_stage_1")]
    owner, _ = _eval(rows, sid="sub_z", hotkey="O", ts=100.0)
    assert owner is not None  # "sub_a" < "sub_z" → B owns the tie
    rows = [("B", 100.0, "sub_z", "screening_stage_1")]
    owner, sweep = _eval(rows, sid="sub_a", hotkey="O", ts=100.0)
    assert owner is None and sweep == ["sub_z"]  # I own it; escapee swept
