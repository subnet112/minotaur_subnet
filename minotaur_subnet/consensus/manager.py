"""Consensus manager for multi-validator agreement on execution plans.

Phase 2 MVP: Single-validator auto-approve. The validator signs its own
approval and immediately reaches quorum.

Future: Multi-validator consensus where N-of-M validators must independently
agree on the same plan hash and score before the relayer can submit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.shared.types import ExecutionPlan, SignedApproval, ConsensusResult
from .protocol_config import ProtocolConfig
from .signatures import sign_plan_approval, verify_plan_approval, hash_plan

logger = logging.getLogger(__name__)


class ConsensusManager:
    """Manages validator consensus for execution plan approval.

    Phase 2 MVP: single-validator mode. The manager auto-approves with
    its own signature, achieving instant quorum of 1/1.

    Args:
        validator_id: This validator's address/ID.
        private_key: This validator's signing key.
        protocol_config: Holds the canonical quorum_bps (read from
            ValidatorRegistry, refreshed in place). The ConsensusManager
            reads ``protocol_config.quorum_bps`` whenever it needs the
            current threshold, so on-chain ``setQuorumBps`` changes
            propagate without restart.
        validators: List of all validator addresses in the set.
        timeout: Seconds to wait for signatures before giving up.
    """

    def __init__(
        self,
        validator_id: str,
        private_key: str,
        protocol_config: ProtocolConfig,
        validators: list[str] | None = None,
        timeout: float = 30.0,
        chain_id: int = 31337,
        contract_address: str = "0x" + "00" * 20,
        domain_separator: bytes | None = None,
        score_threshold_bps: int = 5000,
    ) -> None:
        self.validator_id = validator_id
        self.private_key = private_key
        self.protocol_config = protocol_config
        self.validators = validators or [validator_id]
        self.timeout = timeout
        self.chain_id = chain_id
        self.contract_address = contract_address
        self.score_threshold_bps = score_threshold_bps

        # Pre-compute or accept domain separator
        if domain_separator is not None:
            self.domain_separator = domain_separator
        else:
            from .eip712 import build_domain_separator
            self.domain_separator = build_domain_separator(chain_id, contract_address)

        # Pending proposals awaiting signatures
        self._pending: dict[str, _PendingProposal] = {}
        self._lock = asyncio.Lock()

    @property
    def quorum_bps(self) -> int:
        """Current network quorum threshold in basis points.

        Reads through to ``protocol_config.quorum_bps`` so an on-chain
        ``ValidatorRegistry.setQuorumBps`` is picked up without restart
        on the next refresh tick.
        """
        return self.protocol_config.quorum_bps

    @property
    def quorum_required(self) -> int:
        """Number of validators needed for quorum."""
        n = len(self.validators)
        return max(1, (n * self.quorum_bps + 9999) // 10000)  # ceil division

    async def propose(
        self,
        order_id: str,
        plan: ExecutionPlan,
        score: float,
        plan_hash: str,
        chain_id: int | None = None,
        contract_address: str | None = None,
    ) -> ConsensusResult:
        """Propose a plan for consensus and sign it.

        In single-validator mode, this immediately returns with quorum reached.
        In multi-validator mode, this would broadcast the proposal and wait
        for other validators' signatures.
        """
        # Sign our approval (use per-order domain if provided)
        approval = self.sign_approval(
            order_id, plan_hash, score,
            chain_id=chain_id, contract_address=contract_address,
        )

        # Single-validator fast path
        if len(self.validators) == 1:
            return ConsensusResult(
                reached=True,
                approvals=[approval],
                quorum=1,
                collected=1,
                combined_score=score,
            )

        # Multi-validator: start collecting
        logger.info(
            "Multi-validator propose: order=%s validators=%d quorum=%d",
            order_id, len(self.validators), self.quorum_required,
        )
        # Compute per-order domain if chain_id/contract_address provided
        order_domain = self.domain_separator
        if chain_id is not None and contract_address:
            from .eip712 import build_domain_separator
            order_domain = build_domain_separator(chain_id, contract_address)
        proposal = _PendingProposal(
            order_id=order_id,
            plan_hash=plan_hash,
            score=score,
            quorum=self.quorum_required,
            domain_separator=order_domain,
            chain_id=chain_id if chain_id is not None else self.chain_id,
        )
        proposal.add_approval(approval)
        self._pending[order_id] = proposal

        # Check if we already have quorum (e.g., quorum=1 with >1 validators)
        if proposal.has_quorum:
            approvals = sorted(
                proposal.approvals.values(),
                key=lambda a: int(a.validator_id.replace("0x", ""), 16),
            )
            del self._pending[order_id]
            return ConsensusResult(
                reached=True,
                approvals=approvals,
                quorum=proposal.quorum,
                collected=len(approvals),
                combined_score=proposal.average_score,
            )

        # Wait for other validators to call receive_approval()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            async with self._lock:
                if order_id not in self._pending:
                    # Was resolved by receive_approval()
                    return ConsensusResult(reached=False, quorum=proposal.quorum)
                if self._pending[order_id].has_quorum:
                    p = self._pending.pop(order_id)
                    approvals = sorted(
                        p.approvals.values(),
                        key=lambda a: int(a.validator_id.replace("0x", ""), 16),
                    )
                    return ConsensusResult(
                        reached=True,
                        approvals=approvals,
                        quorum=p.quorum,
                        collected=len(approvals),
                        combined_score=p.average_score,
                    )
            await asyncio.sleep(0.05)

        # Timeout — quorum not reached; leave in _pending for prune_expired()
        return ConsensusResult(
            reached=False,
            approvals=list(proposal.approvals.values()),
            quorum=proposal.quorum,
            collected=len(proposal.approvals),
            combined_score=proposal.average_score,
        )

    def sign_approval(
        self,
        order_id: str,
        plan_hash: str,
        score: float,
        chain_id: int | None = None,
        contract_address: str | None = None,
    ) -> SignedApproval:
        """Sign an approval for a plan.

        Signs with the contract's scoreThreshold BPS (not the JS float score),
        matching what verifyValidatorSignatures expects on-chain.

        If ``chain_id`` and ``contract_address`` are provided, a dynamic
        domain separator is used (matching the target contract).
        """
        domain = self.domain_separator
        if chain_id is not None and contract_address:
            from .eip712 import build_domain_separator
            domain = build_domain_separator(chain_id, contract_address)
        signature = sign_plan_approval(
            self.private_key, order_id, plan_hash, score,
            domain_separator=domain,
            score_bps=self.score_threshold_bps,
        )
        return SignedApproval(
            validator_id=self.validator_id,
            order_id=order_id,
            plan_hash=plan_hash,
            score=score,
            signature=signature,
            timestamp=time.time(),
        )

    async def receive_approval(self, approval: SignedApproval) -> ConsensusResult | None:
        """Receive an approval from another validator.

        Returns ConsensusResult if quorum is now reached, else None.
        """
        async with self._lock:
            if approval.order_id not in self._pending:
                logger.warning(
                    "Received approval for unknown order: %s", approval.order_id,
                )
                return None

            # Verify the signature
            if approval.validator_id not in self.validators:
                logger.warning(
                    "Received approval from non-validator: %s", approval.validator_id,
                )
                return None

            proposal = self._pending[approval.order_id]
            # Use per-order domain if stored, otherwise fall back to default
            verify_domain = proposal.domain_separator or self.domain_separator
            if not verify_plan_approval(
                approval.validator_id,
                approval.signature,
                approval.order_id,
                approval.plan_hash,
                approval.score,
                domain_separator=verify_domain,
                score_bps=self.score_threshold_bps,
            ):
                logger.warning(
                    "Invalid signature from %s for order %s",
                    approval.validator_id, approval.order_id,
                )
                return None

            # Cross-check against the on-chain ValidatorRegistry when
            # CONSENSUS_ENFORCE_ONCHAIN_REGISTRY=1. Default-off keeps
            # behavior identical for setups without a deployed registry.
            # See consensus/validator_registry_cache.py for the 5s TTL cache.
            from minotaur_subnet.consensus.validator_registry_cache import (
                is_on_chain_validator,
            )
            chain_id_for_check = proposal.chain_id or 0
            if chain_id_for_check and not is_on_chain_validator(
                approval.validator_id, chain_id_for_check,
            ):
                logger.warning(
                    "Signer %s not registered on-chain ValidatorRegistry for "
                    "chain %d (order %s)",
                    approval.validator_id, chain_id_for_check, approval.order_id,
                )
                return None

            # Check plan hash matches
            if approval.plan_hash != proposal.plan_hash:
                logger.warning(
                    "Plan hash mismatch from %s: expected %s got %s",
                    approval.validator_id, proposal.plan_hash, approval.plan_hash,
                )
                return None

            proposal.add_approval(approval)

            if proposal.has_quorum:
                # Don't delete from _pending here; propose()'s polling loop
                # will detect quorum, clean up, and return the sorted result.
                return ConsensusResult(
                    reached=True,
                    approvals=list(proposal.approvals.values()),
                    quorum=proposal.quorum,
                    collected=len(proposal.approvals),
                    combined_score=proposal.average_score,
                )

            return None

    async def clear_all_pending(self) -> int:
        """Drop all pending proposals (CON-15: called on leader change).

        Returns the number of proposals cleared.
        """
        async with self._lock:
            count = len(self._pending)
            if count > 0:
                logger.info("Clearing %d pending proposals (leader change)", count)
                self._pending.clear()
            return count

    async def prune_expired(self, now: float | None = None) -> list[str]:
        """Remove proposals that have timed out."""
        async with self._lock:
            now = now or time.time()
            expired = []
            for order_id, proposal in list(self._pending.items()):
                if now - proposal.created_at > self.timeout:
                    expired.append(order_id)
                    del self._pending[order_id]
            return expired


@dataclass
class _PendingProposal:
    """Internal tracking of a pending consensus round."""
    order_id: str
    plan_hash: str
    score: float
    quorum: int
    approvals: dict[str, SignedApproval] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    domain_separator: bytes | None = None  # Per-order domain for signature verification
    chain_id: int | None = None  # Used by the on-chain registry cross-check

    def add_approval(self, approval: SignedApproval) -> None:
        self.approvals[approval.validator_id] = approval

    @property
    def has_quorum(self) -> bool:
        return len(self.approvals) >= self.quorum

    @property
    def average_score(self) -> float:
        if not self.approvals:
            return 0.0
        return sum(a.score for a in self.approvals.values()) / len(self.approvals)
