"""Submission feedback report.

Turns a benchmarked submission into an actionable, PER-ORDER report so a miner
knows exactly where they stand and where to improve: the same-pin ``relative``
block — which orders they matched, beat, or lost against the champion, and by
how much (per-order output ratio) — plus the adoption verdict.

The legacy aggregate-vs-champion SCALARS (``your_score`` / ``champion_score`` /
``score_to_beat`` / ``gap`` / ``dethrone_margin``) were REMOVED. Under raw-output
scoring the JS ``score`` is a saturated [0,1] validity sentinel, so an aggregate
scalar is meaningless — worse, it could CONTRADICT the authoritative per-order
verdict (a positive ``gap`` on a submission the relative rule rejects), which
misled miners about why they weren't adopted. The relative rule is the sole
adoption path, so the per-order block is the sole, honest signal.

Per-order detail is the stored ``benchmark_details["relative"]`` block alone:
computed at round evaluation against the champion re-benched at the SAME
fork-pin. The earlier cross-fork per-order surfaces (``per_case`` head-to-head
and observe-only ``shadow_relative``) compared this submission's frozen results
against the champion's LATER, different-pin bench — fabricating phantom drops —
and have been removed.

Pure assembly from data already on ``Submission.benchmark_details`` plus the
abort reason. No new benchmark compute — a read + shape job, cheap on the status
endpoint.
"""

from __future__ import annotations

from typing import Any

# GitHub rejects issue-comment bodies over 65536 chars; only truncate if a
# (very large) report would actually exceed it.
_GH_COMMENT_MAX_CHARS = 65000

# Cap the rendered per-order table so a huge benchmark pack can't blow the GH
# comment limit; the machine-readable ``relative.per_order`` on the status
# endpoint always carries the full list.
_MAX_PER_ORDER_ROWS = 60


def _status_value(sub: Any) -> str:
    st = getattr(sub, "status", None)
    return getattr(st, "value", None) or str(st or "")


def build_submission_report(
    sub: Any,
    *,
    reason: str | None,
    won: bool = False,
) -> dict[str, Any] | None:
    """Assemble the per-order feedback report, or None if nothing to report yet.

    Returns None while the submission is still queued/screening/benchmarking.
    The report is the same-pin per-order ``relative`` block plus a verdict-derived
    ``outcome`` — no aggregate scalars (see module docstring). Defensive:
    tolerates missing fields.
    """
    status = _status_value(sub)
    details = getattr(sub, "benchmark_details", None) or {}
    screening = getattr(sub, "screening", None) or {}

    benchmarked = bool(details)
    rejected_in_screening = status == "rejected" and not benchmarked
    if not benchmarked and not rejected_in_screening:
        return None  # queued / screening / benchmarking — no report yet

    # SAME-PIN per-order counts, READ from this submission's OWN persisted
    # ``benchmark_details["relative"]`` (computed at round evaluation against the
    # champion re-benched in this submission's round at the SAME fork-pin — see
    # ``EpochManager._persist_round_relative_counts``). We deliberately do NOT
    # recompute against the champion's LATEST details: that record was re-benched
    # in a different, later round at a different Base block, so a cross-fork
    # recompute fabricates wins/regressions from ETH price drift. Absent (benched
    # before this shipped, or a non-leader that never evaluated the round) → the
    # block is omitted (pending), never a cross-fork fallback.
    stored_rel = details.get("relative") if isinstance(details, dict) else None
    rel = stored_rel if isinstance(stored_rel, dict) else None

    # ── outcome: derived from the PER-ORDER verdict, never a scalar ──
    if status == "adopted":
        outcome = "adopted"
    elif rejected_in_screening:
        outcome = "rejected_screening"
    elif details.get("errors") and not details.get("plans_generated"):
        # No plans at all is the root cause — report it ahead of anything else.
        outcome = "benchmark_failed"
    elif rel is not None:
        verdict = rel.get("verdict")
        if verdict in ("dethrone", "better"):
            outcome = "beat_champion"       # better on >=1 order (may not be finalist)
        elif rel.get("worse", 0):
            outcome = "regressed"           # lost ground on >=1 order
        else:
            outcome = "matched"             # tied the champion, no order better
    else:
        outcome = status or "scored"

    if won:
        # Leader selected this as the round finalist (beat the champion on the
        # per-order rule). Override so the PR renders a win.
        outcome = "won"

    report: dict[str, Any] = {
        "outcome": outcome,
        "reason": reason,
        "scoring_mode": "relative",
    }

    # Factorization metric (Phase 0, OBSERVE-ONLY) — surfaced next to the per-order
    # relative stats so the frontend can show it. This submission's OWN
    # max_region_nodes (a golf-immune worst-entanglement proxy); champion
    # comparison arrives with the Phase-2 backfill. Measured, not gated.
    factor = getattr(sub, "max_region_nodes", None)
    if factor is not None:
        try:
            from minotaur_subnet.harness.screening import FLOOR_VERSION

            floor_version: int | None = FLOOR_VERSION
        except Exception:  # additive surface — never break the report
            floor_version = None
        report["factorization"] = {
            "max_region_nodes": factor,
            "floor_version": floor_version,
            "observe_only": True,
        }

    if rel is not None:
        report["relative"] = rel
        try:
            from minotaur_subnet.epoch.relative_scoring import relative_reason

            rel_reason = relative_reason(
                rel, candidate_id=getattr(sub, "submission_id", None),
            )
            if rel_reason:
                report["reason_relative"] = rel_reason
        except Exception:  # additive surface — must never break the report
            pass

    if rejected_in_screening:
        report["screening"] = screening
    return report


