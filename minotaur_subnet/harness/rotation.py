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
import time
from typing import Any

logger = logging.getLogger(__name__)

# Statuses OUT of the running for slate selection: already-rejected submissions
# (screening fail etc.) don't occupy a slot; ADOPTED can't occur pre-close.
_TERMINAL_STATUSES = ("rejected", "adopted")


def _status_value(sub: Any) -> str:
    st = getattr(sub, "status", None)
    return str(getattr(st, "value", None) or st or "")


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


def apply_rotation_slate(
    sub_store: Any,
    round_id: str,
    slots: int,
    ledger: RotationLedger,
    now: float | None = None,
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
    """
    if slots <= 0:
        return {"applied": False, "reason": "rotation disabled (slots <= 0)"}
    subs = sub_store.list_by_round(round_id)
    candidates = [s for s in subs if _status_value(s) not in _TERMINAL_STATUSES]
    selected, skipped = select_rotation_slate(
        candidates, slots, ledger.load(), round_id,
    )
    for sub in skipped:
        try:
            sub_store.reject(
                sub.submission_id,
                (
                    f"not selected for {round_id} (rotation: "
                    f"{len(candidates)} candidates, {slots} slots) — resubmit "
                    f"next round; miners benched longest ago go first"
                ),
            )
        except Exception:
            logger.warning(
                "rotation reject failed for %s (ignored)",
                getattr(sub, "submission_id", "?"), exc_info=True,
            )
    if selected:
        ledger.mark_selected(
            [getattr(s, "hotkey", "") or "" for s in selected],
            time.time() if now is None else now,
        )
    return {
        "applied": True,
        "candidates": len(candidates),
        "slots": slots,
        "selected": [getattr(s, "submission_id", "?") for s in selected],
        "skipped": [getattr(s, "submission_id", "?") for s in skipped],
    }
