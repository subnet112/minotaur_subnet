"""Submission feedback report (P1).

Turns a benchmarked submission into an actionable report so a miner knows where
they stand: per-case JS + on-chain scores, threshold pass/fail, the aggregate vs
the current champion, the dethrone gap, and the worst cases to fix.

Pure assembly from data already on ``Submission.benchmark_details`` plus a few
scalars the caller looks up (champion score, threshold, dethrone margin, reason).
No new benchmark compute — this is a read + shape job, so it stays cheap on the
status endpoint.

Scope notes (see docs/submission-feedback-report-spec.md):
  * P1 (here): this submission's per-case detail + aggregate-vs-champion.
  * P2: champion *per-case* delta (exact for fixed scenarios).
  * P3: public/shadow split (inert here — everything is reported as public).
"""

from __future__ import annotations

from typing import Any

_WORST_N = 5
# GitHub rejects issue-comment bodies over 65536 chars; only truncate if a
# (very large) report would actually exceed it — otherwise show every case.
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
    champion_details: dict[str, Any] | None = None,
    won: bool = False,
) -> dict[str, Any] | None:
    """Assemble the feedback report, or None if the submission isn't far enough.

    Returns None while the submission is still queued/screening/benchmarking
    (nothing to report yet). Defensive: tolerates missing fields. Pass the
    champion submission's ``benchmark_details`` as ``champion_details`` to get a
    per-case champion-vs-challenger comparison (joined by ``intent_id``).
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

    # ── champion per-case map (joined by intent_id) for a head-to-head ──
    champ_by_case: dict[str, dict[str, Any]] = {}
    for cpi in (champion_details or {}).get("per_intent") or []:
        cid = cpi.get("intent_id")
        if cid is not None:
            champ_by_case[cid] = {
                "js": cpi.get("score"),
                "on_chain": cpi.get("on_chain_score"),
            }

    # ── per-case (this submission, plus the champion's score where known) ──
    scorecard = details.get("scorecard") or {}
    scenario_scores = scorecard.get("scenario_scores") or {}
    per_intent = details.get("per_intent") or []
    per_case: list[dict[str, Any]] = []
    for pi in per_intent:
        label = pi.get("intent_id")
        js = pi.get("score")
        if js is None:
            js = scenario_scores.get(label)
        entry: dict[str, Any] = {
            "case": label,
            "your": {
                "js": js,
                "on_chain": pi.get("on_chain_score"),
                "passed": js is not None and js >= threshold,
                "had_plan": pi.get("has_plan"),
                "error": pi.get("error"),
                "revert_reason": pi.get("revert_reason"),
                "revert_trace": pi.get("revert_trace"),
                "mock_sim": pi.get("mock_simulation", False),
            },
        }
        champ = champ_by_case.get(label)
        if champ is not None:
            entry["champion"] = champ
            if js is not None and champ.get("js") is not None:
                entry["delta"] = round(js - champ["js"], 6)
        per_case.append(entry)
    worst_cases = [
        c["case"]
        for c in sorted(
            (c for c in per_case if c["your"]["js"] is not None),
            key=lambda c: c["your"]["js"],
        )[:_WORST_N]
    ]

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
        "per_case": per_case,
        "worst_cases": worst_cases,
        "coverage": {
            "public_cases": len(per_case),
            "shadow_cases": 0,  # P3: real public/shadow split (inert until shadow ships)
        },
    }
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


def _render_trace_details(case: str, trace: dict[str, Any]) -> str:
    """Render one case's per-step revert trace as a collapsed ``<details>`` block:
    which decoded call ran, its status (revert reason on the failing step), gas."""
    summary = _cell(trace.get("summary") or "revert trace")
    out = [f"<details><summary>🔬 <code>{_cell(case)}</code> — {summary}</summary>", "",
           "| # | Call | Status | Gas |", "|---|---|---|---|"]
    for step in trace.get("interactions") or []:
        idx = step.get("index")
        fn = _cell(step.get("fn") or step.get("target") or "—")
        status = step.get("status") or "—"
        if status == "reverted" and step.get("revert_reason"):
            status = f"reverted: {_cell(step.get('revert_reason'))}"
        gas = step.get("gas_used")
        out.append(
            f"| {idx if idx is not None else '—'} | `{fn}` | {status} | "
            f"{gas if gas is not None else '—'} |"
        )
    out += ["", "</details>"]
    return "\n".join(out)


def render_report_md(report: dict[str, Any] | None, *, submission_id: str | None = None) -> str:
    """Render a :func:`build_submission_report` dict into a GitHub-flavored
    markdown comment for the miner's PR — the FULL per-case detail, worst cases
    first.

    Pure formatting, tolerant of partial/empty reports. Returns ``""`` when
    there is nothing to render. Truncates only if the body would exceed GitHub's
    issue-comment size limit (every case is shown otherwise).
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

    # Full per-case table — worst first so the fixes are at the top. When the
    # champion's per-case scores are present, order by the regression (Δ) and
    # show a head-to-head column; otherwise by the challenger's own JS.
    per_case = list(report.get("per_case") or [])
    if per_case:
        has_champ = any("champion" in c for c in per_case)

        def _sort_key(c: dict[str, Any]) -> tuple[int, float]:
            if has_champ and c.get("delta") is not None:
                return (1, float(c["delta"]))  # biggest regression (most negative) first
            js = (c.get("your") or {}).get("js")
            return (0, -1.0) if js is None else (1, float(js))  # unscored first

        per_case.sort(key=_sort_key)
        lines.append(f"**Per-case results** ({len(per_case)} case(s), worst first):")
        lines.append("")
        if has_champ:
            lines += ["| Case | You | Champion | Δ | On-chain (bps) | Result |",
                      "|---|---|---|---|---|---|"]
        else:
            lines += ["| Case | JS | On-chain (bps) | Result |", "|---|---|---|---|"]

        traces: list[tuple[str, dict[str, Any]]] = []
        for c in per_case:
            y = c.get("your") or {}
            err = y.get("error") or y.get("revert_reason")
            # ✅ only if it cleared the JS threshold AND didn't error / revert on
            # chain — so a revert never reads as a pass.
            res = "✅" if (y.get("passed") and not err) else "❌"
            note = err or ""
            if y.get("mock_sim"):
                note = f"{note} · mock-sim".strip(" ·")
            if note:
                res = f"{res} {_cell(note)}"
            onchain = y.get("on_chain")
            onchain_cell = onchain if onchain is not None else "—"
            case_cell = f"`{_cell(c.get('case'))}`"
            if has_champ:
                ch = c.get("champion") or {}
                delta = c.get("delta")
                delta_cell = (
                    f"{'+' if delta >= 0 else ''}{_num(delta, 3)}" if delta is not None else "—"
                )
                lines.append(
                    f"| {case_cell} | {_num(y.get('js'), 3)} | {_num(ch.get('js'), 3)} | "
                    f"{delta_cell} | {onchain_cell} | {res} |"
                )
            else:
                lines.append(
                    f"| {case_cell} | {_num(y.get('js'), 3)} | {onchain_cell} | {res} |"
                )
            if y.get("revert_trace"):
                traces.append((str(c.get("case")), y["revert_trace"]))
        lines.append("")

        # Per-step revert traces (collapsed) for the cases that captured one.
        for case_label, trace in traces:
            lines.append(_render_trace_details(case_label, trace))
            lines.append("")

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