# ── Markdown rendering (for the miner's PR comment) ──────────────────────────


def _cell(value: Any) -> str:
    """Escape a value for a markdown table cell (pipes, newlines)."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _pct(ratio: Any) -> str:
    """Render a per-order output ratio as a signed % delta vs the champion.

    ``ratio`` is challenger_output / champion_output, so ``(ratio - 1) * 100`` is
    how much more (+) or less (-) output the miner delivered on that order."""
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        return f"{(ratio - 1.0) * 100:+.2f}%"
    return "—"


def render_report_md(report: dict[str, Any] | None, *, submission_id: str | None = None) -> str:
    """Render a :func:`build_submission_report` dict into a GitHub-flavored
    markdown comment for the miner's PR: the same-pin per-order ``relative``
    summary plus the specific orders that differ from the champion — the only
    thing that tells a miner where to improve. No aggregate scalars.

    Pure formatting, tolerant of partial/empty reports. Returns ``""`` when there
    is nothing to render. Truncates only if the body would exceed GitHub's
    issue-comment size limit.
    """
    if not report:
        return ""
    outcome = report.get("outcome") or "scored"
    reason = report.get("reason")

    _headers = {
        "adopted": "### ✅ Adopted as champion",
        "won": "### 🏆 Beat the champion — selected as finalist",
        "beat_champion": "### 🥈 Beat the champion on >=1 order (not the finalist)",
        "matched": "### ➖ Matched the champion — no order improved",
        "regressed": "### ❌ Regressed vs the champion on >=1 order",
        "rejected_screening": "### ❌ Screening rejected",
        "benchmark_failed": "### ❌ Benchmark produced no plans",
    }
    header = _headers.get(outcome, "### ❌ Submission rejected")
    if reason and outcome not in ("adopted", "won"):
        header += f" — {reason}"
    lines = [header, ""]

    rel = report.get("relative")
    if isinstance(rel, dict):
        seg = (
            f"**Per-order vs champion (same-pin):** {rel.get('better', 0)} better · "
            f"{rel.get('worse', 0)} worse · {rel.get('matched', 0)} matched · "
            f"{rel.get('new', 0)} new"
        )
        if rel.get("repeats"):
            # Armed blind-spot REPEAT guard: covers that only re-delivered the
            # incumbent's adoption-time value — counted in matched, called out
            # so the miner knows the cover earned nothing and why.
            seg += f" · {rel['repeats']} repeat (not credited)"
        if rel.get("verdict"):
            seg += f" — _{_cell(rel['verdict'])}_"
        lines += [seg, ""]

        # The actionable part: the specific orders that DIFFER from the champion.
        # Worse rows first (that's where to focus optimization), then wins.
        # ALLOWLIST the diverging verdicts: matched orders are summarized by the
        # counts above (listing hundreds of ties helps no one), and ``skip`` rows
        # (neither side delivered) carry no signal — rendered, they read as
        # phantom ✅ wins with no delta. ``dropped`` (the challenger produced
        # nothing on a champion-served order) is a hard veto, so it counts as
        # worse: ❌, sorted with the regressions.
        per_order = rel.get("per_order")
        _worse = {"worse", "regression", "dropped"}
        _better = {"better", "win", "new", "blind_spot_cover"}
        # blind_spot_repeat (armed guard): delivered on a champion-blind order
        # but did NOT exceed the incumbent's adoption-time value — neutral, and
        # the single most actionable row for the miner, so render it.
        _neutral = {"blind_spot_repeat"}
        diffs = (
            [
                o for o in per_order
                if isinstance(o, dict) and o.get("verdict") in _worse | _better | _neutral
            ]
            if isinstance(per_order, list)
            else []
        )
        diffs.sort(key=lambda o: 0 if o.get("verdict") in _worse else 1)
        if diffs:
            lines += [
                "**Orders that differ from the champion** — optimize the ❌ rows:",
                "",
                "| Order | Δ output vs champion | |",
                "|---|---|---|",
            ]
            for o in diffs[:_MAX_PER_ORDER_ROWS]:
                verdict = o.get("verdict")
                if verdict == "dropped":
                    mark = "❌ dropped"  # no plan on a champion-served order (hard veto)
                elif verdict in ("new", "blind_spot_cover"):
                    mark = "✅ new"  # covered an order the champion delivers nothing on
                elif verdict == "blind_spot_repeat":
                    # Neutral: must EXCEED the incumbent's adoption-time value
                    # on this order to earn cover credit.
                    mark = "➖ repeat (beat the recorded value to credit)"
                else:
                    mark = "❌" if verdict in _worse else "✅"
                lines.append(
                    f"| `{_cell(o.get('intent_id'))}` | {_pct(o.get('ratio'))} | {mark} |"
                )
            if len(diffs) > _MAX_PER_ORDER_ROWS:
                lines.append(f"| _…and {len(diffs) - _MAX_PER_ORDER_ROWS} more_ | | |")
            lines.append("")
        elif rel.get("matched") and not rel.get("better") and not rel.get("worse"):
            n = rel.get("compared") or rel.get("matched")
            lines += [
                f"_Identical output to the champion on all {n} orders. To win you need a "
                f"strictly better route on at least one order — find pairs/sizes where a "
                f"different route returns more output._",
                "",
            ]
    else:
        lines += [
            "_Per-order detail is in the machine-readable `relative` block "
            "(same-pin) on the status endpoint — pending until the round is evaluated._",
            "",
        ]

    if outcome == "rejected_screening":
        scr = report.get("screening") or {}
        stages = ", ".join(
            f"{k}={'pass' if (v or {}).get('passed') else 'fail'}"
            for k, v in scr.items() if isinstance(v, dict)
        )
        if stages:
            lines += [f"**Screening:** {stages}", ""]

    if submission_id:
        lines.append(
            f"<sub>Machine-readable per-order detail: "
            f"`GET /v1/submissions/{submission_id}/status`</sub>"
        )

    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > _GH_COMMENT_MAX_CHARS:
        keep = body[: _GH_COMMENT_MAX_CHARS - 200]
        keep = keep[: keep.rfind("\n")]
        body = keep + (
            "\n\n_…table truncated at GitHub's comment size limit; full detail via "
            "the status endpoint._\n"
        )
    return body
