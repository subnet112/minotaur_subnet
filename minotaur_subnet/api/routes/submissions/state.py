"""Shared module-level state for the submissions package.

Holds singleton references to stores, managers, and providers that are
wired up by the server at startup and accessed by routes and business logic.
"""

from __future__ import annotations

import os
import re
import threading
from collections import deque
from typing import Any

from minotaur_subnet.harness.submission_store import SubmissionStore
from minotaur_subnet.harness.round_store import RoundState, RoundStore

# ── Singleton state ─────────────────────────────────────────────────────────

_store: SubmissionStore | None = None
_round_store: RoundStore | None = None
_epoch_manager: Any = None
_champion_consensus_manager: Any = None
_champion_peer_network: Any = None
_solver_round_epoch_provider: Any = None
_benchmark_worker: Any = None
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[str, deque[float]] = {}
_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def get_store() -> SubmissionStore:
    """Get or create the submission store singleton."""
    global _store
    if _store is None:
        from pathlib import Path
        persist_path = os.environ.get("SUBMISSION_STORE_PATH")
        _store = SubmissionStore(
            persist_path=Path(persist_path) if persist_path else None,
        )
    return _store


def set_store(store: SubmissionStore) -> None:
    """Override the store (for testing)."""
    global _store
    _store = store


def _round_history_sink(state: RoundState) -> None:
    """Mirror a round mutation into the order-book DB (AppIntentStore) so round
    history is durable + queryable via GET /v1/solver/rounds. Best-effort —
    never breaks round logic (RoundStore._record also guards)."""
    try:
        from minotaur_subnet.api.server_context import ctx
        save = getattr(getattr(ctx, "store", None), "save_round", None)
        if callable(save):
            save(state.to_dict())
    except Exception:  # noqa: BLE001 — history is best-effort
        pass


def get_round_store() -> RoundStore:
    """Get or create the solver round store singleton."""
    global _round_store
    if _round_store is None:
        from pathlib import Path

        persist_path = os.environ.get("SOLVER_ROUND_STORE_PATH")
        _round_store = RoundStore(
            persist_path=Path(persist_path) if persist_path else None,
            record_sink=_round_history_sink,
        )
        # One-time backfill: mirror rounds already in the JSON store into the
        # order-book DB so history is complete from first use (best-effort).
        try:
            for _rs in _round_store.list_rounds():
                _round_history_sink(_rs)
        except Exception:  # noqa: BLE001
            pass
    return _round_store


def set_round_store(store: RoundStore) -> None:
    """Override the round store (for testing)."""
    global _round_store
    _round_store = store


def get_epoch_manager() -> Any | None:
    """Return the shared round coordinator if configured."""
    return _epoch_manager


def set_epoch_manager(manager: Any | None) -> None:
    """Override the epoch manager/coordinator (for testing/server wiring)."""
    global _epoch_manager
    _epoch_manager = manager


def get_champion_consensus_manager() -> Any | None:
    """Return the shared champion consensus manager if configured."""
    return _champion_consensus_manager


def set_champion_consensus_manager(manager: Any | None) -> None:
    """Override the champion consensus manager (for testing/server wiring)."""
    global _champion_consensus_manager
    _champion_consensus_manager = manager


def get_champion_peer_network() -> Any | None:
    """Return the shared champion peer network if configured."""
    return _champion_peer_network


def set_champion_peer_network(network: Any | None) -> None:
    """Override the champion peer network (for testing/server wiring)."""
    global _champion_peer_network
    _champion_peer_network = network


def get_benchmark_worker() -> Any | None:
    """Return the shared benchmark worker (leader only) if configured."""
    return _benchmark_worker


def set_benchmark_worker(worker: Any | None) -> None:
    """Override the benchmark worker (server wiring / diagnostics)."""
    global _benchmark_worker
    _benchmark_worker = worker


def get_solver_round_epoch_provider() -> Any | None:
    """Return the shared solver round epoch provider if configured."""
    return _solver_round_epoch_provider


def set_solver_round_epoch_provider(provider: Any | None) -> None:
    """Override the solver round epoch provider (for testing/server wiring)."""
    global _solver_round_epoch_provider
    _solver_round_epoch_provider = provider
