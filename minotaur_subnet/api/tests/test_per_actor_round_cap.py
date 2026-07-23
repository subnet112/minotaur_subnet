"""Per-OPERATOR round cap: one operator (coldkey ∪ github owner) gets N slots
per round however many hotkeys / coldkeys / accounts it splits into.

Generalizes the per-account cap; supersedes it when SUBMISSIONS_MAX_PER_ACTOR_
PER_ROUND is set. Degrades to hotkey ∪ owner when no coldkey resolver is given.
"""
import pytest

from minotaur_subnet.harness.submission_store import (
    SubmissionStore,
    _same_operator,
)


def _store(tmp_path):
    return SubmissionStore(persist_path=tmp_path / "s.json")


def _mk(store, hotkey, rid="rr", **kw):
    return store.create("r", "h", epoch=1, hotkey=hotkey, round_id=rid, **kw)


# A fake coldkey∪owner resolver: hotkeys of coldkey CK_A collapse to "CK_A".
FLEET = {"hkA1": "CK_A", "hkA2": "CK_A", "hkA3": "CK_A", "solo": "CK_S"}
def actor_of(hk):
    return FLEET.get(hk, hk)


# ── _same_operator ───────────────────────────────────────────────────────────

def test_same_operator_by_hotkey_owner_actor():
    assert _same_operator("h", "o", "h", "x") is True              # same hotkey
    assert _same_operator("h1", "Alice", "h2", "alice") is True    # same owner (ci)
    assert _same_operator("hkA1", "", "hkA2", "", actor_of) is True  # same coldkey
    assert _same_operator("hkA1", "", "solo", "", actor_of) is False # different coldkey
    assert _same_operator("h1", "", "h2", "") is False             # nothing shared
    assert _same_operator("h1", "", "h2", "", None) is False       # no resolver, no owner


# ── the operator cap: coldkey split across hotkeys AND accounts ───────────────

def test_actor_cap_collapses_coldkey_split_across_accounts():
    store = _store(None) if False else SubmissionStore(persist_path=None)
    # One coldkey CK_A, three hotkeys, three DIFFERENT github accounts — the
    # exact SF-1 shape the account-only cap misses.
    _mk(store, "hkA1", github_owner="acct1", max_per_actor_per_round=1, actor_of=actor_of)
    with pytest.raises(ValueError, match="already submitted"):
        _mk(store, "hkA2", github_owner="acct2", max_per_actor_per_round=1, actor_of=actor_of)
    with pytest.raises(ValueError, match="operator"):
        _mk(store, "hkA3", github_owner="acct3", max_per_actor_per_round=1, actor_of=actor_of)
    # A different coldkey is unaffected.
    _mk(store, "solo", github_owner="acct4", max_per_actor_per_round=1, actor_of=actor_of)
    assert store.count_by_round("rr") == 2


def test_actor_cap_counts_via_operator_helper():
    store = SubmissionStore(persist_path=None)
    _mk(store, "hkA1", github_owner="acct1", max_per_actor_per_round=2, actor_of=actor_of)
    _mk(store, "hkA2", github_owner="acct2", max_per_actor_per_round=2, actor_of=actor_of)
    assert store.count_by_operator_round("hkA3", "acct3", "rr", actor_of=actor_of) == 2
    assert store.count_by_operator_round("solo", "acctX", "rr", actor_of=actor_of) == 0


def test_actor_cap_degrades_to_owner_without_resolver():
    # actor_of=None (pre-metagraph): the operator cap == the old per-account cap.
    store = SubmissionStore(persist_path=None)
    _mk(store, "hkX", github_owner="alice", max_per_actor_per_round=1, actor_of=None)
    with pytest.raises(ValueError, match="already submitted"):
        _mk(store, "hkY", github_owner="ALICE", max_per_actor_per_round=1, actor_of=None)


def test_actor_cap_supersedes_owner_cap_value():
    # max_per_actor wins over max_per_owner when both set.
    store = SubmissionStore(persist_path=None)
    _mk(store, "hkA1", github_owner="a", max_per_owner_per_round=5,
        max_per_actor_per_round=1, actor_of=actor_of)
    with pytest.raises(ValueError, match="max 1 per round"):
        _mk(store, "hkA2", github_owner="b", max_per_owner_per_round=5,
            max_per_actor_per_round=1, actor_of=actor_of)


def test_actor_cap_exempts_inline_unmapped():
    # No owner, no coldkey (actor_of returns the hotkey itself) => only its own
    # hotkey; the per-hotkey cap governs it, the operator cap stays out.
    store = SubmissionStore(persist_path=None)
    _mk(store, "loneA", max_per_actor_per_round=1, actor_of=actor_of)   # actor_of("loneA")="loneA"
    _mk(store, "loneB", max_per_actor_per_round=1, actor_of=actor_of)   # different lone hotkey
    assert store.count_by_round("rr") == 2


def test_actor_cap_zero_disables():
    store = SubmissionStore(persist_path=None)
    for hk in ("hkA1", "hkA2", "hkA3"):
        _mk(store, hk, github_owner="a", max_per_actor_per_round=0,
            max_per_owner_per_round=0, actor_of=actor_of)
    assert store.count_by_round("rr") == 3
