"""Submission feedback report.

Turns a benchmarked submission into an actionable report so a miner knows where
they stand: the aggregate-vs-champion scalars, threshold pass/fail, and the
same-pin per-order ``relative`` count block (better/worse/matched/new + verdict).

Per-order detail is the stored ``benchmark_details["relative"]`` block alone:
it is computed at round evaluation against the champion re-benched at the SAME
fork-pin. The earlier cross-fork per-order surfaces (the ``per_case`` head-to-head
and the observe-only ``shadow_relative`` block) compared this submission's frozen
per-order results against the champion's LATER, different-pin bench — fabricating
phantom drops — and have been removed.

Pure assembly from data already on ``Submission.benchmark_details`` plus a few
scalars the caller looks up (champion score, threshold, dethrone margin, reason).
No new benchmark compute — this is a read + shape job, so it stays cheap on the
status endpoint.
"""

from __future__ import annotations

from typing import Any

# GitHub rejects issue-comment bodies over 65536 chars; only truncate if a
# (very large) report would actually exceed it.
_GH_COMMENT_MAX_CHARS = 65000


def _status_value(sub: Any) -> str:
    st = getattr(sub, "status", None)
    return getattr(st, "value", None) or str(st or "")


def build_submission_report(
    sub: Any,
    *,
    champion_score: float | None,
    threshold: float,
    dethrone_margin: float,
    reason: str | None,
    won: bool = False,
) -> dict[str, Any] | None:
    """Assemble the feedback report, or None if the submission isn't far enough.

    Returns None while the submission is still queued/screening/benchmarking
    (nothing to report yet). Defensive: tolerates missing fields. Per-order
    detail is carried by the same-pin ``relative`` block, read from this
    submission's own persisted ``benchmark_details["relative"]``.
    """
    status = _status_value(sub)
    details = getattr(sub, "benchmark_details", None) or {}
    your_score = getattr(sub, "benchmark_score", None)
    screening = getattr(sub, "screening", None) or {}

    benchmarked = bool(details) or your_score is not None
    rejected_in_screening = status == "rejected" and not benchmarked
    if not benchmarked and not rejected_in_screening:
        return None  # queued / screening / benchmarking — no report yet

    # ── aggregate vs champion ──
    score_to_beat = (
        champion_score * (1.0 + dethrone_margin) if champion_score is not None else None
    )
    gap = (
        your_score - score_to_beat
        if (your_score is not None and score_to_beat is not None)
        else None
    )
    aggregate = {
        "your_score": your_score,
        "champion_score": champion_score,
        "threshold": threshold,           # min-score floor; per-case pass uses the same
        "dethrone_margin": dethrone_margin,
        "score_to_beat": round(score_to_beat, 6) if score_to_beat is not None else None,
        "gap": round(gap, 6) if gap is not None else None,
    }

    # ── outcome ──
    if status == "adopted":
        outcome = "adopted"
    elif rejected_in_screening:
        outcome = "rejected_screening"
    elif details.get("errors") and not details.get("plans_generated"):
        # No plans at all is the root cause — report it ahead of the
        # (consequent) sub-threshold score.
        outcome = "benchmark_failed"
    elif your_score is not None and your_score < threshold:
        outcome = "rejected_threshold"
    elif (
        score_to_beat is not None
        and your_score is not None
        and your_score < score_to_beat
    ):
        outcome = "scored_not_adopted"
    else:
        outcome = status or "scored"

    if won:
        # Leader selected this as the round finalist (it beat the champion by the
        # dethrone margin). Override any score-derived outcome so the PR renders a win.
        outcome = "won"

    report: dict[str, Any] = {
        "outcome": outcome,
        "reason": reason,
        "aggregate": aggregate,
    }

    # ── AUTHORITATIVE relative counts (the sole per-order surface) ──
    # The relative rule is the sole adoption path, so ALWAYS surface the COUNT shape
    # (better/worse/matched/new + verdict) that replaces the saturated score, and
    # set ``scoring_mode`` to "relative". The only legacy scalars kept are the
    # aggregate ``your_score``/``champion_score`` (head-to-head scalar, additive);
    # the cross-fork ``per_case`` + ``shadow_relative`` per-order surfaces were
    # removed because they compared this submission's frozen per-order results
    # against the champion's LATER, different-pin bench (fabricated phantom drops).
    #
    # SAME-PIN: the counts are READ from this submission's OWN persisted
    # ``benchmark_details["relative"]`` — computed at round evaluation against the
    # champion re-benched in this submission's round at the SAME fork-pin (see
    # ``EpochManager._persist_round_relative_counts``). We deliberately do NOT
    # recompute here against the champion's LATEST ``benchmark_details``: that
    # champion record was re-benched in a DIFFERENT (later) round at a different
    # Base block, so a cross-fork recompute fabricates wins/regressions from ETH
    # price drift. When no stored block exists (benched before this shipped, no
    # shadow rows, or a non-leader that never evaluated the round) the relative
    # block is omitted (pending) — never a cross-fork fallback.
    try:
        from minotaur_subnet.epoch.relative_scoring import (
            relative_reason,
        )

        report["scoring_mode"] = "relative"
        stored_rel = details.get("relative") if isinstance(details, dict) else None
        if isinstance(stored_rel, dict):
            report["relative"] = stored_rel
            rel_reason = relative_reason(
                stored_rel, candidate_id=getattr(sub, "submission_id", None),
            )
            if rel_reason:
                report["reason_relative"] = rel_reason
    except Exception:  # additive surface — must never break the report
        pass

    if rejected_in_screening:
        report["screening"] = screening
    return report


