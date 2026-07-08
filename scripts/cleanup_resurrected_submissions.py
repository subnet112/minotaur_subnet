#!/usr/bin/env python3
"""One-shot cleanup: re-terminate rotation-rejected submissions that a racing
benchmark resurrected to SCORED (the pre-#596 poison).

Before #596, ``set_benchmark_result`` flipped a terminally REJECTED submission
back to SCORED when its (in-flight at close, or restart-orphaned) bench
completed after the rotation reject. The resurrected record kept its rotation
rejection_reason but ranked as scored — and under the tie-break ladder could
win as finalist and die at relayer-finalize "no token — FAIL-CLOSED". #596
made terminal-REJECTED immutable going forward; this script repairs the
records poisoned BEFORE that fix landed (~368 on the live leader as of
2026-07-07).

Target: status == scored AND rejection_reason contains "(rotation:" — i.e. the
close-time rotation terminally rejected it, yet it shows scored. The repair
sets status back to REJECTED via the store (cross-process safe: the store
serializes writers via its advisory file lock), PRESERVING the original
rejection_reason and the benchmark_details (the miner's report stays intact).

Never touched (fail-safe exclusions):
  - the currently adopted champion (submission-store ADOPTED record and/or the
    round store's active-champion snapshot);
  - any submission whose round is still in flight (not ACTIVATED/ABORTED) —
    the live round's evaluation must not have records flipped under it. A
    submission whose round is unknown to the round store is treated as an old,
    finished round (eligible).

Default is a DRY RUN (prints counts + ids). Pass --apply to persist.

Usage (inside the api container, so the store resolves to the live /data):
    docker exec production-api-1 python scripts/cleanup_resurrected_submissions.py
    docker exec production-api-1 python scripts/cleanup_resurrected_submissions.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

POISON_MARKER = "(rotation:"

# Round states that mean "this round is finished" — flipping one of its
# records can no longer change any in-flight evaluation/certification.
_TERMINAL_ROUND_STATUSES = ("activated", "aborted")


def _status_str(obj: Any) -> str:
    status = getattr(obj, "status", None)
    return str(getattr(status, "value", None) or status or "")


def find_poison(sub_store: Any, round_store: Any) -> tuple[list[Any], list[tuple[str, str]]]:
    """Return (eligible poison submissions, [(submission_id, skip_reason)])."""
    from minotaur_subnet.harness.submission_store import SubmissionStatus

    protected: set[str] = set()
    champion = None
    try:
        champion = sub_store.get_champion()
    except Exception:  # noqa: BLE001 — protection is best-effort widening
        pass
    if champion is not None:
        protected.add(str(getattr(champion, "submission_id", "") or ""))
    try:
        active = round_store.get_active_champion()
        active_id = getattr(active, "submission_id", None)
        if active_id:
            protected.add(str(active_id))
    except Exception:  # noqa: BLE001
        pass
    protected.discard("")

    eligible: list[Any] = []
    skipped: list[tuple[str, str]] = []
    for sub in sub_store.list_by_status(SubmissionStatus.SCORED):
        reason = getattr(sub, "rejection_reason", None) or ""
        if POISON_MARKER not in reason:
            continue
        sid = sub.submission_id
        if sid in protected:
            skipped.append((sid, "currently adopted champion"))
            continue
        round_id = getattr(sub, "round_id", None) or ""
        round_state = None
        try:
            round_state = round_store.get_round(round_id) if round_id else None
        except Exception:  # noqa: BLE001
            round_state = None
        if round_state is not None and _status_str(round_state) not in _TERMINAL_ROUND_STATUSES:
            skipped.append((sid, f"round {round_id} still in flight ({_status_str(round_state)})"))
            continue
        eligible.append(sub)
    return eligible, skipped


def run(sub_store: Any, round_store: Any, *, apply: bool) -> int:
    """Scan, print, and (with apply=True) re-terminate. Returns #flipped."""
    eligible, skipped = find_poison(sub_store, round_store)

    print(f"poison scan: {len(eligible)} eligible, {len(skipped)} excluded")
    for sid, why in skipped:
        print(f"  EXCLUDED {sid}: {why}")
    for sub in eligible:
        print(
            f"  {'FLIP' if apply else 'WOULD FLIP'} {sub.submission_id} "
            f"round={getattr(sub, 'round_id', '?')} "
            f"reason={(getattr(sub, 'rejection_reason', '') or '')[:80]!r}"
        )
    if not apply:
        print("dry-run: nothing persisted (pass --apply to fix)")
        return 0

    flipped = 0
    for sub in eligible:
        # store.reject re-terminates under the store's write lock, re-writing
        # the SAME reason (preserved) and leaving benchmark_details untouched;
        # the token purge inside is a no-op (already purged at the original
        # reject).
        try:
            sub_store.reject(sub.submission_id, sub.rejection_reason)
            flipped += 1
        except Exception as exc:  # noqa: BLE001 — keep sweeping
            print(f"  ERROR {sub.submission_id}: {exc}", file=sys.stderr)
    print(f"flipped {flipped}/{len(eligible)} back to rejected")
    return flipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-terminate rotation-rejected submissions resurrected to scored "
            "(pre-#596 poison). Dry-run by default."
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Persist the repair (default: dry-run listing only)",
    )
    args = parser.parse_args()

    from minotaur_subnet.api.routes.submissions.state import get_round_store, get_store

    run(get_store(), get_round_store(), apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
