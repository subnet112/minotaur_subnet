#!/usr/bin/env python3
"""Backfill a submission's factorization metric (max_region_nodes) — Phase-2 lever.

The saturated-tie factorization dethrone (relative_scoring.FACTOR_MARGIN) is
double-gated: the ARMED code constant (FACTOR_MARGIN int, fleet-wide promotion)
AND measured metrics on BOTH sides (factor_delta_between returns 0 while either
is None). Submissions screened after Phase 0 carry the metric automatically;
the STANDING champion — adopted before the metric existed and never re-screened
— does not. Backfilling ITS value supplies the champion side of the delta.

ORDERING (all three, in order, before a factor-tie dethrone can certify):
  1. Phase-2 code fleet-wide (main/:stable) — never leader-only.
  2. This backfill on the LEADER's store.
  3. PROPAGATE: POST /v1/solver/champion/reattest (champion force-sync). This
     step is REQUIRED at quorum > 1: this script writes only the leader's local
     store, and no automatic path re-delivers an OLD round's record to
     followers (close snapshots are scoped to the closing round; the pull
     reconcile heals only on champion-identity divergence). The reattest
     force-close payload is rebuilt fresh from the leader's store and
     force-upserted into every follower — without it, followers keep None,
     compute factor_delta 0, vote REJECT on a leader-adopted factor tie, and
     the round aborts at the certification deadline.

The operator clones the champion's PINNED commit themselves (auditable — the
same tree the fleet adopted, e.g. canonical main for a published champion) and
points this at the clone; the script verifies the clone's HEAD matches the
submission's pinned commit_hash, computes the metric with the IDENTICAL code
screening uses, and persists it via the store (cross-process safe — the store
serializes writers via its advisory file lock).

Usage (inside the api container, so the store resolves to the live /data):
    docker exec production-api-1 python scripts/backfill_factor_metric.py \
        --submission-id sub_abc123 --repo-dir /tmp/champion-clone
    # inspect without writing:
    ... --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _clone_head(repo_dir: Path) -> str | None:
    """The clone's HEAD commit, or None when repo_dir is not a git tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = out.stdout.strip()
    return head if out.returncode == 0 and len(head) == 40 else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute + persist max_region_nodes for an existing submission."
    )
    parser.add_argument("--submission-id", required=True, help="e.g. sub_abc123")
    parser.add_argument(
        "--repo-dir", required=True,
        help="Local clone of the submission's PINNED commit (operator-verified)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print; do not persist",
    )
    parser.add_argument(
        "--skip-commit-check", action="store_true",
        help="Skip the clone-HEAD == submission commit_hash verification "
             "(only for non-git trees; you are asserting the tree is the pinned one)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an EXISTING max_region_nodes (default: refuse — a "
             "screening-computed value should not be silently clobbered)",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir)
    if not repo_dir.is_dir():
        print(f"error: --repo-dir {repo_dir} is not a directory", file=sys.stderr)
        return 2

    from minotaur_subnet.api.routes.submissions.state import get_store
    from minotaur_subnet.harness.screening import FLOOR_VERSION, max_region_nodes

    store = get_store()
    sub = store.get(args.submission_id)
    if sub is None:
        print(f"error: submission {args.submission_id} not found in store", file=sys.stderr)
        return 2

    # The metric must be computed over the EXACT tree the fleet adopted — verify
    # the clone's HEAD against the submission's pinned commit.
    if not args.skip_commit_check:
        head = _clone_head(repo_dir)
        if head is None:
            print(
                f"error: {repo_dir} is not a git clone (cannot verify HEAD against "
                f"pinned commit {sub.commit_hash[:12]}); pass --skip-commit-check "
                f"only if you are certain this tree is that commit",
                file=sys.stderr,
            )
            return 2
        if head.lower() != sub.commit_hash.lower():
            print(
                f"error: clone HEAD {head[:12]} != submission pinned commit "
                f"{sub.commit_hash[:12]} — wrong tree",
                file=sys.stderr,
            )
            return 2

    if sub.max_region_nodes is not None and not args.force:
        print(
            f"error: {args.submission_id} already carries max_region_nodes="
            f"{sub.max_region_nodes} (screening-computed?); pass --force to overwrite",
            file=sys.stderr,
        )
        return 2

    value = max_region_nodes(str(repo_dir))
    print(
        f"{args.submission_id}: max_region_nodes={value} (floor_version={FLOOR_VERSION}) "
        f"commit={sub.commit_hash[:12]} current={sub.max_region_nodes}"
    )
    if value <= 0:
        # An empty/unparseable tree almost certainly means the wrong --repo-dir;
        # persisting 0 would hand the champion an unbeatable factor bar.
        print("error: metric is 0 (no parseable in-tree Python) — refusing to persist", file=sys.stderr)
        return 2
    if args.dry_run:
        print("dry-run: not persisted")
        return 0

    store.set_max_region_nodes(args.submission_id, value)
    print("persisted")
    print(
        "\nNEXT STEP (REQUIRED at quorum > 1): propagate to followers —\n"
        "  POST /v1/solver/champion/reattest\n"
        "This value exists ONLY in the leader's store until the reattest "
        "force-sync mirrors the record fleet-wide; without it, followers vote "
        "REJECT on a factor-tie dethrone and the round aborts."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
