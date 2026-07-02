"""Benchmark pack hash — consensus primitive for round scoring.

A "benchmark pack" is the full set of scenarios that will be used to
score a solver for a given round: every operational app's synthetic scenarios
(from its JS manifest) PLUS the historical orders sampled deterministically from
round_id. These form ONE flat scenario set — the synthetic/historical STAGE split
+ weighting were removed; the relative rule joins all orders by intent_id.

The V1 hash still emits the historical/synthetic sections separately (byte-for-byte
unchanged) so the rip-out is hash-invisible and mixed old/new builds stay
consensus-compatible. A single-flat-set V2 hash is available behind the
``BENCHMARK_PACK_V2`` code constant (default OFF; flip fleet-wide atomically).

The pack hash is a canonical SHA-256 digest of the scenario inventory.
All validators compute the same hash from the same round_id, and the
hash is included in champion certification proposals. If any validator's
pack differs (e.g. they have out-of-sync app manifests), their approval
won't match and consensus will fail to form.

This hash is what gets stored in ChampionCertificate.benchmark_pack_hash
and attested on-chain via ChampionRegistry.certify().
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Fleet-uniform CODE constant — NEVER env-read. A per-validator value would split
# the pack hash and strand quorum. ON: the single-flat-set V2 hash (matches the
# single-stage benchmark). This is a fleet-wide ATOMIC cutover (same discipline as
# ROUND_ANCHORED_PIN / the compute budget) — every validator must ship the same value
# in one release, or mixed V1/V2 builds compute different pack hashes and quorum
# strands. Set back to False only to roll the whole fleet back to the V1 layout.
BENCHMARK_PACK_V2: bool = True


def compute_pack_hash(
    round_id: str,
    synthetic_scenarios: Iterable[dict[str, Any]],
    historical_order_ids: Iterable[str],
    compute_budget: dict[str, Any] | None = None,
    block_rewrite: dict[str, Any] | None = None,
) -> str:
    """Compute the canonical SHA-256 hash of a benchmark pack.

    The hash must be reproducible across validators, so input
    serialization is strict and sorted:
    - round_id is prefixed verbatim
    - synthetic scenarios are sorted by (app_id, scenario_name)
    - each scenario contributes its name + params dict (sorted keys)
    - historical order IDs are sorted alphabetically

    Args:
        round_id: The current round identifier.
        synthetic_scenarios: List of dicts with keys at minimum:
            {"app_id", "name", "params", "chains"} — the scenarios
            as they'd be sent to the solver.
        historical_order_ids: Order IDs sampled for Stage 2.

    Returns:
        Hex string (0x-prefixed, 64 chars) representing the SHA-256 digest.
    """
    h = hashlib.sha256()
    if BENCHMARK_PACK_V2:
        # SINGLE FLAT SET (V2): one unified SCENARIOS section — synthetic scenarios
        # and historical orders as one sorted set of canonical tokens, mirroring the
        # single-stage benchmark (no synthetic/historical split). Scores + the retired
        # _stage tag were never hashed, so this differs from V1 ONLY in structure.
        h.update(b"MINOTAUR_BENCHMARK_PACK_V2\n")
        h.update(round_id.encode("utf-8"))
        h.update(b"\n")
        tokens = [
            json.dumps(_canonical_scenario(s), sort_keys=True, separators=(",", ":"))
            for s in synthetic_scenarios
        ]
        tokens += [
            json.dumps({"order_id": oid}, separators=(",", ":"))
            for oid in historical_order_ids
        ]
        h.update(b"SCENARIOS\n")
        for tok in sorted(tokens):
            h.update(tok.encode("utf-8"))
            h.update(b"\n")
    else:
        h.update(b"MINOTAUR_BENCHMARK_PACK_V1\n")
        h.update(round_id.encode("utf-8"))
        h.update(b"\n")

        # Synthetic: sort by (app_id, scenario_name) for determinism
        synthetic_list = sorted(
            list(synthetic_scenarios),
            key=lambda s: (s.get("app_id", ""), s.get("name", "")),
        )
        h.update(b"SYNTHETIC\n")
        for scenario in synthetic_list:
            canonical = _canonical_scenario(scenario)
            h.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")

        # Historical: sort order IDs alphabetically
        historical_sorted = sorted(historical_order_ids)
        h.update(b"HISTORICAL\n")
        for order_id in historical_sorted:
            h.update(order_id.encode("utf-8"))
            h.update(b"\n")

    # Deterministic RPC compute budget (the budget proxy's {budget, cost_table}
    # record). Folded in ONLY when active, so the hash is byte-identical to the
    # pre-budget pack while inert (backward-compatible — a fleet not yet running
    # the budget computes the unchanged hash). Once a fleet enforces the budget,
    # this binds it into consensus: a validator on a different budget or cost
    # table produces a different pack hash and cannot reach quorum with the
    # majority. CONSENSUS-BREAKING the instant it goes non-None fleet-wide — the
    # cost_table_version must be bumped + rolled out atomically (same discipline
    # as ROUND_ANCHORED_PIN), or quorum silently drops during a staggered
    # upgrade.
    if compute_budget is not None:
        h.update(b"COMPUTE_BUDGET_V1\n")
        h.update(
            json.dumps(compute_budget, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        h.update(b"\n")

    # Block-pin rewrite table (the proxy's versioned block-rewrite rules). Folded
    # in ONLY when the solver-read proxy is active, so the hash is byte-identical
    # to the pre-pin pack while inert (a fleet not yet routing reads through the
    # proxy computes the unchanged hash). Once a fleet routes reads through the
    # proxy this binds the rewrite table into consensus: a validator on a
    # different BLOCK_REWRITE_VERSION (which would pin reads differently) produces
    # a different pack hash and cannot reach quorum. CONSENSUS-BREAKING the
    # instant it goes non-None fleet-wide — bump BLOCK_REWRITE_VERSION + roll out
    # atomically (same discipline as ROUND_ANCHORED_PIN / the compute budget).
    if block_rewrite is not None:
        h.update(b"BLOCK_REWRITE_V1\n")
        h.update(
            json.dumps(block_rewrite, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        h.update(b"\n")

    return "0x" + h.hexdigest()


def _canonical_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Extract only the fields that matter for pack identity.

    Fields like descriptions, comments, or stateful metadata are excluded.
    """
    keys = ("app_id", "name", "intent_function", "params", "chains", "fund")
    return {k: scenario[k] for k in keys if k in scenario}


def collect_synthetic_scenarios(app_store: Any) -> list[dict[str, Any]]:
    """Gather all synthetic scenarios from operational apps' manifests.

    Walks the app store, extracts benchmark_scenarios from each app's
    JS manifest, annotates each with the app_id. Returns a flat list
    suitable for pack hash computation.
    """
    scenarios: list[dict[str, Any]] = []
    if app_store is None:
        return scenarios

    try:
        apps = app_store.list_apps()
    except Exception as exc:
        logger.warning("Failed to list apps for pack hash: %s", exc)
        return scenarios

    for app in apps:
        manifest = getattr(app, "manifest", None) or {}
        app_scenarios = manifest.get("benchmark_scenarios", []) or []
        for scenario in app_scenarios:
            if not isinstance(scenario, dict):
                continue
            scenarios.append({
                "app_id": app.app_id,
                **scenario,
            })

    return scenarios
