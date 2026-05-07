"""Local testnet consensus — auto-approve with all validator keys.

For the local testnet only. Signs plan approvals with every registered
validator's private key, instantly satisfying the on-chain quorum check
(AppIntentBase.executeIntent → verifyValidatorSignatures).

Usage in server.py:
    from minotaur_subnet.consensus.local_consensus import LocalTestnetConsensus
    consensus = LocalTestnetConsensus(
        validator_keys=[(addr1, key1), (addr2, key2), ...],
        score_threshold_bps=5000,
    )
    block_loop.set_consensus(consensus)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minotaur_subnet.shared.types import ConsensusResult, SignedApproval
from minotaur_subnet.consensus.signatures import sign_plan_approval
from minotaur_subnet.consensus.eip712 import build_domain_separator

logger = logging.getLogger(__name__)


class LocalTestnetConsensus:
    """Signs plan approvals with all known validator keys.

    Produces N-of-N signatures for every proposal, guaranteeing that
    the on-chain quorum check always passes on the local testnet.
    """

    def __init__(
        self,
        validator_keys: list[tuple[str, str]],
        score_threshold_bps: int = 5000,
    ) -> None:
        """
        Args:
            validator_keys: List of (address, hex_private_key) tuples.
            score_threshold_bps: Score threshold in BPS (must match the
                                 contract's scoreThreshold).
        """
        self.validator_keys = validator_keys
        self.score_threshold_bps = score_threshold_bps

    async def propose(
        self,
        order_id: str,
        plan: Any,
        score: float,
        plan_hash: str,
        *,
        chain_id: int = 31337,
        contract_address: str = "",
    ) -> ConsensusResult:
        """Auto-approve by signing with every validator key."""
        if not contract_address:
            raise ValueError("LocalTestnetConsensus requires contract_address")

        domain_separator = build_domain_separator(chain_id, contract_address)
        approvals: list[SignedApproval] = []

        for addr, key in self.validator_keys:
            sig = sign_plan_approval(
                key,
                order_id,
                plan_hash,
                score,
                domain_separator=domain_separator,
                score_bps=self.score_threshold_bps,
            )
            approvals.append(SignedApproval(
                validator_id=addr,
                order_id=order_id,
                plan_hash=plan_hash,
                score=score,
                signature=sig,
                timestamp=time.time(),
            ))

        # Sort by address ascending (required by EIP712Verifier on-chain)
        approvals.sort(
            key=lambda a: int(a.validator_id.replace("0x", ""), 16),
        )

        logger.info(
            "Local consensus: auto-approved order %s with %d/%d validators",
            order_id[:16], len(approvals), len(self.validator_keys),
        )

        return ConsensusResult(
            reached=True,
            approvals=approvals,
            quorum=len(approvals),
            collected=len(approvals),
            combined_score=score,
        )
