"""Round-entry rotation (harness/rotation.py): LRU slate selection at close.

Fairness contract: with M contending miners and N slots, every miner is
selected at least once every ceil(M/N) rounds — never-benched miners first,
then longest-ago-benched; ties break by a per-round salted hash (deterministic,
publicly recomputable, no arrival-time or alphabetical advantage).
"""

import asyncio
import math
import threading
from types import SimpleNamespace

import pytest

from minotaur_subnet.harness.rotation import (
    RotationLedger,
    apply_rotation_slate,
    rotation_sort_key,
    select_rotation_slate,
)


def _sub(hotkey, sid=None, status="queued"):
    return SimpleNamespace(
        submission_id=sid or f"sub_{hotkey}",
        hotkey=hotkey,
        status=SimpleNamespace(value=status),
    )


class _FakeStore:
    def __init__(self, subs):
        self.subs = list(subs)
        self.rejected: dict[str, str] = {}

    def list_by_round(self, round_id):
        return self.subs

    def reject(self, submission_id, reason):
        self.rejected[submission_id] = reason


# ── pure selection ────────────────────────────────────────────────────────────

def test_never_benched_outrank_benched():
    subs = [_sub("A"), _sub("B"), _sub("C")]
    last = {"A": 100.0}  # A benched before; B, C never
    selected, skipped = select_rotation_slate(subs, 2, last, "r1")
    assert {s.hotkey for s in selected} == {"B", "C"}
    assert [s.hotkey for s in skipped] == ["A"]


def test_lru_order_among_benched():
    subs = [_sub("A"), _sub("B"), _sub("C")]
    last = {"A": 300.0, "B": 100.0, "C": 200.0}
    selected, skipped = select_rotation_slate(subs, 2, last, "r1")
    assert {s.hotkey for s in selected} == {"B", "C"}  # longest-ago first
    assert [s.hotkey for s in skipped] == ["A"]


def test_tie_break_is_deterministic_and_reshuffles_per_round():
    subs = [_sub(hk) for hk in ("A", "B", "C", "D")]
    order_r1 = [s.hotkey for s in select_rotation_slate(subs, 4, {}, "r1")[0]]
    order_r1_again = [s.hotkey for s in select_rotation_slate(subs, 4, {}, "r1")[0]]
    order_r2 = [s.hotkey for s in select_rotation_slate(subs, 4, {}, "r2")[0]]
    assert order_r1 == order_r1_again          # deterministic within a round
    assert order_r1 != sorted(order_r1) or order_r2 != order_r1  # salted, not alphabetical/fixed
    # the salt actually depends on the round id
    assert rotation_sort_key("A", "r1", {}) != rotation_sort_key("A", "r2", {})


def test_slots_zero_selects_nobody():
    subs = [_sub("A")]
    selected, skipped = select_rotation_slate(subs, 0, {}, "r1")
    assert selected == [] and skipped == subs


# ── ledger ────────────────────────────────────────────────────────────────────

def test_ledger_roundtrip_and_missing_file(tmp_path):
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    assert ledger.load() == {}  # missing file → everyone never-benched
    ledger.mark_selected(["A", "B", ""], 123.0)  # empty hotkey ignored
    assert ledger.load() == {"A": 123.0, "B": 123.0}
    ledger.mark_selected(["A"], 456.0)  # advances, keeps B
    assert ledger.load() == {"A": 456.0, "B": 123.0}


def test_ledger_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "rot.json"
    p.write_text("{not json")
    assert RotationLedger(str(p)).load() == {}


# ── apply at close ────────────────────────────────────────────────────────────

def test_apply_rejects_overflow_and_advances_ledger(tmp_path):
    store = _FakeStore([_sub("A"), _sub("B"), _sub("C")])
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    ledger.mark_selected(["B"], 100.0)  # B benched before → lowest priority
    res = apply_rotation_slate(store, "r1", 2, ledger, now=200.0)
    assert res["applied"] and res["candidates"] == 3 and res["slots"] == 2
    assert set(res["selected"]) == {"sub_A", "sub_C"}
    assert res["skipped"] == ["sub_B"]
    assert "rotation" in store.rejected["sub_B"]
    assert "resubmit" in store.rejected["sub_B"]
    # selected advanced to now; skipped kept seniority for next round
    assert ledger.load() == {"A": 200.0, "C": 200.0, "B": 100.0}


def test_apply_uncontested_still_advances_ledger(tmp_path):
    store = _FakeStore([_sub("A")])
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    res = apply_rotation_slate(store, "r1", 3, ledger, now=50.0)
    assert res["skipped"] == [] and store.rejected == {}
    assert ledger.load() == {"A": 50.0}  # seniority reflects the actual bench


def test_apply_excludes_already_rejected_candidates(tmp_path):
    store = _FakeStore([
        _sub("A"),
        _sub("B", status="rejected"),  # screening fail — not a candidate
        _sub("C"),
    ])
    res = apply_rotation_slate(
        store, "r1", 2, RotationLedger(str(tmp_path / "rot.json")), now=1.0,
    )
    assert res["candidates"] == 2
    assert set(res["selected"]) == {"sub_A", "sub_C"}
    assert store.rejected == {}  # nothing to skip


def test_apply_disabled_when_slots_nonpositive(tmp_path):
    store = _FakeStore([_sub("A"), _sub("B")])
    res = apply_rotation_slate(
        store, "r1", 0, RotationLedger(str(tmp_path / "rot.json")),
    )
    assert res["applied"] is False and store.rejected == {}


# ── truncation-proofing (2026-07-07: 12 of 19 rejects landed, 10 scored on 3
#    slots — the reject sweep must survive slow/failing/cancelled notifies) ────


