"""Cross-actor identical-code reject (screening_pipeline.find_prior_cross_actor_copy).

First submitter of a normalized fingerprint owns it; later copies from OTHER
actors are rejected pre-build. Same-actor resubmits (the no-fault waitlist
loop) must never match — that false positive was measured at 41.8% of
serious-miner submissions for the naive any-prior-fingerprint rule.
"""

import pytest

from minotaur_subnet.api.routes.submissions.screening_pipeline import (
    find_prior_cross_actor_copy,
)
from minotaur_subnet.harness.actor import set_coldkey_provider

COLDKEYS = {"A1": "CK_A", "A2": "CK_A", "S": "CK_S"}


@pytest.fixture(autouse=True)
def _actor_map():
    set_coldkey_provider(lambda: COLDKEYS)
    yield
    set_coldkey_provider(None)


class _Store:
    def __init__(self, rows):
        self.rows = rows  # (hotkey, created_at, submission_id)

    def fingerprint_submitters(self, fingerprint, *, exclude_submission_id=None):
        return [r for r in self.rows if r[2] != exclude_submission_id]


def test_copy_from_other_actor_is_flagged():
    store = _Store([("S", 100.0, "sub_orig")])
    hit = find_prior_cross_actor_copy(
        store, "fp1", submission_id="sub_copy", hotkey="A1", created_at=200.0,
    )
    assert hit == ("S", 100.0, "sub_orig")


def test_first_submitter_is_never_flagged():
    store = _Store([("A1", 200.0, "sub_late")])
    assert find_prior_cross_actor_copy(
        store, "fp1", submission_id="sub_first", hotkey="S", created_at=100.0,
    ) is None


def test_same_actor_resubmit_passes_even_across_hotkeys():
    # A2 resubmitting the fleet's own code (same coldkey as A1) is a resubmit,
    # not a copy — the designed waitlist loop keeps working.
    store = _Store([("A1", 100.0, "sub_orig")])
    assert find_prior_cross_actor_copy(
        store, "fp1", submission_id="sub_re", hotkey="A2", created_at=200.0,
    ) is None


def test_race_resolves_to_single_winner_deterministically():
    # Two copies in flight concurrently: each sees the other in the store.
    # (created_at, submission_id) is a total order → exactly one is flagged.
    rows = [("S", 100.0, "sub_s"), ("A1", 100.0, "sub_a")]
    flag_s = find_prior_cross_actor_copy(
        _Store(rows), "fp1", submission_id="sub_s", hotkey="S", created_at=100.0,
    )
    flag_a = find_prior_cross_actor_copy(
        _Store(rows), "fp1", submission_id="sub_a", hotkey="A1", created_at=100.0,
    )
    assert (flag_s is None) != (flag_a is None)  # one owner, one copy
    assert flag_s is not None  # "sub_a" < "sub_s" → A1 owns the tie


def test_earliest_prior_copy_is_reported():
    store = _Store([("S", 300.0, "sub_late"), ("S", 100.0, "sub_early")])
    hit = find_prior_cross_actor_copy(
        store, "fp1", submission_id="sub_copy", hotkey="A1", created_at=400.0,
    )
    assert hit == ("S", 100.0, "sub_early")


def test_unknown_hotkeys_are_distinct_actors():
    set_coldkey_provider(lambda: {})  # metagraph not synced yet
    store = _Store([("other-hk", 100.0, "sub_orig")])
    hit = find_prior_cross_actor_copy(
        _Store([("other-hk", 100.0, "sub_orig")]), "fp1",
        submission_id="sub_copy", hotkey="my-hk", created_at=200.0,
    )
    assert hit is not None  # per-hotkey fallback still rejects the copy
