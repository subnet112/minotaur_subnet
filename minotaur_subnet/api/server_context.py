"""
Centralized mutable state for the Minotaur API server.

All module-level variables that were previously scattered across server.py
are now fields on a single ServerContext dataclass.  Route files and tests
access state via ``from minotaur_subnet.api.server_context import ctx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.epoch import SolverRoundEpochClock


@dataclass
class ServerContext:
    """Holds ALL mutable runtime state for the API server."""

    # ── persistence ──────────────────────────────────────────────────────
    store: Any = None  # AppIntentStore

    # ── benchmark / solver rounds ────────────────────────────────────────
    benchmark_worker: Any = None
    benchmark_task: Any = None
    epoch_manager: Any = None
    solver_round_task: Any = None
    solver_round_metagraph_sync: Any = None
    solver_round_metagraph_task: Any = None
    solver_round_role_task: Any = None
    solver_round_role: str = "standalone"
    solver_round_epoch_clock: SolverRoundEpochClock | None = None

    # ── orderbook / block loop ───────────────────────────────────────────
    orderbook: Any = None
    block_loop: Any = None
    block_loop_task: Any = None

    # ── round-anchor parity probe (observability) ────────────────────────
    # Latest snapshot of the canonical fork pins THIS node derives for the
    # current epoch anchor, refreshed by a background task and surfaced on
    # /health. Lets the fleet's pin parity be diffed by polling /health —
    # no log access, no operator action, decoupled from the champion path.
    round_anchor_parity: dict = field(default_factory=dict)
    round_anchor_task: Any = None

    # ── independent adopt vote (CHALLENGER_QUORUM_MODE observability) ─────
    # This validator's latest independent ADOPT/REJECT vote on a candidate,
    # published on /health for the fleet shadow tally (poll, group by candidate).
    last_independent_vote: dict = field(default_factory=dict)
    # Relative per-order adoption verdict for the latest evaluated challenger: the
    # AUTHORITATIVE rule's ADOPT/REJECT + per-order breakdown (this IS the leader's
    # adoption decision; the relative rule is the sole adoption path). Published on
    # /health. Field name kept (last_shadow_per_order_vote) to avoid rippling the
    # health surface; empty until the first evaluation.
    last_shadow_per_order_vote: dict = field(default_factory=dict)
    # Leader's latest would-be champion quorum tally (collected/quorum/signers),
    # published on /health. Populated by the certify path on every round —
    # including under DISABLE_CHAMPION_ADOPTION, where the full consensus runs but
    # the commit is blocked at activation — so the fleet's cross-host agreement is
    # observable with adoption frozen. Never reflects an actual adoption.
    last_champion_quorum: dict = field(default_factory=dict)

    # ── champion-consensus ProtocolConfig ────────────────────────────────
    # Created at startup pointed at BT EVM ValidatorRegistry (for the
    # validator set) + ChampionRegistry (for the quorum threshold). The
    # metagraph_provider is wired LATER, after solver_round_metagraph_sync
    # is initialized, so the refresh loop can do real peer discovery.
    champion_protocol_config: Any = None
    champion_protocol_config_task: Any = None

    # ── order-consensus ProtocolConfig ───────────────────────────────────
    # Sibling of champion's: pointed at Base ValidatorRegistry. Used by
    # the leader's ``ValidatorPeerNetwork`` to auto-discover external
    # validators via metagraph + on-chain registry cross-attestation,
    # combined with any ``ORDER_CONSENSUS_PEERS`` env-pinned set via the
    # peer_network union mode.
    order_protocol_config: Any = None
    order_protocol_config_task: Any = None

    # ── health snapshots ─────────────────────────────────────────────────
    provenance_policy_health: dict = field(default_factory=lambda: {
        "valid": False,
        "startup_validated": False,
        "mode": "unknown",
        "require_signed": False,
        "require_asymmetric": False,
        "submissions_accepting": True,
        "signer_configured": False,
        "verifier_configured": False,
        "allowed_signers_count": 0,
        "hmac_configured": False,
        "error": "startup not completed",
    })
    runtime_security_policy_health: dict = field(default_factory=lambda: {
        "valid": False,
        "startup_validated": False,
        "enforced": False,
        "violations": [],
        "enable_source_submissions": False,
        "allow_subprocess_benchmark": False,
        "require_signed_provenance": False,
        "require_asymmetric_provenance": False,
        "allowed_signers_count": 0,
        "hmac_configured": False,
        "submissions_accepting": True,
        "submissions_api_key_configured": False,
        "submissions_rate_limit_per_minute": 60,
    })


# Single module-level instance — the one source of truth.
ctx = ServerContext()
