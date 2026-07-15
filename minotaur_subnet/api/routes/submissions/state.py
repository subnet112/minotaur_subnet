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


def _default_persist_path(filename: str) -> str | None:
    """Default a store's persistence onto a persistent volume so it survives container
    restarts (e.g. a watchtower image recreate) WITHOUT each operator having to set an
    env. Otherwise a follower's in-memory round/submission store is wiped on every
    update and it can no longer adopt the standing champion — it 404s its own rounds and
    falls back to 100% burn. Prefers the app store's configured volume
    (``APP_INTENTS_STORE_PATH`` dir, which the leader already sets), falls back to the
    conventional ``/data`` mount, and returns None when neither is a writable directory
    — preserving the prior in-memory behavior on nodes with no persistent volume (no
    regression)."""
    from pathlib import Path

    candidates = []
    _app_store = os.environ.get("APP_INTENTS_STORE_PATH")
    if _app_store:
        candidates.append(Path(_app_store).parent)
    candidates.append(Path("/data"))
    for _dir in candidates:
        try:
            if _dir.is_dir() and os.access(_dir, os.W_OK):
                return str(_dir / filename)
        except OSError:
            continue
    return None


# Fail-loud durable-state mode, set by the production api entrypoint. When on, a store whose
# path cannot be resolved RAISES at construction instead of silently running in-memory.
_REQUIRE_DURABLE_STATE = False


def require_durable_state() -> None:
    """Switch the store getters to FAIL LOUD instead of silently running in-memory.

    Called by the production api entrypoint so a node that can't resolve a durable path
    CRASHES at boot with a clear message, instead of silently losing round/submission state
    on the next restart (the #430 class). Tests/dev leave this off; they may run in-memory
    explicitly via set_store/set_round_store."""
    global _REQUIRE_DURABLE_STATE
    _REQUIRE_DURABLE_STATE = True


def _resolve_persist_path(filename: str, env_var: str) -> str | None:
    """Resolve a durable store path (explicit env, else the shared /data default). When
    durable state is required and nothing resolves, RAISE — never a silent in-memory store."""
    path = os.environ.get(env_var) or _default_persist_path(filename)
    if path is None and _REQUIRE_DURABLE_STATE:
        raise RuntimeError(
            f"No durable path for {filename}: set {env_var}, set APP_INTENTS_STORE_PATH, or "
            f"mount a writable /data volume. Refusing to run a persistent store in-memory "
            f"(state would be lost on the next restart)."
        )
    return path


def get_store() -> SubmissionStore:
    """Get or create the submission store singleton."""
    global _store
    if _store is None:
        from pathlib import Path
        persist_path = _resolve_persist_path("submissions.json", "SUBMISSION_STORE_PATH")
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

        import os
        persist_path = _resolve_persist_path("solver_rounds.json", "SOLVER_ROUND_STORE_PATH")
        # The split benchmark worker (BENCHMARK_WORKER_ONLY) is a READ-ONLY sharer
        # of solver_rounds.json — it must NOT sweep orphan temps (that would delete
        # the api coordinator's in-flight persist temp on the shared /data volume).
        _worker_only = os.environ.get("BENCHMARK_WORKER_ONLY", "").lower() in ("1", "true", "yes")
        _round_store = RoundStore(
            persist_path=Path(persist_path) if persist_path else None,
            record_sink=_round_history_sink,
            sweep_orphan_temps=not _worker_only,
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
