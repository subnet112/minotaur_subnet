"""Consensus proposal handler — HTTP handler logic for /consensus/proposal.

Delegates verification and scoring to ScoringEngine, signs approval if
verification passes.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from minotaur_subnet.consensus import ConsensusManager
    from minotaur_subnet.validator.scoring_engine import ScoringEngine

logger = logging.getLogger("minotaur_subnet.validator.proposal_handler")


class ProposalHandler:
    """Handles consensus proposal HTTP requests.

    Coordinates between ScoringEngine (verification/scoring) and
    ConsensusManager (signing approvals).
    """

    def __init__(
        self,
        scoring_engine: ScoringEngine,
        consensus: ConsensusManager | None = None,
        score_threshold: float = 0.5,
    ) -> None:
        self.scoring_engine = scoring_engine
        self.consensus = consensus
        self.score_threshold = score_threshold

    async def handle_proposal(self, request: web.Request) -> web.Response:
        """Receive a proposal from the leader, re-score, sign, and return approval.

        Used by non-leader validators to participate in consensus.
        """
        if self.consensus is None:
            return web.json_response(
                {"error": "Consensus not configured"}, status=503,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # ── Verify proposer identity (Issue #6: unauthenticated proposals) ──
        sig_ok, sig_reason = self.scoring_engine.verify_proposer_signature(body)
        if not sig_ok:
            from minotaur_subnet.consensus.dissent import RejectionCode
            code = (
                RejectionCode.MISSING_SIGNATURE
                if "Missing proposer_signature" in sig_reason
                else RejectionCode.SIG_INVALID
            )
            return web.json_response(
                {
                    "error": sig_reason,
                    "approved": False,
                    "reason": sig_reason,
                    "reason_code": code.value,
                },
                status=403,
            )

        # ── Verify and score the proposal ──
        # Audit H9: map sandbox overload to 503 so the leader retries,
        # rather than letting the exception bubble out and crash the
        # request handler. Anything else stays an exception so we get a
        # stack trace in logs.
        try:
            result = await self.scoring_engine.verify_and_score_proposal(
                body,
                score_threshold=self.score_threshold,
            )
        except Exception as exc:
            from minotaur_subnet.engine import SandboxOverloadedError
            if isinstance(exc, SandboxOverloadedError):
                logger.warning("Sandbox overload, returning 503: %s", exc)
                return web.json_response(
                    {"error": "sandbox_overloaded", "reason": str(exc)},
                    status=503,
                )
            raise

        # Handle non-approved results
        if not result.get("approved"):
            status = result.pop("status", 200)
            return web.json_response(result, status=status)

        # Sign approval (use per-order domain for correct on-chain verification)
        approval = self.consensus.sign_approval(
            result["order_id"],
            result["plan_hash"],
            result["local_score"],
            chain_id=result["chain_id"],
            contract_address=result.get("contract_address"),
        )

        return web.json_response({
            "approved": True,
            "validator_id": approval.validator_id,
            "order_id": approval.order_id,
            "plan_hash": approval.plan_hash,
            "score": approval.score,
            "signature": approval.signature,
            "timestamp": approval.timestamp,
        })
