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
) -> dict[str, Any] | None:
    """Assemble the feedback report, or None if the submission isn't far enough.

    Returns None while the submission is still queued/screening/benchmarking
    (nothing to report yet). Defensive: tolerates missing fields.
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

    # ── per-case (this submission only; champion per-case delta is P2) ──
    scorecard = details.get("scorecard") or {}
    scenario_scores = scorecard.get("scenario_scores") or {}
    per_intent = details.get("per_intent") or []
    per_case: list[dict[str, Any]] = []
    for pi in per_intent:
        label = pi.get("intent_id")
        js = pi.get("score")
        if js is None:
            js = scenario_scores.get(label)
        per_case.append(
            {
                "case": label,
                "your": {
                    "js": js,
                    "on_chain": pi.get("on_chain_score"),
                    "passed": js is not None and js >= threshold,
                    "had_plan": pi.get("has_plan"),
                    "error": pi.get("error"),
                    "revert_reason": pi.get("revert_reason"),
                    "mock_sim": pi.get("mock_simulation", False),
                },
            }
        )
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