class _TokenStore(_FakeStore):
    """FakeStore with a private-token side table purged on (terminal) reject."""

    def __init__(self, subs, tokens=None):
        super().__init__(subs)
        self.tokens = dict(tokens or {})

    def get_repo_token(self, submission_id):
        return self.tokens.get(submission_id)

    def reject(self, submission_id, reason):
        self.tokens.pop(submission_id, None)
        super().reject(submission_id, reason)


def test_all_rejects_land_before_any_notify_runs(tmp_path):
    """A hanging notify (GitHub stall) must not delay a single reject: phase 1
    lands every reject synchronously; notify runs afterwards, off-path."""
    subs = [_sub(hk) for hk in "ABCDE"]
    store = _TokenStore(subs, tokens={s.submission_id: f"tok_{s.hotkey}" for s in subs})
    release = threading.Event()
    notified = []

    def notify(sub, reason, repo_token=None):
        notified.append((sub.submission_id, repo_token))
        assert release.wait(timeout=10)  # simulate a stalled GitHub POST

    res = apply_rotation_slate(
        store, "r1", 2, RotationLedger(str(tmp_path / "rot.json")),
        now=1.0, notify=notify,
    )
    # apply_rotation_slate returned while notify is still hanging — and every
    # reject already landed.
    assert sorted(store.rejected) == sorted(res["skipped"])
    assert len(store.rejected) == 3
    release.set()
    res["notify_thread"].join(timeout=10)
    assert not res["notify_thread"].is_alive()
    # Every skipped sub got its notification, with the token captured BEFORE
    # the reject purged it.
    assert sorted(notified) == sorted(
        (sid, f"tok_{sid.split('_')[1]}") for sid in res["skipped"]
    )
    assert all(sid not in store.tokens for sid in res["skipped"])  # still purged


def test_notify_exception_loses_no_reject(tmp_path):
    subs = [_sub(hk) for hk in "ABCD"]
    store = _TokenStore(subs)

    def notify(sub, reason, repo_token=None):
        raise RuntimeError("github 502")

    res = apply_rotation_slate(
        store, "r1", 1, RotationLedger(str(tmp_path / "rot.json")),
        now=1.0, notify=notify,
    )
    res["notify_thread"].join(timeout=10)
    assert sorted(store.rejected) == sorted(res["skipped"])
    assert len(store.rejected) == 3


def test_cancellation_mid_sweep_still_rejects_everything(tmp_path):
    """CancelledError escaping a store call mid-sweep (e.g. a future async
    store, or a timeout scope around the close step) must not abandon the tail:
    the remaining rejects land via the store-only recovery loop, THEN the
    cancellation propagates. The ledger must NOT advance (the bench-pickup belt
    relies on a truncated close leaving the pre-close ledger)."""

    class _CancelOnceStore(_TokenStore):
        def __init__(self, subs, cancel_on):
            super().__init__(subs)
            self.cancel_on = cancel_on
            self.cancelled_once = False

        def reject(self, submission_id, reason):
            if submission_id == self.cancel_on and not self.cancelled_once:
                self.cancelled_once = True
                raise asyncio.CancelledError()
            super().reject(submission_id, reason)

    subs = [_sub(hk) for hk in "ABCDEF"]
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    # Find which sub rotation will skip SECOND, to cancel mid-sweep.
    _, skipped_preview = select_rotation_slate(subs, 2, {}, "r1")
    store = _CancelOnceStore(subs, cancel_on=skipped_preview[1].submission_id)

    with pytest.raises(asyncio.CancelledError):
        apply_rotation_slate(store, "r1", 2, ledger, now=1.0)

    # Every skipped sub is rejected — including the one whose first attempt
    # raised and the tail behind it.
    assert sorted(store.rejected) == sorted(s.submission_id for s in skipped_preview)
    # mark_selected never ran: the ledger still reflects the pre-close state.
    assert ledger.load() == {}


def test_no_notify_thread_without_notify_or_skips(tmp_path):
    store = _FakeStore([_sub("A")])
    res = apply_rotation_slate(
        store, "r1", 2, RotationLedger(str(tmp_path / "rot.json")), now=1.0,
    )
    assert res["notify_thread"] is None


# ── the fairness contract ─────────────────────────────────────────────────────

def test_every_miner_benched_within_ceil_m_over_n_rounds(tmp_path):
    miners = [f"5M{i}" for i in range(7)]
    slots = 3
    ledger = RotationLedger(str(tmp_path / "rot.json"))
    benched: dict[str, int] = {}
    bound = math.ceil(len(miners) / slots)  # 3 rounds
    last_slate: set[str] = set()
    for rnd in range(bound):
        # every miner resubmits every round until selected (the client loop)
        store = _FakeStore([_sub(hk, sid=f"sub_{hk}_r{rnd}") for hk in miners])
        res = apply_rotation_slate(store, f"round-{rnd}", slots, ledger, now=float(rnd + 1))
        last_slate = {sid.split("_")[1] for sid in res["selected"]}
        for hk in last_slate:
            benched.setdefault(hk, rnd)
    assert set(benched) == set(miners), f"not all benched in {bound} rounds: {benched}"
    # and the rotation keeps cycling: the most recent slate is at the BACK of
    # the queue, so the next round can never re-select any of its members while
    # older miners are contending
    store = _FakeStore([_sub(hk, sid=f"sub_{hk}_next") for hk in miners])
    res = apply_rotation_slate(store, "round-next", slots, ledger, now=99.0)
    assert not last_slate & {sid.split("_")[1] for sid in res["selected"]}
