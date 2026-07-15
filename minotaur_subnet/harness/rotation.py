"""Round-entry rotation: fair slate selection across rounds (LRU, not first-come).

A round benches at most ``SOLVER_ROUND_MAX_SUBMISSIONS`` submissions, but with
rotation the intake no longer turns miners away once that many have arrived
(the old first-come behaviour). Every submission accepted during the OPEN
window is a slate CANDIDATE; at close the leader selects the miners that were
benchmarked LONGEST AGO (never-benched first) and rejects the overflow with an
explicit resubmit-next-round reason. A skipped miner's rotation seniority keeps
growing, so selection is starvation-free by construction: with M contending
miners and N slots, every miner is benched at least once every ceil(M/N) rounds.

Ties (equal seniority — e.g. two never-benched miners) break by
``sha256(hotkey:round_id)``, so the order reshuffles every round and anyone can
recompute it from public data — no alphabetical or arrival-time advantage.

The ledger is LEADER-LOCAL operator state, the same category as the intake caps
(admission control, not a fleet-consensus parameter): followers simply mirror
whatever slate the leader closed with via the close broadcast's submission
snapshot. Losing the ledger degrades gracefully — everyone ties at
"never benched" and the salted-hash shuffle decides until history rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Statuses OUT of the running for slate selection: already-rejected submissions
# (screening fail etc.) don't occupy a slot; ADOPTED can't occur pre-close;
# WAITLISTED is a prior rotation pass's decision (re-running must not re-process
# it).
_TERMINAL_STATUSES = ("rejected", "adopted", "waitlisted")


def _status_value(sub: Any) -> str:
    st = getattr(sub, "status", None)
    return str(getattr(st, "value", None) or st or "")


def benchable_candidate_count(subs: Iterable[Any]) -> int:
    """How many of ``subs`` a rotation pass would consider — i.e. how many get
    BENCHED when rotation is disabled (``slots <= 0``) or fails.

    Shares ``_TERMINAL_STATUSES`` with :func:`apply_rotation_slate` on purpose:
    the decision-window autoscale used to keep its OWN copy of this rule as
    "status != rejected", and when #620 parked rotation's overflow in
    ``waitlisted`` instead of ``rejected`` the two silently diverged — never-benched
    submissions inflated the window until activation outlived the champion
    approval and certify() reverted "Expired". One definition, one place.
    """
    return len([s for s in subs if _status_value(s) not in _TERMINAL_STATUSES])


def rotation_ledger_path() -> str:
    """Path of the leader-local rotation ledger.

    ``SOLVER_ROTATION_LEDGER_PATH`` wins; otherwise the ledger lives next to
    the round store (``SOLVER_ROUND_STORE_PATH``) so it lands on the same
    persistent volume (/data in production, per #430). Shared by the api's
    close-time rotation and the benchmark worker's slate-width belt so both
    read the SAME ledger.
    """
    explicit = os.environ.get("SOLVER_ROTATION_LEDGER_PATH", "").strip()
    if explicit:
        return explicit
    round_store_path = os.environ.get("SOLVER_ROUND_STORE_PATH", "").strip()
    base = os.path.dirname(round_store_path) if round_store_path else "."
    return os.path.join(base or ".", "solver_rotation.json")


def rotation_sort_key(
    hotkey: str, round_id: str, last_selected: dict[str, float],
) -> tuple[float, str]:
    """(seniority, salted tie-break) — lower sorts first, i.e. gets a slot.

    Seniority is the hotkey's last-selected timestamp (0.0 = never benched, so
    newcomers outrank everyone). The tie-break salts the hotkey with the round
    id so equal-seniority order is deterministic + publicly recomputable but
    reshuffles every round.
    """
    return (
        float(last_selected.get(hotkey, 0.0)),
        hashlib.sha256(f"{hotkey}:{round_id}".encode()).hexdigest(),
    )


def select_rotation_slate(
    candidates: list[Any],
    slots: int,
    last_selected: dict[str, float],
    round_id: str,
) -> tuple[list[Any], list[Any]]:
    """PURE: split candidates into (selected, skipped) by rotation order."""
    slots = max(0, int(slots))
    ordered = sorted(
        candidates,
        key=lambda s: rotation_sort_key(
            getattr(s, "hotkey", "") or "", round_id, last_selected,
        ),
    )
    return ordered[:slots], ordered[slots:]


class RotationLedger:
    """``{hotkey: last_selected_unix_ts}`` with atomic JSON persistence.

    Single-writer (the leader's round coordinator); best-effort by design — a
    lost write only delays fairness by one round, never corrupts a round.
    """

    def __init__(self, path: str) -> None:
        self._path = str(path)

    def load(self) -> dict[str, float]:
        try:
            with open(self._path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning(
                "rotation ledger unreadable (%s) — treating all miners as never-benched",
                self._path, exc_info=True,
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def mark_selected(self, hotkeys: list[str], ts: float) -> None:
        data = self.load()
        for hk in hotkeys:
            if hk:
                data[hk] = float(ts)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(self._path) or ".", prefix=".rotation-",
            )
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
            tmp = None
        except Exception:
            logger.warning("rotation ledger write failed (%s)", self._path, exc_info=True)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _notify_skipped_in_background(
    notify: Any,
    items: list[tuple[Any, str | None]],
    reason: str,
    round_id: str,
) -> threading.Thread:
    """Fire the per-submission not-selected notifications OFF the close path.

    Each ``notify(sub, reason, repo_token)`` posts a GitHub PR comment —
    seconds of blocking network per private submission. Run serially inline
    (the pre-fix behaviour), a 20-candidate round freezes the event loop for a
    minute+: /health goes dark, uvicorn's SIGTERM handler can never run, and a
    container stop escalates to SIGKILL mid-close (observed 2026-07-07,
    round-e29724243-n1). A daemon thread keeps the feedback best-effort without
    holding the round close hostage; a crash mid-thread only loses PR comments,
    never rejects (those already landed in phase 1).
    """
    def _run() -> None:
        posted = 0
        for sub, token in items:
            try:
                notify(sub, reason, token)
                posted += 1
            except Exception:
                logger.warning(
                    "rotation notify failed for %s (ignored)",
                    getattr(sub, "submission_id", "?"), exc_info=True,
                )
        logger.info(
            "rotation notify for %s: %d/%d not-selected comments attempted",
            round_id, posted, len(items),
        )

    thread = threading.Thread(
        target=_run, name=f"rotation-notify-{round_id}", daemon=True,
    )
    thread.start()
    return thread


def apply_rotation_slate(
    sub_store: Any,
    round_id: str,
    slots: int,
    ledger: RotationLedger,
    now: float | None = None,
    notify: Any = None,
) -> dict[str, Any]:
    """Select the round's benched slate by rotation and reject the overflow.

    Runs on the LEADER at round close, before the close snapshot is built, so
    followers mirror the post-rotation submission set. Rotation applies to ALL
    live submissions regardless of screening/benchmark progress — fairness must
    not depend on who screened fastest, or the arrival race the rotation exists
    to remove comes straight back through the side door.

    ``slots <= 0`` disables rotation (matches the cap's 0-=-unlimited
    convention). Selected miners' ledger entries advance even when the round is
    uncontested, so seniority always reflects the last actual bench.

    TRUNCATION-PROOF DESIGN (two phases). The old shape interleaved a slow
    network call per skipped submission (notify → GitHub PR comment, seconds
    each) with its store reject; killing the process mid-sweep abandoned the
    tail of the rejects and the un-rejected survivors were benched, busting the
    slate width (2026-07-07: 12 of 19 rejects landed, 10 scored on 3 slots).
    Now:

      Phase 1 — REJECT (fast, local-only, no network): for every skipped
      submission, capture its private-repo token (reject purges it, and the PR
      comment needs it), then ``store.reject``. Per-submission failures are
      contained; a ``CancelledError``/``BaseException`` mid-sweep still lands
      every remaining reject via a tight store-only loop before re-raising.

      Phase 2 — NOTIFY (best-effort, background thread): post the
      not-selected PR comments with the tokens retained from phase 1, off the
      close path, so the event loop is never blocked and a slow/failing GitHub
      never delays or truncates anything.

    ``notify`` (optional) is called as ``notify(submission, reason,
    repo_token)`` where ``repo_token`` is the token captured BEFORE the
    terminal reject purged it (None for public submissions).
    """
    if slots <= 0:
        return {"applied": False, "reason": "rotation disabled (slots <= 0)"}
    subs = sub_store.list_by_round(round_id)
    candidates = [s for s in subs if _status_value(s) not in _TERMINAL_STATUSES]
    selected, skipped = select_rotation_slate(
        candidates, slots, ledger.load(), round_id,
    )
    reject_reason = (
        f"not selected for {round_id} (rotation: "
        f"{len(candidates)} candidates, {slots} slots) — resubmit "
        f"next round; miners benched longest ago go first"
    )
    n_skipped = len(skipped)

    # skipped is in seniority order (best next-round priority FIRST), so the
    # 1-based index is the waitlist position. WAITLIST (not reject): not being
    # selected is a no-fault outcome that keeps next-round seniority. Falls back
    # to reject for stores without the method (older/test doubles).
    _waitlist = getattr(sub_store, "waitlist", None)

    def _park(sub: Any, position: int) -> None:
        if callable(_waitlist):
            _waitlist(
                sub.submission_id, reject_reason,
                outcome_code="rotation_not_selected",
                position=position, contenders=len(candidates), slots=slots,
            )
        else:
            sub_store.reject(sub.submission_id, reject_reason)

    # ── Phase 1: park every skipped submission (fast, no network) ────────────
    to_notify: list[tuple[Any, str | None]] = []
    get_token = getattr(sub_store, "get_repo_token", None)
    done = 0
    try:
        for idx, sub in enumerate(skipped):
            token = None
            if notify is not None and callable(get_token):
                try:
                    # Captured BEFORE the terminal transition purges the private
                    # token, which the phase-2 PR comment needs.
                    token = get_token(sub.submission_id)
                except Exception:
                    logger.warning(
                        "rotation token capture failed for %s (comment may "
                        "not post; waitlist unaffected)",
                        getattr(sub, "submission_id", "?"), exc_info=True,
                    )
            try:
                _park(sub, idx + 1)
            except Exception:
                logger.warning(
                    "rotation waitlist failed for %s (ignored)",
                    getattr(sub, "submission_id", "?"), exc_info=True,
                )
            done += 1
            if notify is not None:
                to_notify.append((sub, token))
    except BaseException:
        # Cancellation / interpreter teardown mid-sweep: parking the skipped set
        # is the round's INTEGRITY (an un-parked survivor gets benched and busts
        # the slate width) — finish the rest with a tight store-only loop (no
        # token capture, no notify bookkeeping) before re-raising.
        for idx in range(done, n_skipped):
            sub = skipped[idx]
            try:
                _park(sub, idx + 1)
            except BaseException:  # noqa: BLE001 — best-effort cleanup path
                pass
        raise
    if selected:
        ledger.mark_selected(
            [getattr(s, "hotkey", "") or "" for s in selected],
            time.time() if now is None else now,
        )
    # ── Phase 2: best-effort miner feedback, off the close path ─────────────
    notify_thread = (
        _notify_skipped_in_background(notify, to_notify, reject_reason, round_id)
        if (notify is not None and to_notify)
        else None
    )
    return {
        "applied": True,
        "candidates": len(candidates),
        "slots": slots,
        "selected": [getattr(s, "submission_id", "?") for s in selected],
        "skipped": [getattr(s, "submission_id", "?") for s in skipped],
        "notify_thread": notify_thread,
    }
