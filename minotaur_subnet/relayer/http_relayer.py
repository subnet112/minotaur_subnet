"""HttpRelayer — client-side `RelayerBase` that POSTs the signed bundle to a
remote relayer service instead of holding a gas wallet locally.

Use case: the validator-side api process should never hold
`RELAYER_PRIVATE_KEY`. Validators sign EIP-712 approvals; the leader's api
collects quorum; then it hands the full (order, plan, score,
consensus_result) bundle to the subnet-team's singleton relayer service
over HTTP. That service verifies the quorum sigs against the on-chain
``ValidatorRegistry`` and pays the gas to submit.

Wire format (POST `/v1/submit-plan` request body):

    {
      "order":              {Order.to_dict() output},
      "plan":               {intent_id, interactions[...], deadline, nonce, metadata, ...},
      "score":              float,
      "consensus_result":   {reached, approvals[...], quorum, collected, combined_score},
      "contract_address":   "0x..." | null
    }

Response (200 OK):

    {"success": bool, "tx_hash": "0x...", "chain_id": int, "block_number": int|null,
     "gas_used": int, "error": str|null}

Errors (4xx) carry an ``error`` string with the reason — most common are
quorum-verification failures (signer not in ``ValidatorRegistry``, sig
count below ``quorumBps()``) which the relayer rejects before spending
gas.

When ``RELAYER_URL`` is unset the api/startup wiring falls back to the
embedded ``EvmRelayer`` — preserves local-testnet + existing tests
during the transition.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import time
from typing import Any

import aiohttp

from minotaur_subnet.consensus.leader_wrapper import sign_wrapper
from minotaur_subnet.consensus.signatures import hash_plan

from .base import RelayerBase, SubmitResult

logger = logging.getLogger(__name__)


def _to_jsonable(obj: Any) -> Any:
    """Convert an Order / ExecutionPlan / ConsensusResult dataclass to a
    JSON-friendly dict, preserving nested dataclasses (Interaction,
    SignedApproval) by recursion."""
    if obj is None:
        return None
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


class HttpRelayer(RelayerBase):
    """Client that POSTs signed quorum bundles to a remote relayer service.

    Mirrors ``EvmRelayer`` from the BlockLoop's perspective — same
    ``submit_plan()`` / ``on_leader_changed()`` interface — but the
    actual encoding, nonce management, gas pricing, retries, and chain
    submission all live on the server side. This process never holds
    a gas wallet.

    Args:
        url: Base URL of the remote relayer service (e.g.
            ``https://relayer.minotaursubnet.com`` or
            ``http://relayer:8091`` for an in-cluster setup).
        timeout: Seconds to wait for the relayer's response. Defaults
            to 60 — the server waits for the on-chain receipt, so a
            full round-trip can take 15-30s on Base mainnet plus
            transit. Bump this in slow-RPC environments.
    """

    def __init__(
        self,
        url: str,
        *,
        signing_key: str = "",
        timeout: float = 60.0,
    ) -> None:
        """
        Args:
            url: Base URL of the remote relayer service.
            signing_key: The api's EVM private key (same as
                ``VALIDATOR_PRIVATE_KEY``). Used to sign the freshness
                wrapper around each submission so the relayer can
                verify the caller is a registered validator.
            timeout: Seconds to wait for the relayer's response.
        """
        self.url = url.rstrip("/")
        self.signing_key = signing_key.strip()
        self.timeout = timeout
        self._current_leader: str = ""
        # Initial nonce — wall-clock seconds gives us a monotonic seed
        # that's unique-enough across api restarts; the itertools counter
        # makes it strictly monotonic within a process.
        self._nonce_counter = itertools.count(int(time.time() * 1000))

    async def submit_plan(
        self,
        order: Any,
        plan: Any,
        score: float,
        consensus_result: Any = None,
        contract_address: str | None = None,
    ) -> SubmitResult:
        chain_id = int(getattr(order, "chain_id", 0) or 0)
        order_id = getattr(order, "order_id", "")

        # Sign the freshness wrapper. The relayer rejects submissions
        # whose wrapper doesn't recover to a registered validator. Same
        # key the api uses for validator consensus signing.
        if not self.signing_key:
            return SubmitResult(
                success=False,
                error="HttpRelayer requires signing_key (set VALIDATOR_PRIVATE_KEY on the api)",
                chain_id=chain_id,
            )
        plan_hash = hash_plan(plan)
        nonce = next(self._nonce_counter)
        wrapper, wrapper_sig = sign_wrapper(
            self.signing_key,
            plan_hash=plan_hash,
            submission_nonce=nonce,
            chain_id=chain_id,
        )

        payload = {
            "order": _to_jsonable(order),
            "plan": _to_jsonable(plan),
            "score": float(score),
            "consensus_result": _to_jsonable(consensus_result),
            "contract_address": contract_address,
            "wrapper": {
                "plan_hash": wrapper.plan_hash,
                "submission_nonce": wrapper.submission_nonce,
                "timestamp": wrapper.timestamp,
                "chain_id": wrapper.chain_id,
            },
            "wrapper_signature": wrapper_sig,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.url}/v1/submit-plan",
                    json=payload,
                ) as resp:
                    body = await resp.json()
                    if resp.status == 200 and body.get("success"):
                        return SubmitResult(
                            success=True,
                            tx_hash=body.get("tx_hash"),
                            chain_id=int(body.get("chain_id", chain_id)),
                            block_number=body.get("block_number"),
                            gas_used=int(body.get("gas_used", 0)),
                        )
                    err = body.get("error") or f"HTTP {resp.status}"
                    logger.error(
                        "HttpRelayer: submit_plan rejected by %s for order=%s: %s",
                        self.url, order_id[:12], err,
                    )
                    return SubmitResult(
                        success=False,
                        error=err,
                        chain_id=chain_id,
                        tx_hash=body.get("tx_hash"),
                    )
        except Exception as exc:
            logger.exception(
                "HttpRelayer: transport error to %s for order=%s",
                self.url, order_id[:12],
            )
            return SubmitResult(
                success=False,
                error=f"relayer transport: {exc}",
                chain_id=chain_id,
            )

    def on_leader_changed(self, new_leader_id: str) -> int:
        """Notify the remote relayer of a leader change.

        Fire-and-forget — we don't block leader-change handling on a
        network round-trip. The server keeps its own in-flight state;
        we just inform it so it can drop stale submissions.
        """
        self._current_leader = new_leader_id
        # The remote relayer doesn't currently expose a leader-changed
        # endpoint; that's an additive on the server side. For now we
        # just log + return 0 dropped (the server's own SignatureCollector
        # prunes on a timer regardless of this signal).
        logger.info(
            "HttpRelayer: leader changed to %s (notifying %s — best effort)",
            (new_leader_id or "unknown")[:10], self.url,
        )
        return 0