# ── Markdown rendering (for the miner's PR comment) ──────────────────────────


def _num(v: Any, nd: int = 4) -> str:
    """Format a score, or an em-dash for missing values. Bools are not numbers."""
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "—"


def _cell(value: Any) -> str:
    """Escape a value for a markdown table cell (pipes, newlines)."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def render_report_md(report: dict[str, Any] | None, *, submission_id: str | None = None) -> str:
    """Render a :func:`build_submission_report` dict into a GitHub-flavored
    markdown comment for the miner's PR — the aggregate-vs-champion scalars plus
    the same-pin per-order ``relative`` count summary.

    Pure formatting, tolerant of partial/empty reports. Returns ``""`` when
    there is nothing to render. Truncates only if the body would exceed GitHub's
    issue-comment size limit.
    """
    if not report:
        return ""
    outcome = report.get("outcome") or "scored"
    reason = report.get("reason")
    agg = report.get("aggregate") or {}

    if outcome == "adopted":
        header = "### ✅ Adopted as champion"
    elif outcome == "won":
        header = "### 🏆 Beat the champion — selected as finalist"
    elif outcome == "rejected_screening":
        header = "### ❌ Screening rejected"
    else:
        header = "### ❌ Submission rejected"
    if reason and outcome not in ("adopted", "won"):
        header += f" — {reason}"
    lines = [header, ""]

    # Aggregate vs champion.
    your = agg.get("your_score")
    if your is not None or agg.get("champion_score") is not None:
        seg = [f"**Your score:** {_num(your)}"]
        if agg.get("champion_score") is not None:
            seg.append(f"**Champion:** {_num(agg.get('champion_score'))}")
        if agg.get("score_to_beat") is not None:
            seg.append(f"**To beat:** {_num(agg.get('score_to_beat'))}")
        if agg.get("gap") is not None:
            g = agg.get("gap")
            seg.append(f"**Gap:** {'+' if g >= 0 else ''}{_num(g)}")
        lines += [" · ".join(seg), ""]

    # Per-order detail is the same-pin ``relative`` count block (machine-readable
    # via the status endpoint). The earlier cross-fork per-case table was removed:
    # it compared this submission's frozen per-order results against the champion's
    # LATER, different-pin bench, fabricating phantom drops.
    rel = report.get("relative")
    if isinstance(rel, dict):
        seg = (
            f"**Per-order vs champion (same-pin):** {rel.get('better', 0)} better · "
            f"{rel.get('worse', 0)} worse · {rel.get('matched', 0)} matched · "
            f"{rel.get('new', 0)} new"
        )
        if rel.get("verdict"):
            seg += f" — _{_cell(rel['verdict'])}_"
        lines += [seg, ""]
    else:
        lines += [
            "_Per-order detail is in the machine-readable `relative` block "
            "(same-pin) on the status endpoint._",
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
            f"<sub>Machine-readable detail: `GET /v1/submissions/{submission_id}/status`</sub>"
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
