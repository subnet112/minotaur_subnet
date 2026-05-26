"""Structured reason codes for peer dissent during consensus.

Peers that reject a proposal today can only return free-text via the
``reason`` field. That's fine for humans reading logs but useless for
forensics or metrics — a peer offline, a peer with a bad signature, and a
peer that independently disagreed on score all collapse into "missing sig"
from the leader's view.

This module gives every rejection a stable short string the leader and
peers agree on. Leader code stores it per-(signer, round/order, timestamp)
for audit; we can wire it to a metric exporter when observability lands.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Deque

logger = logging.getLogger(__name__)


class RejectionCode(str, Enum):
    """Canonical dissent reasons. Add here before using in a peer response."""

    # Transport layer
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"

    # Message layer
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    SIG_INVALID = "SIG_INVALID"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_REGISTERED_VALIDATOR = "NOT_REGISTERED_VALIDATOR"

    # Semantic layer (order consensus)
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    ON_CHAIN_SCORE_BELOW_THRESHOLD = "ON_CHAIN_SCORE_BELOW_THRESHOLD"
    PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
    SIMULATION_FAILED = "SIMULATION_FAILED"
    # Local Anvil unreachable/down/timed-out — distinct from SIMULATION_FAILED
    # which means the plan was simulated but reverted. Followers refuse to sign
    # when this fires because falling back to leader-supplied simulation data is
    # an unverified-trust path.
    SIMULATOR_UNAVAILABLE = "SIMULATOR_UNAVAILABLE"
    APP_NOT_REGISTERED = "APP_NOT_REGISTERED"

    # Semantic layer (champion consensus)
    ROUND_UNKNOWN = "ROUND_UNKNOWN"
    ROUND_WRONG_STATE = "ROUND_WRONG_STATE"
    ROUND_DEADLINE_ELAPSED = "ROUND_DEADLINE_ELAPSED"
    QUORUM_MISMATCH = "QUORUM_MISMATCH"
    BENCHMARK_MISMATCH = "BENCHMARK_MISMATCH"
    BENCHMARK_ERROR = "BENCHMARK_ERROR"
    PACK_HASH_MISMATCH = "PACK_HASH_MISMATCH"
    COMMITTEE_MISMATCH = "COMMITTEE_MISMATCH"

    # Rate-limit enforcement
    RATE_LIMITED = "RATE_LIMITED"

    # Fallback
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DissentEvent:
    """A single peer-rejection event as seen by the leader."""

    peer_id: str            # recovered signer or declared validator_id, whichever we have
    code: RejectionCode
    subject_kind: str       # "order" or "round"
    subject_id: str         # order_id or round_id
    reason: str             # free-text detail ≤200 chars
    timestamp: float = field(default_factory=time.time)


class DissentLog:
    """Bounded in-memory ring buffer of recent rejections.

    Sized to a few thousand entries — enough to forensics a few hours of
    activity without unbounded memory growth. Counters are maintained
    separately so "how many BENCHMARK_MISMATCH this hour" is O(1).
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._events: Deque[DissentEvent] = deque(maxlen=capacity)
        self._counts: dict[RejectionCode, int] = {}
        self._lock = RLock()

    def record(self, event: DissentEvent) -> None:
        with self._lock:
            self._events.append(event)
            self._counts[event.code] = self._counts.get(event.code, 0) + 1
        logger.info(
            "[dissent] %s %s=%s peer=%s reason=%s",
            event.code.value,
            event.subject_kind,
            event.subject_id,
            event.peer_id[:10] if event.peer_id else "?",
            event.reason[:120],
        )

    def recent(self, limit: int = 100) -> list[DissentEvent]:
        with self._lock:
            return list(self._events)[-limit:]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {code.value: n for code, n in self._counts.items()}


# Process-wide singleton. Leader code calls record(); health endpoints can
# read recent()/counts() for diagnostics.
_LOG = DissentLog()


def record_dissent(
    *,
    peer_id: str,
    code: RejectionCode | str,
    subject_kind: str,
    subject_id: str,
    reason: str,
) -> None:
    """Append one rejection to the process-wide log."""
    if not isinstance(code, RejectionCode):
        try:
            code = RejectionCode(str(code))
        except ValueError:
            code = RejectionCode.UNKNOWN
    _LOG.record(DissentEvent(
        peer_id=peer_id or "",
        code=code,
        subject_kind=subject_kind,
        subject_id=subject_id,
        reason=(reason or "")[:200],
    ))


def get_dissent_log() -> DissentLog:
    return _LOG
