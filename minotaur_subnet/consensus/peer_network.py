"""ValidatorPeerNetwork — HTTP-based proposal broadcasting between validators.

Leader broadcasts proposals to all peers via ``POST /consensus/proposal``.
Non-leaders receive proposals, re-score, sign approvals, and return them.
The leader's ConsensusManager collects approvals for quorum.

Peer discovery:
- Production: ``ProtocolConfig.refresh_loop`` walks the Bittensor metagraph
  axon list, probes each ``/identity``, and cross-checks against the on-chain
  ``ValidatorRegistry``. The peer list is mutated in place; ``peers``
  property reads through to ``protocol_config.peers`` on every call.
- Test / local-testnet override: pass an explicit ``peers`` list to the
  constructor, or set ``ORDER_CONSENSUS_PEERS`` env (``addr@url`` pairs)
  and let api/startup.py parse it via ``parse_peers_env``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from minotaur_subnet.harness.round_store import ChampionApproval
from minotaur_subnet.shared.types import SignedApproval

logger = logging.getLogger(__name__)


@dataclass
class PeerEndpoint:
    """A remote validator's network endpoint."""

    validator_id: str  # EVM address (0x...)
    url: str  # http://host:port


class ValidatorPeerNetwork:
    """HTTP-based peer networking for multi-validator consensus.

    The leader uses ``broadcast_proposal()`` to send proposals to all peers.
    Each peer independently verifies, re-scores, signs, and returns an approval.
    Approvals are fed into ``ConsensusManager.receive_approval()``.

    Args:
        validator_id: This validator's EVM address.
        private_key: This validator's signing key (hex).
        consensus: The ConsensusManager instance for receiving approvals.
        peers: Initial list of peer endpoints.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        validator_id: str,
        private_key: str,
        consensus: Any,
        peers: list[PeerEndpoint] | None = None,
        protocol_config: Any = None,
        timeout: float = 10.0,
        default_headers: dict[str, str] | None = None,
        peer_url_transform: Callable[[str], str] | None = None,
    ) -> None:
        self.validator_id = validator_id
        self._private_key = private_key
        self._signing_key_fallback_warned = False
        self.consensus = consensus
        # When peers is explicitly passed, pin it (tests, manual override).
        # When None and protocol_config is set, the peers property reads
        # through to discovered peers so this network automatically targets
        # whatever peers the discovery loop has found.
        self._peers_override: list[PeerEndpoint] | None = (
            [p for p in peers if p.validator_id != validator_id]
            if peers is not None else None
        )
        self.protocol_config = protocol_config
        self.timeout = timeout
        self._default_headers = dict(default_headers or {})
        self._peer_url_transform = peer_url_transform
        self._session: aiohttp.ClientSession | None = None
        # Result summary of the most recent champion-proposal broadcast, read by
        # the coordinator's cert handler for restart recovery: when the reachable
        # fleet rejects with ROUND_WRONG_STATE (the round they hold is aborted —
        # e.g. the leader replayed a round the fleet moved past after a restart),
        # the leader accepts that verdict and advances instead of looping the
        # doomed cert until the deadline. Shape: {"round_id": str, "approvals":
        # int, "dissent_codes": list[str], "fleet_aborted": bool}.
        self.last_champion_broadcast: dict[str, Any] | None = None

    @property
    def private_key(self) -> str:
        """This validator's signing key (hex).

        Resolves to the construction-time key, but falls back to the
        ``VALIDATOR_PRIVATE_KEY`` env var when that is empty. This keeps
        lifecycle broadcasts and champion proposals SIGNED even if this instance
        was somehow handed an empty key — observed in production as a
        recreate-vs-restart difference where a freshly recreated container's
        champion network reaches sign-time with no key, so broadcasts went out
        unsigned and followers 401'd. ``VALIDATOR_PRIVATE_KEY`` is set from the
        compose substitution on every boot, so it is the reliable source of
        truth; the warning below flags the underlying keyless-instance bug for
        root-causing without ever dropping a signature.
        """
        if self._private_key:
            return self._private_key
        env_key = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
        if env_key and not self._signing_key_fallback_warned:
            logger.warning(
                "ValidatorPeerNetwork(%s): construction-time signing key was "
                "empty; falling back to VALIDATOR_PRIVATE_KEY env so broadcasts "
                "and proposals stay signed. This flags the recreate-vs-restart "
                "keyless-instance bug — investigate the empty construction key.",
                self.validator_id[:10],
            )
            self._signing_key_fallback_warned = True
        return env_key

    def set_peers(self, peers: list[PeerEndpoint]) -> None:
        """Set the pinned peer override.

        The override and the discovery loop combine into a union when the
        ``peers`` property is read — pinned entries take precedence on
        ``validator_id`` collisions, then any discovered peers not in the
        override are appended. This lets operators env-pin in-cluster peers
        (which aren't on the Bittensor metagraph) while still picking up
        externally-reachable validators auto-discovered via metagraph +
        on-chain ``ValidatorRegistry`` cross-attestation.
        """
        # Exclude self
        self._peers_override = [p for p in peers if p.validator_id != self.validator_id]
        logger.info("Peer list updated (pinned): %d peers", len(self._peers_override))

    @property
    def peers(self) -> list[PeerEndpoint]:
        """Current peer list — union of pinned override + discovered set.

        Sources, in order:
          1. ``set_peers()`` override (typically ``ORDER_CONSENSUS_PEERS``
             env at startup — the team's in-cluster peers that aren't on
             the Bittensor metagraph).
          2. ``protocol_config.peers`` from the discovery loop (metagraph
             walk + ``ValidatorRegistry`` cross-attestation + ``/identity``
             EIP-712 verification).

        Deduped by lowercased ``validator_id``. Pinned entries win on
        collision so an operator URL override is respected even if
        discovery still reports an older URL. Self filtered from both
        sources.

        Backward-compat with the previous mutually-exclusive behavior:
        if only one source has entries, the union is exactly that source.
        """
        result: list[PeerEndpoint] = []
        seen: set[str] = set()
        me_lower = self.validator_id.lower()

        if self._peers_override is not None:
            for p in self._peers_override:
                key = p.validator_id.lower()
                if key == me_lower or key in seen:
                    continue
                seen.add(key)
                result.append(p)

        if self.protocol_config is not None:
            for p in self.protocol_config.peers:
                key = p.evm_address.lower()
                if key == me_lower or key in seen:
                    continue
                seen.add(key)
                # Prefer the peer's advertised public API base (from /identity)
                # when present — it's the reachable endpoint the operator chose,
                # which may differ from the daemon axon's host:port (e.g. an API
                # behind a domain/LB). Otherwise fall back to the axon→API port
                # transform (or the raw axon when no transform is configured).
                advertised = getattr(p, "api_url", None)
                if advertised:
                    url = advertised
                elif self._peer_url_transform is not None:
                    url = self._peer_url_transform(p.axon_url)
                else:
                    url = p.axon_url
                result.append(
                    PeerEndpoint(validator_id=p.evm_address, url=url)
                )

        return result

    def set_default_headers(self, headers: dict[str, str] | None) -> None:
        """Update default headers sent to peer validators."""
        self._default_headers = dict(headers or {})

    async def start(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        logger.info(
            "PeerNetwork started (validator=%s, peers=%d)",
            self.validator_id[:10], len(self.peers),
        )

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def broadcast_proposal(
        self,
        order_id: str,
        plan: Any,
        score: float,
        plan_hash: str,
        order: Any = None,
        app_id: str = "",
        simulation: Any = None,
    ) -> list[SignedApproval]:
        """Broadcast a proposal to all peers and collect approvals.

        Called by the leader after scoring a plan. Sends concurrent
        requests to all peers and feeds valid approvals into the
        ConsensusManager.

        Returns:
            List of successfully collected SignedApprovals.
        """
        # Snapshot once — discovery loop may swap the list during broadcast.
        peers = self.peers
        if not peers:
            # Was a silent `return []` — live incident 2026-07-16: the peer
            # set flapped to empty one second before an order's proposal,
            # nobody was asked to approve, and the leader waited out the
            # full consensus timeout before terminally rejecting the order.
            # The order processor now defers instead of burning the timeout
            # (it checks `peers` before proposing); this warning covers any
            # other caller reaching here with an empty set.
            logger.warning(
                "broadcast_proposal(%s): peer list is EMPTY — proposal sent "
                "to nobody; quorum > 1 cannot succeed this round",
                order_id,
            )
            return []

        if self._session is None or self._session.closed:
            await self.start()

        # Build proposal payload (includes leader's simulation result
        # so followers can re-score without re-simulating)
        payload = self._build_proposal_payload(
            order_id, plan, score, plan_hash, app_id, order,
            simulation=simulation,
        )

        # Send to all peers concurrently, feeding approvals as they arrive
        # so ConsensusManager.propose() can reach quorum without waiting
        # for ALL peers to respond.
        print(f"[CONSENSUS] Broadcasting to {len(peers)} peers: {[p.url for p in peers]}", flush=True)
        tasks = {
            asyncio.ensure_future(self._send_proposal(peer, payload)): peer
            for peer in peers
        }
        approvals: list[SignedApproval] = []
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
            except Exception as exc:
                logger.warning("Proposal to peer failed: %s", exc)
                continue
            if result is not None:
                approvals.append(result)
                # Feed into consensus manager immediately so propose()
                # can reach quorum as soon as enough peers respond.
                await self.consensus.receive_approval(result)

        logger.info(
            "Broadcast complete: %d/%d approvals collected",
            len(approvals), len(peers),
        )
        return approvals

    async def broadcast_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Broadcast an authenticated JSON payload to all peers."""
        peers = self.peers
        if not peers:
            logger.warning(
                "broadcast_json(%s): peer list is EMPTY — payload sent to nobody",
                path,
            )
            return []

        if self._session is None or self._session.closed:
            await self.start()

        tasks = [
            self._send_json(peer, payload, path=path)
            for peer in peers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        responses: list[dict[str, Any]] = []
        for peer, result in zip(peers, results):
            if isinstance(result, Exception):
                logger.warning(
                    "JSON sync to %s failed: %s",
                    peer.validator_id[:10], result,
                )
                continue
            if result is not None:
                responses.append(result)

        logger.info(
            "JSON sync complete for %s: %d/%d responses",
            path,
            len(responses),
            len(peers),
        )
        return responses

    async def broadcast_champion_proposal(
        self,
        proposal: Any,
        *,
        collector: Any | None = None,
        close_epoch: int | None = None,
        quorum_required: int | None = None,
        decision_deadline_epoch: int | None = None,
        committee_block: int | None = None,
        request_timeout: float | None = None,
        path: str = "/v1/solver/round/consensus/proposal",
    ) -> list[ChampionApproval]:
        """Broadcast a champion certification proposal to validator peers.

        ``request_timeout`` (seconds) overrides the per-POST timeout for this
        broadcast only — used by the observe-only shadow-quorum harvest, where
        followers run a full reactive benchmark (minutes) before signing, far
        longer than the default session timeout. ``None`` keeps the session default.
        """
        peers = self.peers
        if not peers:
            logger.warning(
                "broadcast_champion_proposal(%s): peer list is EMPTY — "
                "proposal sent to nobody; quorum > 1 cannot succeed this round",
                getattr(proposal, "round_id", proposal),
            )
            return []

        if self._session is None or self._session.closed:
            await self.start()

        payload = self._build_champion_proposal_payload(
            proposal,
            close_epoch=close_epoch,
            quorum_required=quorum_required,
            decision_deadline_epoch=decision_deadline_epoch,
            committee_block=committee_block,
        )

        dissent_codes: list[str] = []

        async def _send_with_peer(peer: PeerEndpoint) -> tuple[PeerEndpoint, ChampionApproval | Exception | None]:
            try:
                # Only thread request_timeout when explicitly set (the shadow
                # harvest), so the real broadcast call is byte-identical to before.
                _champ_kw: dict[str, Any] = {"path": path, "dissent_sink": dissent_codes}
                if request_timeout is not None:
                    _champ_kw["request_timeout"] = request_timeout
                result = await self._send_champion_proposal(peer, payload, **_champ_kw)
                return peer, result
            except Exception as exc:  # pragma: no cover - defensive
                return peer, exc

        tasks = [
            asyncio.create_task(_send_with_peer(peer))
            for peer in peers
        ]

        approvals: list[ChampionApproval] = []
        for task in asyncio.as_completed(tasks):
            peer, result = await task
            if isinstance(result, Exception):
                logger.warning(
                    "Champion proposal to %s failed: %s",
                    peer.validator_id[:10], result,
                )
                continue
            if result is None:
                continue
            approvals.append(result)
            if collector is not None:
                collector.receive_approval(result)

        # Restart-recovery signal: the reachable fleet rejected this proposal AND
        # every rejection was ROUND_WRONG_STATE — i.e. the round the fleet holds is
        # aborted, so the leader is out of sync (it replayed a round the fleet moved
        # past). Conservative: requires zero approvals and that EVERY responding peer
        # said ROUND_WRONG_STATE (a single different reason → real disagreement, not a
        # stale round → don't trip). Network errors aren't dissents, so they never
        # set this. The coordinator reads it to abort + advance instead of looping.
        from minotaur_subnet.consensus.dissent import RejectionCode
        fleet_aborted = (
            not approvals
            and bool(dissent_codes)
            and all(c == RejectionCode.ROUND_WRONG_STATE.value for c in dissent_codes)
        )
        self.last_champion_broadcast = {
            "round_id": str(getattr(proposal, "round_id", "")),
            "approvals": len(approvals),
            "dissent_codes": dissent_codes,
            "fleet_aborted": fleet_aborted,
        }
        logger.info(
            "Champion broadcast complete: %d/%d approvals collected%s",
            len(approvals), len(peers),
            " — fleet reports round aborted" if fleet_aborted else "",
        )
        return approvals

    def _build_proposal_payload(
        self,
        order_id: str,
        plan: Any,
        score: float,
        plan_hash: str,
        app_id: str,
        order: Any = None,
        simulation: Any = None,
    ) -> dict[str, Any]:
        """Build the JSON payload for a proposal."""
        # Serialize plan
        plan_dict: dict[str, Any] = {}
        if hasattr(plan, "interactions"):
            plan_dict = {
                "intent_id": plan.intent_id,
                "interactions": [
                    {
                        "target": ix.target,
                        "value": ix.value,
                        "call_data": ix.call_data,
                        "chain_id": ix.chain_id,
                    }
                    for ix in plan.interactions
                ],
                "deadline": plan.deadline,
                "nonce": plan.nonce,
                "metadata": plan.metadata,
            }

        payload = {
            "order_id": order_id,
            "plan": plan_dict,
            "score": score,
            "plan_hash": plan_hash,
            "app_id": app_id,
            "intent_function": getattr(order, "intent_function", "execute") or "execute",
            "params": dict(getattr(order, "params", {}) or {}),
            "chain_id": int(getattr(order, "chain_id", 1) or 1),
            "submitted_by": getattr(order, "submitted_by", "") or "",
            "deadline": int(getattr(order, "deadline", 0) or 0),
            "perpetual": bool(getattr(order, "perpetual", False)),
            "max_executions": int(getattr(order, "max_executions", 1) or 1),
            "cooldown": int(getattr(order, "cooldown", 0) or 0),
            "proposer": self.validator_id,
            "timestamp": time.time(),
            "simulation": self._serialize_simulation(simulation),
        }

        # Sign the payload so peers can verify the proposer identity
        # (required when CONSENSUS_REQUIRE_SIGNED_PROPOSALS=1)
        if self.private_key:
            try:
                from eth_account import Account
                from eth_account.messages import encode_defunct
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                msg = encode_defunct(text=canonical)
                sig = Account.sign_message(msg, private_key=self.private_key)
                payload["proposer_signature"] = sig.signature.hex()
            except Exception as exc:
                logger.warning("Failed to sign proposal: %s", exc)

        return payload

    @staticmethod
    def _serialize_simulation(simulation: Any) -> dict[str, Any] | None:
        """Serialize a SimulationResult for inclusion in the proposal payload."""
        if simulation is None:
            return None
        return {
            "success": getattr(simulation, "success", False),
            "gas_used": getattr(simulation, "gas_used", 0),
            "token_transfers": [
                {
                    "token": getattr(t, "token", ""),
                    "from_addr": getattr(t, "from_addr", ""),
                    "to_addr": getattr(t, "to_addr", ""),
                    "amount": str(getattr(t, "amount", "0")),
                }
                for t in (getattr(simulation, "token_transfers", None) or [])
            ],
            "on_chain_score": getattr(simulation, "on_chain_score", None),
            "error": getattr(simulation, "error", None),
        }

    def _build_champion_proposal_payload(
        self,
        proposal: Any,
        *,
        close_epoch: int | None = None,
        quorum_required: int | None = None,
        decision_deadline_epoch: int | None = None,
        committee_block: int | None = None,
    ) -> dict[str, Any]:
        """Build the JSON payload for champion certification."""
        payload = {
            "round_id": getattr(proposal, "round_id", ""),
            "committee_hash": getattr(proposal, "committee_hash", None),
            "incumbent_image_id": getattr(proposal, "incumbent_image_id", None),
            "candidate_submission_id": getattr(proposal, "candidate_submission_id", None),
            "candidate_image_id": getattr(proposal, "candidate_image_id", None),
            "benchmark_pack_hash": getattr(proposal, "benchmark_pack_hash", None),
            "shadow_case_log_hash": getattr(proposal, "shadow_case_log_hash", None),
            "effective_epoch": getattr(proposal, "effective_epoch", 0),
            "close_epoch": close_epoch,
            "quorum_required": quorum_required,
            "decision_deadline_epoch": decision_deadline_epoch,
            "committee_block": committee_block,
            # v2 signed digest fields — peers must reconstruct the exact same
            # EIP-712 tuple to verify signatures, so propagate these.
            "commit_hash": getattr(proposal, "commit_hash", None),
            "nonce": int(getattr(proposal, "nonce", 0) or 0),
            "deadline": int(getattr(proposal, "deadline", 0) or 0),
            "proposer": self.validator_id,
            # NB: every key here MUST be a field of ChampionConsensusProposalRequest.
            # The follower verifies the signature over body.model_dump() (model fields
            # only), so any extra key would be in the SIGNED canonical here but DROPPED
            # on the verify side → signer mismatch. A non-deterministic `timestamp` here
            # made the recovered signer wrong AND varying per attempt, so followers
            # rejected every champion proposal (quorum could never be reached). Removed.
        }

        # Sign the canonical JSON of the payload so peers can verify the
        # leader's identity (required when CONSENSUS_REQUIRE_SIGNED_CHAMPION_PROPOSALS=1).
        # Uses the same canonical-JSON-then-eth_sign-personal-message pattern
        # as order-consensus proposals.
        if self.private_key:
            try:
                from eth_account import Account
                from eth_account.messages import encode_defunct
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                signed = Account.sign_message(
                    encode_defunct(text=canonical),
                    private_key=self.private_key,
                )
                payload["proposer_signature"] = signed.signature.hex()
            except Exception as exc:
                logger.warning("Failed to sign champion proposal: %s", exc)

        return payload

    async def _send_proposal(
        self,
        peer: PeerEndpoint,
        payload: dict[str, Any],
    ) -> SignedApproval | None:
        """Send a proposal to a single peer and parse the approval response."""
        url = f"{peer.url.rstrip('/')}/consensus/proposal"
        print(f"[CONSENSUS] Sending proposal to {peer.validator_id[:10]}... at {url}", flush=True)
        try:
            async with self._session.post(  # type: ignore[union-attr]
                url,
                json=payload,
                headers=self._request_headers(),
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                print(f"[CONSENSUS] Response from {peer.validator_id[:10]}...: HTTP {resp.status}", flush=True)
                from minotaur_subnet.consensus.dissent import (
                    RejectionCode, record_dissent,
                )
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[CONSENSUS] Rejection body: {body[:200]}", flush=True)
                    logger.warning(
                        "Peer %s rejected proposal (HTTP %d): %s",
                        peer.validator_id[:10], resp.status, body[:200],
                    )
                    # Try to parse a JSON body for reason_code; fall back to
                    # a code derived from the HTTP status.
                    code: RejectionCode = RejectionCode.UNKNOWN
                    reason_text = body[:200]
                    try:
                        import json as _json
                        parsed = _json.loads(body)
                        if isinstance(parsed, dict):
                            reason_text = parsed.get("reason") or parsed.get("error") or reason_text
                            if parsed.get("reason_code"):
                                code = RejectionCode(parsed["reason_code"])
                    except Exception:
                        pass
                    if code is RejectionCode.UNKNOWN:
                        if resp.status == 401 or resp.status == 403:
                            code = RejectionCode.UNAUTHENTICATED
                        elif resp.status == 400:
                            code = RejectionCode.MALFORMED_PAYLOAD
                    record_dissent(
                        peer_id=peer.validator_id,
                        code=code,
                        subject_kind="order",
                        subject_id=str(payload.get("order_id", "")),
                        reason=reason_text,
                    )
                    return None

                data = await resp.json()
                if not data.get("approved"):
                    logger.info(
                        "Peer %s declined: %s",
                        peer.validator_id[:10], data.get("reason", "unknown"),
                    )
                    record_dissent(
                        peer_id=peer.validator_id,
                        code=data.get("reason_code") or RejectionCode.UNKNOWN,
                        subject_kind="order",
                        subject_id=str(payload.get("order_id", "")),
                        reason=str(data.get("reason", "")),
                    )
                    return None

                approval = SignedApproval(
                    validator_id=data["validator_id"],
                    order_id=data["order_id"],
                    plan_hash=data["plan_hash"],
                    score=data["score"],
                    signature=data["signature"],
                    timestamp=data.get("timestamp", time.time()),
                )
                print(f"[CONSENSUS] Got approval from {approval.validator_id[:10]}... score={approval.score}", flush=True)
                return approval

        except asyncio.TimeoutError:
            logger.warning("Proposal to %s timed out", peer.validator_id[:10])
            from minotaur_subnet.consensus.dissent import (
                RejectionCode, record_dissent,
            )
            record_dissent(
                peer_id=peer.validator_id,
                code=RejectionCode.TIMEOUT,
                subject_kind="order",
                subject_id=str(payload.get("order_id", "")),
                reason="HTTP timeout",
            )
            return None
        except aiohttp.ClientError as exc:
            logger.warning("Proposal to %s failed: %s", peer.validator_id[:10], exc)
            from minotaur_subnet.consensus.dissent import (
                RejectionCode, record_dissent,
            )
            record_dissent(
                peer_id=peer.validator_id,
                code=RejectionCode.NETWORK_ERROR,
                subject_kind="order",
                subject_id=str(payload.get("order_id", "")),
                reason=str(exc)[:200],
            )
            return None

    async def _send_champion_proposal(
        self,
        peer: PeerEndpoint,
        payload: dict[str, Any],
        *,
        path: str,
        dissent_sink: list[str] | None = None,
        request_timeout: float | None = None,
    ) -> ChampionApproval | None:
        """Send a champion certification proposal to a single peer.

        ``dissent_sink``: when provided, the peer's rejection ``reason_code`` is
        appended on a dissent so the broadcaster can tell whether the fleet
        rejected because their round is in the wrong state (aborted).
        """
        url = f"{peer.url.rstrip('/')}/{path.lstrip('/')}"
        try:
            _to = (
                aiohttp.ClientTimeout(total=request_timeout)
                if request_timeout is not None else None
            )
            async with self._session.post(  # type: ignore[union-attr]
                url,
                json=payload,
                headers=self._request_headers(),
                **({"timeout": _to} if _to is not None else {}),
            ) as resp:
                from minotaur_subnet.consensus.dissent import (
                    RejectionCode, record_dissent,
                )
                round_id_for_log = str(payload.get("round_id", ""))
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Peer %s rejected champion proposal (HTTP %d): %s",
                        peer.validator_id[:10], resp.status, body[:200],
                    )
                    code: RejectionCode = RejectionCode.UNKNOWN
                    reason_text = body[:200]
                    try:
                        import json as _json
                        parsed = _json.loads(body)
                        if isinstance(parsed, dict):
                            reason_text = parsed.get("reason") or parsed.get("error") or reason_text
                            if parsed.get("reason_code"):
                                code = RejectionCode(parsed["reason_code"])
                    except Exception:
                        pass
                    if code is RejectionCode.UNKNOWN:
                        if resp.status in (401, 403):
                            code = RejectionCode.UNAUTHENTICATED
                        elif resp.status == 400:
                            code = RejectionCode.MALFORMED_PAYLOAD
                    record_dissent(
                        peer_id=peer.validator_id,
                        code=code,
                        subject_kind="round",
                        subject_id=round_id_for_log,
                        reason=reason_text,
                    )
                    if dissent_sink is not None:
                        dissent_sink.append(str(getattr(code, "value", code)))
                    return None

                data = await resp.json()
                if not data.get("approved"):
                    logger.debug(
                        "Peer %s declined champion certification: %s",
                        peer.validator_id[:10], data.get("reason", "unknown"),
                    )
                    _declined_code = data.get("reason_code") or RejectionCode.UNKNOWN
                    record_dissent(
                        peer_id=peer.validator_id,
                        code=_declined_code,
                        subject_kind="round",
                        subject_id=round_id_for_log,
                        reason=str(data.get("reason", "")),
                    )
                    if dissent_sink is not None:
                        dissent_sink.append(str(getattr(_declined_code, "value", _declined_code)))
                    return None

                return ChampionApproval(
                    validator_id=data["validator_id"],
                    round_id=data["round_id"],
                    committee_hash=data.get("committee_hash"),
                    incumbent_image_id=data.get("incumbent_image_id"),
                    candidate_submission_id=data.get("candidate_submission_id"),
                    candidate_image_id=data.get("candidate_image_id"),
                    benchmark_pack_hash=data.get("benchmark_pack_hash"),
                    shadow_case_log_hash=data.get("shadow_case_log_hash"),
                    effective_epoch=data.get("effective_epoch", 0),
                    # v2 signed fields — must round-trip unchanged so the
                    # leader's verify_approval rebuilds the same digest.
                    commit_hash=data.get("commit_hash"),
                    nonce=int(data.get("nonce") or 0),
                    deadline=int(data.get("deadline") or 0),
                    timestamp=data.get("timestamp", time.time()),
                    signature=data.get("signature", ""),
                )

        except asyncio.TimeoutError:
            logger.warning(
                "Champion proposal to %s timed out",
                peer.validator_id[:10],
            )
            from minotaur_subnet.consensus.dissent import (
                RejectionCode, record_dissent,
            )
            record_dissent(
                peer_id=peer.validator_id,
                code=RejectionCode.TIMEOUT,
                subject_kind="round",
                subject_id=str(payload.get("round_id", "")),
                reason="HTTP timeout",
            )
            return None
        except aiohttp.ClientError as exc:
            logger.warning(
                "Champion proposal to %s failed: %s",
                peer.validator_id[:10],
                exc,
            )
            from minotaur_subnet.consensus.dissent import (
                RejectionCode, record_dissent,
            )
            record_dissent(
                peer_id=peer.validator_id,
                code=RejectionCode.NETWORK_ERROR,
                subject_kind="round",
                subject_id=str(payload.get("round_id", "")),
                reason=str(exc)[:200],
            )
            return None

    async def _send_json(
        self,
        peer: PeerEndpoint,
        payload: dict[str, Any],
        *,
        path: str,
    ) -> dict[str, Any] | None:
        """Send a generic JSON payload to a peer validator."""
        url = f"{peer.url.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with self._session.post(  # type: ignore[union-attr]
                url,
                json=payload,
                headers=self._request_headers(),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Peer %s rejected %s (HTTP %d): %s",
                        peer.validator_id[:10], path, resp.status, body[:200],
                    )
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning("JSON sync %s to %s timed out", path, peer.validator_id[:10])
            return None
        except aiohttp.ClientError as exc:
            logger.warning("JSON sync %s to %s failed: %s", path, peer.validator_id[:10], exc)
            return None

    def _request_headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        """Build request headers for peer traffic."""
        headers = {"Content-Type": "application/json"}
        headers.update(self._default_headers)
        if extra_headers:
            headers.update(extra_headers)
        return headers


def parse_peers_env(peers_str: str) -> list[PeerEndpoint]:
    """Parse the ``addr@url,addr@url,...`` peer-list format.

    Used to interpret the ``ORDER_CONSENSUS_PEERS`` env var (named manual
    override for tests + local-testnet). Production code relies on
    ``ProtocolConfig.refresh_loop`` discovery and leaves the override unset.

    Example:
        ``0xAbC1@http://host1:9100,0xDeF2@http://host2:9100``
    """
    if not peers_str.strip():
        return []

    endpoints: list[PeerEndpoint] = []
    for entry in peers_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "@" not in entry:
            logger.warning("Invalid peer entry (missing @): %s", entry)
            continue
        addr, url = entry.split("@", 1)
        endpoints.append(PeerEndpoint(validator_id=addr.strip(), url=url.strip()))

    return endpoints
