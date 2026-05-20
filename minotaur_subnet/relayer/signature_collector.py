"""Signature collector for the relayer.

Aggregates validator signatures for a plan hash. Once quorum is reached,
returns the pending execution for on-chain submission.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.consensus.protocol_config import ProtocolConfig


@dataclass
class PendingExecution:
    """An execution ready for on-chain submission (quorum met)."""
    order_id: str
    plan_hash: str
    score: float
    signatures: list[tuple[str, bytes]]  # (validator_address, signature)
    created_at: float = field(default_factory=time.time)
    order: Any = None
    plan: Any = None


class SignatureCollector:
    """Collects validator signatures and detects quorum.

    Validators submit signatures to the relayer. Once enough valid
    signatures are collected, the relayer can submit the co-signed
    transaction to the target chain.

    Args:
        protocol_config: Holds the canonical quorum_bps (read from
            ValidatorRegistry, refreshed in place). Read through on every
            quorum check so on-chain ``setQuorumBps`` changes propagate
            without restarting the relayer.
        validators: Set of valid validator addresses.
        timeout: Seconds before expiring incomplete collections.
    """

    def __init__(
        self,
        protocol_config: ProtocolConfig,
        validators: list[str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.protocol_config = protocol_config
        self.validators = set(v.lower() for v in (validators or []))
        self.timeout = timeout

        # plan_hash -> collected signatures
        self._pending: dict[str, _CollectionState] = {}

    @property
    def quorum_bps(self) -> int:
        """Current network quorum threshold in basis points (live-read)."""
        return self.protocol_config.quorum_bps

    @property
    def quorum_required(self) -> int:
        n = len(self.validators)
        return max(1, (n * self.quorum_bps + 9999) // 10000)

    def add_signature(
        self,
        plan_hash: str,
        validator_address: str,
        signature: bytes,
        order_id: str = "",
        score: float = 0.0,
        order: Any = None,
        plan: Any = None,
    ) -> PendingExecution | None:
        """Add a validator signature. Returns PendingExecution when quorum is met."""
        addr = validator_address.lower()
        if addr not in self.validators:
            return None

        if plan_hash not in self._pending:
            self._pending[plan_hash] = _CollectionState(
                order_id=order_id,
                plan_hash=plan_hash,
                score=score,
                order=order,
                plan=plan,
            )

        state = self._pending[plan_hash]

        # Prevent duplicate signatures
        if addr in state.signers:
            return None

        state.signers.add(addr)
        state.signatures.append((validator_address, signature))

        if len(state.signatures) >= self.quorum_required:
            # Quorum reached
            result = PendingExecution(
                order_id=state.order_id,
                plan_hash=plan_hash,
                score=state.score,
                signatures=list(state.signatures),
                order=state.order,
                plan=state.plan,
            )
            del self._pending[plan_hash]
            return result

        return None

    def prune_expired(self, now: float | None = None) -> list[str]:
        """Remove stale collections that haven't reached quorum."""
        now = now or time.time()
        expired = []
        for plan_hash, state in list(self._pending.items()):
            if now - state.created_at > self.timeout:
                expired.append(plan_hash)
                del self._pending[plan_hash]
        return expired

    @property
    def pending_count(self) -> int:
        return len(self._pending)


@dataclass
class _CollectionState:
    order_id: str
    plan_hash: str
    score: float
    signers: set[str] = field(default_factory=set)
    signatures: list[tuple[str, bytes]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    order: Any = None
    plan: Any = None
