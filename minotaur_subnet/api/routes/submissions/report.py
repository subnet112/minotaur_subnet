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
            from minotaur_subnet.epoch.relative_scoring import FACTOR_MARGIN
            from minotaur_subnet.harness.screening import FLOOR_VERSION, MAX_REGION_NODES

            floor_version: int | None = FLOOR_VERSION
            floor_cap: int | None = MAX_REGION_NODES
            factor_margin: int | None = FACTOR_MARGIN
        except Exception:  # additive surface — never break the report
            floor_version = floor_cap = factor_margin = None
        armed = floor_cap is not None or factor_margin is not None
        block: dict[str, Any] = {
            "max_region_nodes": factor,
            "floor_version": floor_version,
            # The armed state is COMPUTED from the live constants — never
            # hardcoded, so this block can't lie about whether the rule bites.
            "armed": armed,
            "observe_only": not armed,  # backward-compat alias of ``not armed``
            "floor_cap": floor_cap,
            "factor_margin": factor_margin,
        }
        # Same-pin champion baseline, attached by the round evaluation
        # (_persist_round_relative_counts) when both records were in hand —
        # gives the miner the actionable numbers: their value, the champion's,
        # and the delta the tie-break judged.
        rel_fz = rel.get("factorization") if isinstance(rel, dict) else None
        if isinstance(rel_fz, dict):
            block["champion_nodes"] = rel_fz.get("champion_nodes")
            block["factor_delta"] = rel_fz.get("factor_delta")
        report["factorization"] = block

    # Deadwood metric (Phase 0, OBSERVE-ONLY) — this submission's OWN persisted
    # unproductive_nodes plus the top-offender deletion list, so a miner can see
    # exactly WHAT to delete. Never recomputed here; champion comparison arrives
    # with the (future) margin PR. Measured, not gated.
    dw_version = getattr(sub, "unproductive_metric_version", None)
    if dw_version is not None:
        try:
            from minotaur_subnet.harness.deadwood import UNPRODUCTIVE_NODES_MAX

            # Phase 0: always True — the floor is disarmed (None) and no
            # tie-break margin exists yet.
            observe_only = UNPRODUCTIVE_NODES_MAX is None
        except Exception:  # additive surface — never break the report
            observe_only = True
        report["deadwood"] = {
            "unproductive_nodes": getattr(sub, "unproductive_nodes", None),
            "metric_version": dw_version,
            "observe_only": observe_only,
            "top_offenders": getattr(sub, "unproductive_top_offenders", None),
        }

    # GAS-PAR clause (ships DISARMED) — rule-state transparency, mirroring the
    # factorization block. Always emitted (the block describes the RULE, not a
    # per-submission metric): a miner sees whether matched-output-less-gas is a
    # live way to win, at what margin, and — when the stored relative block
    # carries the same-pin totals — the actual numbers the tie-break judged.
    try:
        from minotaur_subnet.epoch.relative_scoring import GAS_BASIS, GAS_MARGIN_BPS

        gas_margin: int | None = GAS_MARGIN_BPS
        gas_basis: str | None = GAS_BASIS
    except Exception:  # additive surface — never break the report
        gas_margin = gas_basis = None
    gas_armed = gas_margin is not None
    gas_block: dict[str, Any] = {
        # The armed state is COMPUTED from the live constant — never
        # hardcoded, so this block can't lie about whether the rule bites.
        "armed": gas_armed,
        "observe_only": not gas_armed,  # backward-compat alias of ``not armed``
        "gas_margin_bps": gas_margin,
        "basis": gas_basis,
    }
    rel_gas = rel.get("gas") if isinstance(rel, dict) else None
    if isinstance(rel_gas, dict):
        # Same-pin totals/coverage, attached by the round evaluation
        # (_persist_round_relative_counts) when the clause was armed.
        gas_block["champ_total"] = rel_gas.get("champ_total")
        gas_block["chal_total"] = rel_gas.get("chal_total")
        gas_block["measured_full"] = rel_gas.get("measured_full")
        gas_block["unmeasured"] = rel_gas.get("unmeasured")
        gas_block["order_worse"] = rel_gas.get("order_worse")
    report["gas"] = gas_block

    # DEADWOOD tie-break (4th ladder key, ships ARMED — fires only once records
    # carry same-version unproductive metrics) — RULE-state transparency,
    # mirroring the gas block. Always emitted (it describes the RULE, not a
    # per-submission metric). Keyed ``deadwood_rule``, NOT ``deadwood``: the
    # #575 lineage (which implements the metric itself) adds an observe-only
    # per-submission ``deadwood`` block (own unproductive_nodes +
    # top_offenders) under that key — the two blocks merge into one
    # ``deadwood`` block when the lineages converge; the distinct key avoids a
    # semantic merge collision until then.
    try:
        from minotaur_subnet.epoch.relative_scoring import UNPRODUCTIVE_MARGIN

        dw_margin: int | None = UNPRODUCTIVE_MARGIN
    except Exception:  # additive surface — never break the report
        dw_margin = None
    dw_armed = dw_margin is not None
    dw_block: dict[str, Any] = {
        # The armed state is COMPUTED from the live constant — never
        # hardcoded, so this block can't lie about whether the rule bites.
        "armed": dw_armed,
        "observe_only": not dw_armed,  # backward-compat alias of ``not armed``
        "unproductive_margin": dw_margin,
    }
    rel_dw = rel.get("deadwood") if isinstance(rel, dict) else None
    if isinstance(rel_dw, dict):
        # Same-pin baseline/delta, attached by the round evaluation
        # (_persist_round_relative_counts) when both records were in hand.
        dw_block["candidate_nodes"] = rel_dw.get("candidate_nodes")
        dw_block["champion_nodes"] = rel_dw.get("champion_nodes")
        dw_block["deadwood_delta"] = rel_dw.get("deadwood_delta")
    report["deadwood_rule"] = dw_block

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

        if rel.get("adopt_via") == "gas":
            gz = rel.get("gas") if isinstance(rel.get("gas"), dict) else {}
            lines += [
                (
                    "⛽ **Won on gas:** matched the champion on every order, "
                    f"delivered the same outputs on materially less total gas "
                    f"(yours {gz.get('chal_total', '?')} vs champion "
                    f"{gz.get('champ_total', '?')}, "
                    f"margin {gz.get('gas_margin_bps', '?')} bps)."
                ),
                "",
            ]

        if rel.get("adopt_via") == "factorization":
            fz = rel.get("factorization") if isinstance(rel.get("factorization"), dict) else {}
            lines += [
                (
                    "🧹 **Won on factorization:** matched the champion on every order, "
                    f"largest code region {fz.get('factor_delta', '?')} AST nodes smaller "
                    f"(yours {fz.get('candidate_nodes', '?')} vs champion {fz.get('champion_nodes', '?')}, "
                    f"margin {fz.get('factor_margin', '?')})."
                ),
                "",
            ]

        if rel.get("adopt_via") == "deadwood":
            dz = rel.get("deadwood") if isinstance(rel.get("deadwood"), dict) else {}
            lines += [
                (
                    "🪓 **Won on deadwood:** matched the champion on every order, "
                    f"{dz.get('deadwood_delta', '?')} fewer unproductive AST nodes "
                    f"(yours {dz.get('candidate_nodes', '?')} vs champion "
                    f"{dz.get('champion_nodes', '?')}, margin {dz.get('margin', '?')})."
                ),
                "",
            ]

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
            hint = (
                f"_Identical output to the champion on all {n} orders. To win you need a "
                f"strictly better route on at least one order — find pairs/sizes where a "
                f"different route returns more output"
            )
            # Second way to win (when the factorization tie-break is armed and
            # the same-pin baseline is known): dethrone on cleaner code. Name
            # the exact target the same way the ❌ rows name the orders to fix.
            fz = rel.get("factorization") if isinstance(rel.get("factorization"), dict) else None
            if (
                fz
                and fz.get("armed")
                and isinstance(fz.get("factor_margin"), int)
                and isinstance(fz.get("champion_nodes"), int)
            ):
                need = fz["champion_nodes"] - fz["factor_margin"]
                hint += (
                    f" — OR ship better-factored code: your largest code region is "
                    f"{fz.get('candidate_nodes', '?')} AST nodes vs the champion's "
                    f"{fz['champion_nodes']}; get it to ≤ {need} to win this tie on "
                    f"factorization (lower is better)"
                )
            # Third way to win (when the GAS-PAR tie-break is armed and the
            # same-pin totals are known): dethrone on materially less total
            # gas. Name the exact target the same way the factor hint does.
            gz = rel.get("gas") if isinstance(rel.get("gas"), dict) else None
            if (
                gz
                and gz.get("armed")
                and isinstance(gz.get("gas_margin_bps"), int)
                and isinstance(gz.get("champ_total"), int)
                and gz["champ_total"] > 0
            ):
                need_gas = gz["champ_total"] * (10000 - gz["gas_margin_bps"]) // 10000
                hint += (
                    f" — OR deliver the same outputs on less gas: your total "
                    f"metered gas is {gz.get('chal_total', '?')} vs the champion's "
                    f"{gz['champ_total']}; get it below {need_gas} to win this tie "
                    f"on gas (lower is better)"
                )
            # Fourth way to win (when the DEADWOOD tie-break is armed and the
            # same-pin version-guarded delta is known): dethrone on materially
            # less dead code. Name the exact target the same way the factor
            # and gas hints do. need_dw = margin - delta (delta is already
            # version-guarded by the persist pass); shown only while positive
            # — at delta >= margin the clause either won or was blocked by a
            # factor decision, and either way "delete more" is not the ask.
            dz = rel.get("deadwood") if isinstance(rel.get("deadwood"), dict) else None
            if (
                dz
                and dz.get("armed")
                and isinstance(dz.get("margin"), int)
                and isinstance(dz.get("deadwood_delta"), int)
                and dz["deadwood_delta"] < dz["margin"]
            ):
                need_dw = dz["margin"] - dz["deadwood_delta"]
                hint += (
                    f" — OR ship less dead code: your unproductive-node count "
                    f"is {dz.get('candidate_nodes', '?')} vs the champion's "
                    f"{dz.get('champion_nodes', '?')}; delete ≥ {need_dw} more "
                    f"dead nodes to win this tie on deadwood (lower is better)"
                )
            lines += [hint + "._", ""]
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
