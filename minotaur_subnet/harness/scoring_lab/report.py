"""The Sealed Report — the content-addressed, portable artifact the whole mechanism turns on.

A Report = a SEAL (the reproducible scoring inputs: the sealed cases + the fork block + the
scorer digest + the min-deriving slippage) plus ONE solver's scores on that seal. Two reports
that share a `report_hash` were scored on identical inputs and are therefore DIFFABLE — and the
diff is the adoption decision. Because the seal is content-addressed, a miner can regenerate the
champion's report on their own machine and verify it, and validators can agree by hash.

What's hashed (the seal): the cases (params incl. quote-derived min + fork block) + scorer digest
+ slippage. What's NOT hashed: the per-solver scores (OUTPUTS, differ per solver), the rule/margin/
floor (DECISION policy applied to the report), and the timestamp. Four layers (provenance /
regression-floor / head-to-head / live) are tagged per case; the lab populates head-to-head, the
others are filled in production (plan Phase 5).

Ports to: harness/benchmark_pack.py:compute_pack_hash (extended to bind block + scorer digest) and a
production report store + EIP-712 commitment (plan Phases 4-7).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import LabConfig, Scenario
from .stages import _card_global
from .pipeline import SolverResult

REPORT_VERSION = "v1"


def scorer_digest(scorer_path: str) -> str:
    try:
        body = Path(scorer_path).read_text().encode()
    except OSError:
        body = scorer_path.encode()  # inline JS
    return "sha256:" + hashlib.sha256(body).hexdigest()[:16]


def canonical_case(sc: Scenario, fork_block: int | None, layer: str = "head_to_head") -> dict[str, Any]:
    """The sealed, content-addressed inputs of one case (no scores)."""
    return {
        "app_id": sc.app_id,
        "name": sc.name,
        "intent_function": sc.intent_function,
        "chain_id": sc.chain_id,
        "fork_block": fork_block,
        "layer": layer,
        "params": {
            "input_token": sc.input_token,
            "output_token": sc.output_token,
            "input_amount": sc.input_amount,
            "min_output_amount": sc.min_output_amount,   # quote-derived (Phase 1)
            "quoted_output": sc.quoted_output,
            "receiver": sc.receiver,
            "contract_address": sc.contract_address,
        },
    }


def compute_report_hash(cases: list[dict], fork_block: int | None, digest: str, slippage_bps: int) -> str:
    h = hashlib.sha256()
    h.update(b"MINOTAUR_SCORING_REPORT_" + REPORT_VERSION.encode() + b"\n")
    h.update(f"fork_block={fork_block}\n".encode())
    h.update(f"scorer={digest}\n".encode())
    h.update(f"slippage_bps={slippage_bps}\n".encode())
    for c in sorted(cases, key=lambda c: (c["app_id"], c["name"])):
        h.update(json.dumps(c, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return "sha256:" + h.hexdigest()[:16]


def build_report(
    label: str, result: SolverResult, scenarios: list[Scenario], cfg: LabConfig,
    timestamp: int | None = None,
) -> dict[str, Any]:
    digest = scorer_digest(cfg.scorer_path)
    cases = [canonical_case(sc, cfg.fork_block) for sc in scenarios]
    report_hash = compute_report_hash(cases, cfg.fork_block, digest, cfg.slippage_bps)
    return {
        "report_version": REPORT_VERSION,
        "seal": {
            "report_hash": report_hash,
            "fork_block": cfg.fork_block,
            "scorer_digest": digest,
            "slippage_bps": cfg.slippage_bps,
            "timestamp": timestamp,
            "cases": cases,
        },
        "solver": {
            "label": label,
            "global_score": round(_card_global(result.card), 6),
            "app_scores": {k: round(v, 6) for k, v in result.card.app_scores.items()},
            "coverage": round(result.card.coverage, 4),
            "total": result.card.total,
            "per_case": result.per_case,
            "onchain_by_app": result.onchain_by_app,
            "quote_by_app": result.quote_by_app,
        },
        "layer": "head_to_head",
    }


def diff_reports(champ_report: dict, chal_report: dict, adopt: bool, adopt_rec) -> dict[str, Any]:
    """The adoption inputs: pair two reports (must share a seal) and surface the verdict."""
    ch_hash = champ_report["seal"]["report_hash"]
    cl_hash = chal_report["seal"]["report_hash"]
    return {
        "report_hash": ch_hash,
        "comparable": ch_hash == cl_hash,
        "champion": champ_report["solver"]["label"],
        "challenger": chal_report["solver"]["label"],
        "verdict": "ADOPT" if adopt else "REJECT",
        "decision": adopt_rec.outputs,
    }
