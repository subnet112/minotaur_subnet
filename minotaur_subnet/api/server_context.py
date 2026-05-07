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
