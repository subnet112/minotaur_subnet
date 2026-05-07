"""Relayer interface and mock implementation.

The relayer collects validator signatures and submits co-signed transactions
to target chains. MockRelayer returns fake tx hashes for testing.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    """Result of submitting an approved plan to the target chain."""
    success: bool
    tx_hash: str | None = None
    error: str | None = None
    chain_id: int = 0
    block_number: int | None = None
    gas_used: int = 0


class RelayerBase(ABC):
    """Abstract base for relayer implementations."""

    @abstractmethod
    async def submit_plan(
        self,
        order: Any,
        plan: Any,
        score: float,
        consensus_result: Any = None,
        contract_address: str | None = None,
    ) -> SubmitResult:
        """Submit an approved plan for on-chain execution."""
        ...

    def on_leader_changed(self, new_leader_id: str) -> int:
        """Drop all in-flight submissions on leader change (REL-12).

        Returns the number of dropped submissions. Subclasses should
        override to cancel any pending on-chain transactions.
        """
        return 0


class MockRelayer(RelayerBase):
    """Mock relayer that returns fake tx hashes. Logs submissions for testing."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self._current_leader: str = ""

    async def submit_plan(
        self,
        order: Any,
        plan: Any,
        score: float,
        consensus_result: Any = None,
        contract_address: str | None = None,
    ) -> SubmitResult:
        tx_hash = f"0x{uuid.uuid4().hex}"
        chain_id = getattr(order, "chain_id", 1)
        self.submissions.append({
            "order_id": getattr(order, "order_id", "unknown"),
            "score": score,
            "tx_hash": tx_hash,
            "chain_id": chain_id,
            "timestamp": time.time(),
        })
        return SubmitResult(
            success=True,
            tx_hash=tx_hash,
            chain_id=chain_id,
            block_number=12345678,
            gas_used=150_000,
        )

    def on_leader_changed(self, new_leader_id: str) -> int:
        """Clear submission log and update leader (REL-12)."""
        dropped = len(self.submissions)
        if dropped > 0:
            logger.info(
                "Leader changed to %s — dropping %d submissions",
                new_leader_id[:10] if new_leader_id else "unknown", dropped,
            )
            self.submissions.clear()
        self._current_leader = new_leader_id
        return dropped
