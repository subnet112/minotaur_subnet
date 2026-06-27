"""Relayer HTTP service — receives validator signatures and submits transactions.

Provides endpoints for validators to submit signatures, trigger deployments,
and check transaction status. Runs as a standalone aiohttp service.

Start:
    python -m minotaur_subnet.relayer.main --port 8091
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aiohttp import web

from minotaur_subnet.consensus.leader_wrapper import (
    WrapperPayload,
    compute_champion_finalize_hash,
    compute_deploy_hash,
    is_wrapper_fresh,
    recover_wrapper_signer,
)
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.score_threshold_cache import score_threshold_for
from minotaur_subnet.consensus.signatures import hash_plan, verify_plan_approval
from minotaur_subnet.relayer.chain_config import get_supported_chains
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.safeguards import Safeguards
from minotaur_subnet.relayer.gas_manager import GasManager
from minotaur_subnet.relayer.validator_sync import ValidatorSync
from minotaur_subnet.shared.types import (
    ConsensusResult,
    ExecutionPlan,
    Interaction,
    SignedApproval,
)

logger = logging.getLogger(__name__)


def _read_authorized_validators(rpc_url: str, registry_address: str) -> list[str]:
    """Read the current authorized-validator set from ValidatorRegistry on chain."""
    from web3 import Web3
    abi = [{
        "name": "getValidators",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    }]
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=abi,
    )
    return [str(a) for a in registry.functions.getValidators().call()]


def _rehydrate_plan(plan_data: dict) -> ExecutionPlan:
    """Rebuild ExecutionPlan + nested Interaction list from JSON dict."""
    interactions_data = plan_data.get("interactions", [])
    interactions = [
        Interaction(
            target=i["target"],
            value=str(i.get("value", "0")),
            call_data=i["call_data"],
            chain_id=int(i.get("chain_id", 0)),
        )
        for i in interactions_data
    ]
    return ExecutionPlan(
        intent_id=plan_data.get("intent_id", ""),
        interactions=interactions,
        deadline=int(plan_data.get("deadline", 0)),
        nonce=int(plan_data.get("nonce", 0)),
        metadata=plan_data.get("metadata", {}) or {},
    )


def _rehydrate_consensus_result(consensus_data: dict) -> ConsensusResult:
    """Rebuild ConsensusResult + nested SignedApproval list from JSON dict."""
    approvals_data = consensus_data.get("approvals", []) if consensus_data else []
    approvals = [
        SignedApproval(
            validator_id=a["validator_id"],
            order_id=a.get("order_id", ""),
            plan_hash=a["plan_hash"],
            score=float(a.get("score", 0.0)),
            signature=a["signature"],
            timestamp=float(a.get("timestamp", 0.0)),
        )
        for a in approvals_data
    ]
    return ConsensusResult(
        reached=bool(consensus_data.get("reached", False)) if consensus_data else False,
        approvals=approvals,
        quorum=int(consensus_data.get("quorum", 1)) if consensus_data else 1,
        collected=int(consensus_data.get("collected", 0)) if consensus_data else 0,
        combined_score=float(consensus_data.get("combined_score", 0.0)) if consensus_data else 0.0,
    )


class _LightOrder:
    """Minimal order-shaped object for ``EvmRelayer.submit_plan``.

    The relayer reads a few attributes off the order — chain_id, order_id,
    user_signature, params. We don't need the full ``Order`` dataclass
    (with status, deadlines, perpetual flags) on the relayer side.
    """

    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class RelayerService:
    """HTTP service for the relayer."""

    def __init__(self) -> None:
        self.chains = get_supported_chains()
        self.relayer = EvmRelayer(
            chains=self.chains,
            private_key=os.environ.get("RELAYER_PRIVATE_KEY", ""),
        )

        # Load canonical quorum from the primary chain's ValidatorRegistry.
        # The relayer is single-chain at signature-collection time (orders are
        # scoped to a chain), but we read from one registry at startup because
        # the network-wide value is the same across chains by convention.
        primary_chain_id = int(os.environ.get("CHAIN_ID", "31337"))
        primary = self.chains.get(primary_chain_id)
        if primary is None or not primary.validator_registry_address:
            raise RuntimeError(
                f"No ValidatorRegistry configured for chain {primary_chain_id}; "
                "relayer cannot load ProtocolConfig"
            )
        self.protocol_config = ProtocolConfig.from_validator_registry(
            rpc_url=primary.rpc_url,
            registry_address=primary.validator_registry_address,
            # The relayer's quorum source IS the primary chain's
            # ValidatorRegistry — pass it explicitly (no silent fallback
            # inside from_validator_registry).
            quorum_address=primary.validator_registry_address,
        )
        # SignatureCollector removed in H3 audit fix; sig collection happens
        # at the api leader, the relayer only verifies pre-formed quorum
        # bundles via /v1/submit-plan.
        self.gas_manager = GasManager(chains=self.chains)
        self.validator_sync = ValidatorSync(chains=self.chains)
        self.safeguards = Safeguards.from_env()

    # H3 (2026-05-25 audit): the legacy ``POST /signatures`` handler used
    # to live here. It accepted individual validator EIP-712 sigs and
    # auto-submitted on quorum, bypassing ALL of the Phase C/D safeguards
    # (no wrapper sig, no monotonic nonce, no rate limit, no gas cap,
    # no plan-hash dedup). Anyone who could observe a quorum's worth of
    # validator approvals on the consensus mesh could replay them through
    # this back door and force a gas-burning submission.
    #
    # Deleted. The authoritative submission path is ``POST /v1/submit-plan``
    # — see ``handle_submit_plan`` below. It accepts a pre-formed quorum
    # bundle + wrapper sig and runs the full safeguard chain.

    async def handle_submit_plan(self, request: web.Request) -> web.Response:
        """POST /v1/submit-plan — submit a fully-signed quorum bundle.

        Body shape (see http_relayer.py for the client side):

            {
              "order":              {chain_id, order_id, user_signature, params, ...},
              "plan":               {intent_id, interactions[...], deadline, nonce, metadata},
              "score":              float,
              "consensus_result":   {reached, approvals[...], quorum, collected, ...},
              "contract_address":   "0x..." | null
            }

        Verifies each approval's EIP-712 signature, confirms each signer
        is in the on-chain ``ValidatorRegistry``, and that the count
        meets ``quorumBps()`` — only then does the relayer spend gas.
        Returns the ``SubmitResult`` from the embedded ``EvmRelayer``.
        """
        try:
            data = await request.json()
        except Exception as exc:
            return web.json_response({"success": False, "error": f"bad JSON: {exc}"}, status=400)

        order_data = data.get("order") or {}
        plan_data = data.get("plan") or {}
        consensus_data = data.get("consensus_result") or {}
        score = float(data.get("score", 0.0))
        contract_address_override = data.get("contract_address")

        chain_id = int(order_data.get("chain_id", 0) or 0)
        if chain_id == 0:
            return web.json_response(
                {"success": False, "error": "order.chain_id required"},
                status=400,
            )

        chain_cfg = self.chains.get(chain_id)
        if chain_cfg is None:
            return web.json_response(
                {"success": False, "error": f"unsupported chain_id {chain_id}"},
                status=400,
            )

        # Rehydrate dataclasses
        try:
            plan = _rehydrate_plan(plan_data)
            consensus_result = _rehydrate_consensus_result(consensus_data)
        except Exception as exc:
            return web.json_response(
                {"success": False, "error": f"malformed plan/consensus_result: {exc}"},
                status=400,
            )

        if not consensus_result.approvals:
            return web.json_response(
                {"success": False, "error": "no approvals in consensus_result"},
                status=400,
            )

        # ── Safeguard 1: deadline check ─────────────────────────────────
        # Cheapest possible check, no state, no RPC.
        ok, err = self.safeguards.check_deadline(plan.deadline)
        if not ok:
            return web.json_response({"success": False, "error": err}, status=400)

        # ── Safeguard 2: daily gas cap precheck ────────────────────────
        # If we've already burned the daily budget, refuse all new submissions.
        # Doesn't change state; just bails before sig recovery.
        ok, err = self.safeguards.check_daily_gas_room()
        if not ok:
            return web.json_response({"success": False, "error": err}, status=429)

        # Verify plan hash matches what each approval claims to have signed
        expected_plan_hash = hash_plan(plan)
        for ap in consensus_result.approvals:
            if ap.plan_hash != expected_plan_hash:
                return web.json_response(
                    {
                        "success": False,
                        "error": (
                            f"plan_hash mismatch: approval from {ap.validator_id[:10]} "
                            f"claims {ap.plan_hash[:18]}... but plan hashes to "
                            f"{expected_plan_hash[:18]}..."
                        ),
                    },
                    status=400,
                )

        # ── Safeguard 3: leader-signed wrapper verification ────────────
        # The api includes a wrapper {plan_hash, submission_nonce, timestamp,
        # chain_id} signed by its VALIDATOR_PRIVATE_KEY. The signer must be a
        # registered validator on the target chain. Wrapper freshness +
        # monotonic-nonce-per-signer give cryptographic replay protection
        # even if the plan_hash dedup cache is wiped (e.g., relayer restart).
        wrapper_data = data.get("wrapper")
        wrapper_sig = data.get("wrapper_signature")
        if not wrapper_data or not wrapper_sig:
            return web.json_response(
                {
                    "success": False,
                    "error": (
                        "missing wrapper or wrapper_signature in payload — the api "
                        "must sign a freshness wrapper around the bundle before "
                        "submitting (see consensus.leader_wrapper.sign_wrapper)"
                    ),
                },
                status=400,
            )

        try:
            wrapper = WrapperPayload(
                plan_hash=wrapper_data["plan_hash"],
                submission_nonce=int(wrapper_data["submission_nonce"]),
                timestamp=int(wrapper_data["timestamp"]),
                chain_id=int(wrapper_data["chain_id"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response(
                {"success": False, "error": f"malformed wrapper: {exc}"},
                status=400,
            )

        # Wrapper binds the same plan + chain we're submitting.
        if wrapper.plan_hash != expected_plan_hash:
            return web.json_response(
                {"success": False, "error": "wrapper plan_hash doesn't match plan"},
                status=400,
            )
        if wrapper.chain_id != chain_id:
            return web.json_response(
                {"success": False, "error": "wrapper chain_id doesn't match order"},
                status=400,
            )

        ok, err = is_wrapper_fresh(wrapper)
        if not ok:
            return web.json_response({"success": False, "error": err}, status=400)

        try:
            wrapper_signer = recover_wrapper_signer(wrapper, wrapper_sig)
        except Exception as exc:
            return web.json_response(
                {"success": False, "error": f"wrapper sig invalid: {exc}"},
                status=400,
            )

        # Read the authorized validator set from on-chain ValidatorRegistry.
        # We deliberately read fresh per-request rather than from a cache:
        # this is cheap (single eth_call) compared to the broadcast tx
        # that follows, and stale-cache rejections are far worse than the
        # extra RPC hop.
        if not chain_cfg.validator_registry_address:
            return web.json_response(
                {
                    "success": False,
                    "error": f"no ValidatorRegistry configured on chain {chain_id}",
                },
                status=500,
            )
        try:
            authorized = _read_authorized_validators(
                chain_cfg.rpc_url,
                chain_cfg.validator_registry_address,
            )
        except Exception as exc:
            return web.json_response(
                {"success": False, "error": f"ValidatorRegistry read failed: {exc}"},
                status=502,
            )
        authorized_lower = {addr.lower() for addr in authorized}

        # ── Safeguard 4: wrapper signer must be a registered validator ─
        if wrapper_signer.lower() not in authorized_lower:
            return web.json_response(
                {
                    "success": False,
                    "error": (
                        f"wrapper signer {wrapper_signer} not in ValidatorRegistry on "
                        f"chain {chain_id} — only registered validators can submit"
                    ),
                },
                status=403,
            )

        # ── Safeguard 5: monotonic nonce per signer ─────────────────────
        ok, err = self.safeguards.check_signer_nonce(wrapper_signer, wrapper.submission_nonce)
        if not ok:
            return web.json_response({"success": False, "error": err}, status=409)

        # ── Safeguard 6: per-caller rate limit ─────────────────────────
        ok, err = self.safeguards.check_caller_rate(wrapper_signer)
        if not ok:
            return web.json_response({"success": False, "error": err}, status=429)

        # Determine the contract_address that the signatures bind to.
        # Same precedence the EvmRelayer uses internally — explicit
        # override beats per-app default beats chain-level zero address.
        contract_for_domain = (
            (contract_address_override or "").strip()
            or order_data.get("contract_address", "")
            or ""
        )

        # Validators sign with score_bps = the App's on-chain scoreThreshold
        # (see ConsensusManager.sign_approval + score_threshold_cache), NOT
        # int(score*10000). The on-chain verifier reconstructs the digest
        # the same way. We have to mirror that here; otherwise every signature
        # in the bundle fails recovery and submit-plan rejects orders that
        # already cleared consensus. Falls back to the 5000-bps floor when the
        # contract is unreachable, which still matches what the signers used.
        threshold_bps = score_threshold_for(contract_for_domain, chain_id)

        # Verify each signature: recover signer + check membership.
        # Tracks unique signers (one approval per validator counts once).
        verified_signers: set[str] = set()
        for ap in consensus_result.approvals:
            if ap.validator_id.lower() not in authorized_lower:
                return web.json_response(
                    {
                        "success": False,
                        "error": (
                            f"signer {ap.validator_id} not in ValidatorRegistry on "
                            f"chain {chain_id}"
                        ),
                    },
                    status=400,
                )
            ok = verify_plan_approval(
                public_key=ap.validator_id,
                signature=ap.signature,
                order_id=ap.order_id,
                plan_hash=ap.plan_hash,
                score=ap.score,
                chain_id=chain_id,
                contract_address=contract_for_domain or ("0x" + "00" * 20),
                score_bps=threshold_bps,
            )
            if not ok:
                return web.json_response(
                    {
                        "success": False,
                        "error": f"invalid EIP-712 signature from {ap.validator_id}",
                    },
                    status=400,
                )
            verified_signers.add(ap.validator_id.lower())

        # Quorum check — count of unique verified signers against the
        # on-chain quorumBps applied to total validator count.
        quorum_bps = self.protocol_config.quorum_bps
        total_validators = len(authorized)
        quorum_required = max(1, (total_validators * quorum_bps + 9999) // 10000)
        if len(verified_signers) < quorum_required:
            return web.json_response(
                {
                    "success": False,
                    "error": (
                        f"insufficient quorum: {len(verified_signers)} verified "
                        f"signers, need {quorum_required} of {total_validators} "
                        f"(quorum_bps={quorum_bps})"
                    ),
                },
                status=400,
            )

        # ── Safeguard 7: plan-hash dedup (committed last, after all
        # cheaper checks — so we don't waste a slot on a request that
        # would have failed verification anyway). ────────────────────
        ok, err = self.safeguards.check_plan_hash_unseen(expected_plan_hash, plan.deadline)
        if not ok:
            return web.json_response({"success": False, "error": err}, status=409)

        logger.info(
            "Relayer: submit-plan accepted (order=%s chain=%d signers=%d/%d required=%d "
            "wrapper-signer=%s nonce=%d)",
            order_data.get("order_id", "")[:12],
            chain_id,
            len(verified_signers),
            total_validators,
            quorum_required,
            wrapper_signer[:10],
            wrapper.submission_nonce,
        )

        # Wrap order_data as a light attribute-bag so EvmRelayer's
        # downstream encoder + verifier can read the order fields off
        # it. The encoder needs more than just the user-signing surface:
        # it builds the on-chain ``Order`` struct from ``submitted_by``,
        # ``deadline``, ``perpetual``, ``max_executions``, and
        # ``cooldown`` too. Missing any of these raised
        # ``AttributeError: '_LightOrder' object has no attribute 'X'``
        # at submit time — caught live on prod 2026-05-27 after
        # consensus was reached (4-of-4 sigs) but the relayer crashed
        # on encoding. The api already includes these fields in the
        # POST payload via ``_to_jsonable(order)``; we just weren't
        # pulling them out here.
        light_order = _LightOrder(
            chain_id=chain_id,
            order_id=order_data.get("order_id", ""),
            user_signature=order_data.get("user_signature", ""),
            params=order_data.get("params", {}) or {},
            submitted_by=order_data.get("submitted_by", ""),
            deadline=order_data.get("deadline", 0),
            perpetual=bool(order_data.get("perpetual", False)),
            max_executions=int(order_data.get("max_executions", 1)),
            cooldown=float(order_data.get("cooldown", 0.0)),
        )

        try:
            submit_result = await self.relayer.submit_plan(
                light_order,
                plan,
                score,
                consensus_result,
                contract_address=contract_address_override,
            )
        except Exception as exc:
            logger.exception("Relayer: submit_plan crashed for order=%s", order_data.get("order_id", "")[:12])
            return web.json_response(
                {"success": False, "error": f"submit_plan crashed: {exc}"},
                status=500,
            )

        # Charge against the daily gas budget. Only on a successful submit.
        if submit_result.success and submit_result.gas_used:
            # Approximate gas price — Base mainnet typically 0.001-0.1 gwei.
            # The Web3 instance inside EvmRelayer has the actual value, but
            # we'd need to thread it through SubmitResult. For now, charge
            # a conservative 0.05 gwei × gas_used per submission.
            approx_gas_price_wei = 50_000_000  # 0.05 gwei
            self.safeguards.record_gas_used(
                gas_used=int(submit_result.gas_used),
                gas_price_wei=approx_gas_price_wei,
            )

        return web.json_response({
            "success": submit_result.success,
            "tx_hash": submit_result.tx_hash,
            "chain_id": submit_result.chain_id,
            "block_number": submit_result.block_number,
            "gas_used": submit_result.gas_used,
            "error": submit_result.error,
        })

    async def handle_finalize_champion(self, request: web.Request) -> web.Response:
        """POST /v1/finalize-champion — attest + squash-merge a certified champion.

        Champion FINALIZATION (the on-chain ``ChampionRegistry.certify()`` tx +
        the GitHub PR squash-merge) lives HERE, in the trusted relayer that holds
        ``RELAYER_PRIVATE_KEY`` + ``SOLVER_REPO_TOKEN`` — NOT on the leader, which
        may be a third party we don't control. The leader asks us to finalize and
        gates its local adoption on our boolean reply (see
        ``solver_repo.on_champion_adopted_via_relayer`` + the #326 adoption gate in
        ``epoch.manager.activate_certified_round``).

        The relayer does NOT trust the leader: it independently re-verifies the
        validator quorum on the ``ChampionCertificate`` before spending gas /
        merging — exactly mirroring ``handle_submit_plan``'s security model:

          1. Each approval's EIP-712 signature is verified against the champion
             DOMAIN_SEPARATOR (``CHAMPION_REGISTRY_964`` on the champion chain).
          2. Each verified signer must be in the on-chain ``ValidatorRegistry``
             (the BT-EVM / chain-964 registry the cert was signed against).
          3. The count of distinct authorized verified signers must meet
             ``cert.quorum_required`` (and quorum_required >= 1).
          4. A leader-signed wrapper (over ``round_id + candidate_submission_id``)
             whose recovered signer is an authorized validator gates the caller
             (anti-spam; the certificate quorum is the real authority).

        Body shape (see ``solver_repo.on_champion_adopted_via_relayer``):

            {
              "certificate": <ChampionCertificate.to_dict()>,
              "submission":  {"submission_id", "commit_hash", "pr_number"},
              "round_id":    str,
              "wrapper":     {plan_hash, submission_nonce, timestamp, chain_id},
              "wrapper_signature": "0x..."
            }

        FAIL-CLOSED: on quorum miss or ANY error this returns HTTP 200 with
        ``{"merge_ok": false, "reason": ...}`` and never attests/merges — the
        leader's #326 gate then aborts the round (``merge_failed``) and the
        champion is left unchanged. We never 500 the leader.
        """
        import types

        from minotaur_subnet.consensus.champion_manager import (
            ChampionProposal,
            verify_champion_approval,
        )
        from minotaur_subnet.consensus.eip712 import build_domain_separator
        from minotaur_subnet.harness.round_store import ChampionCertificate
        from minotaur_subnet.relayer.solver_repo import on_champion_adopted_pr

        try:
            data = await request.json()
        except Exception as exc:
            return web.json_response(
                {"merge_ok": False, "reason": f"bad JSON: {exc}"}, status=200,
            )

        try:
            round_id = str(data.get("round_id") or "").strip()
            cert = ChampionCertificate.from_dict(data.get("certificate"))
            if cert is None:
                return web.json_response(
                    {"merge_ok": False, "reason": "missing certificate"}, status=200,
                )
            if not cert.approvals:
                return web.json_response(
                    {"merge_ok": False, "reason": "certificate has no approvals"},
                    status=200,
                )

            quorum_required = int(cert.quorum_required or 0)
            if quorum_required < 1:
                return web.json_response(
                    {
                        "merge_ok": False,
                        "reason": f"invalid quorum_required {quorum_required} (< 1)",
                    },
                    status=200,
                )

            # ── Champion DOMAIN_SEPARATOR ──────────────────────────────────
            # Recompute the SAME separator the validators signed with — the
            # ChampionConsensusManager builds it as build_domain_separator(
            # champion_chain_id, CHAMPION_REGISTRY_<chain>, "MinotaurChampionConsensus",
            # "1") (see api/startup.py + consensus/champion_manager.py). Keep this
            # in lock-step with that wiring.
            champion_chain_id = int(
                os.environ.get("CHAMPION_CONSENSUS_CHAIN_ID", "964").strip() or "964"
            )
            champion_registry_address = (
                os.environ.get(f"CHAMPION_REGISTRY_{champion_chain_id}", "").strip()
                or os.environ.get("CHAMPION_CONSENSUS_CONTRACT_ADDRESS", "").strip()
            )
            if not champion_registry_address:
                return web.json_response(
                    {
                        "merge_ok": False,
                        "reason": (
                            f"no CHAMPION_REGISTRY_{champion_chain_id} configured — "
                            "cannot recompute champion domain separator"
                        ),
                    },
                    status=200,
                )
            domain_separator = build_domain_separator(
                champion_chain_id,
                champion_registry_address,
                name="MinotaurChampionConsensus",
                version="1",
            )

            # ── Verify each approval's EIP-712 signature ───────────────────
            # Distinct signers whose signature verifies against the champion
            # tuple. One approval per validator counts once.
            verified_signers: set[str] = set()
            for ap in cert.approvals:
                proposal = ChampionProposal(
                    round_id=ap.round_id,
                    committee_hash=ap.committee_hash,
                    incumbent_image_id=ap.incumbent_image_id,
                    candidate_submission_id=ap.candidate_submission_id or "",
                    candidate_image_id=ap.candidate_image_id or "",
                    benchmark_pack_hash=ap.benchmark_pack_hash,
                    shadow_case_log_hash=ap.shadow_case_log_hash,
                    effective_epoch=int(ap.effective_epoch or 0),
                    commit_hash=ap.commit_hash,
                    nonce=int(ap.nonce or 0),
                    deadline=int(ap.deadline or 0),
                )
                if verify_champion_approval(
                    ap.validator_id,
                    ap.signature,
                    proposal,
                    domain_separator=domain_separator,
                ):
                    verified_signers.add(ap.validator_id.lower())

            # ── Authorized-validator membership (on-chain) ─────────────────
            # The cert is signed against the BT-EVM (champion-chain) ValidatorRegistry.
            chain_cfg = self.chains.get(champion_chain_id)
            if chain_cfg is None or not chain_cfg.validator_registry_address:
                return web.json_response(
                    {
                        "merge_ok": False,
                        "reason": (
                            f"no ValidatorRegistry configured on champion chain "
                            f"{champion_chain_id}"
                        ),
                    },
                    status=200,
                )
            try:
                authorized = _read_authorized_validators(
                    chain_cfg.rpc_url,
                    chain_cfg.validator_registry_address,
                )
            except Exception as exc:
                return web.json_response(
                    {"merge_ok": False, "reason": f"ValidatorRegistry read failed: {exc}"},
                    status=200,
                )
            authorized_lower = {addr.lower() for addr in authorized}
            authorized_verified = verified_signers & authorized_lower

            # ── Leader wrapper (anti-spam): caller must be a validator ─────
            # Binds round_id + candidate_submission_id (repurposing the wrapper's
            # bytes32 plan_hash field, same pattern as compute_deploy_hash). The
            # certificate quorum below is the real authority.
            wrapper_data = data.get("wrapper")
            wrapper_sig = data.get("wrapper_signature")
            if not wrapper_data or not wrapper_sig:
                return web.json_response(
                    {"merge_ok": False, "reason": "missing wrapper or wrapper_signature"},
                    status=200,
                )
            try:
                wrapper = WrapperPayload(
                    plan_hash=wrapper_data["plan_hash"],
                    submission_nonce=int(wrapper_data["submission_nonce"]),
                    timestamp=int(wrapper_data["timestamp"]),
                    chain_id=int(wrapper_data["chain_id"]),
                )
            except (KeyError, ValueError, TypeError) as exc:
                return web.json_response(
                    {"merge_ok": False, "reason": f"malformed wrapper: {exc}"},
                    status=200,
                )
            expected_wrapper_hash = compute_champion_finalize_hash(
                round_id, cert.candidate_submission_id or "",
            )
            if wrapper.plan_hash != expected_wrapper_hash:
                return web.json_response(
                    {
                        "merge_ok": False,
                        "reason": "wrapper plan_hash doesn't bind round+submission",
                    },
                    status=200,
                )
            ok, err = is_wrapper_fresh(wrapper)
            if not ok:
                return web.json_response({"merge_ok": False, "reason": err}, status=200)
            try:
                wrapper_signer = recover_wrapper_signer(wrapper, wrapper_sig)
            except Exception as exc:
                return web.json_response(
                    {"merge_ok": False, "reason": f"wrapper sig invalid: {exc}"},
                    status=200,
                )
            if wrapper_signer.lower() not in authorized_lower:
                return web.json_response(
                    {
                        "merge_ok": False,
                        "reason": (
                            f"wrapper signer {wrapper_signer} not in ValidatorRegistry "
                            f"on champion chain {champion_chain_id}"
                        ),
                    },
                    status=200,
                )

            # ── Quorum gate (THE authority) ────────────────────────────────
            if len(authorized_verified) < quorum_required:
                reason = (
                    f"quorum not reached: {len(authorized_verified)}/{quorum_required} "
                    f"authorized verified signers (of {len(authorized)} validators)"
                )
                logger.warning("Relayer: finalize-champion REFUSED (%s) round=%s", reason, round_id)
                return web.json_response(
                    {"merge_ok": False, "reason": reason, "round_id": round_id},
                    status=200,
                )

            # ── Quorum OK → attest on-chain + squash-merge ─────────────────
            submission_data = data.get("submission") or {}
            _is_private = bool(submission_data.get("is_private", False))
            ns = types.SimpleNamespace(
                submission_id=submission_data.get("submission_id", ""),
                commit_hash=submission_data.get("commit_hash", ""),
                pr_number=submission_data.get("pr_number"),
                is_private=_is_private,
                private_repo=submission_data.get("private_repo"),
                repo_token=submission_data.get("repo_token"),
            )
            logger.info(
                "Relayer: finalize-champion accepted (round=%s submission=%s signers=%d/%d "
                "wrapper-signer=%s) — attesting + merging",
                round_id,
                ns.submission_id,
                len(authorized_verified),
                quorum_required,
                wrapper_signer[:10],
            )
            merge_ok = bool(on_champion_adopted_pr(ns, round_id, certificate=cert))
            return web.json_response(
                {
                    "merge_ok": merge_ok,
                    "round_id": round_id,
                    "submission_id": ns.submission_id,
                },
                status=200,
            )
        except Exception as exc:
            logger.exception("Relayer: finalize-champion crashed")
            return web.json_response(
                {"merge_ok": False, "reason": f"finalize crashed: {exc}"}, status=200,
            )

    async def handle_deploy(self, request: web.Request) -> web.Response:
        """POST /deploy — deploy an App contract.

        Same wrapper-sig + safeguards path as ``/v1/submit-plan``: the caller
        must include an EIP-191 wrapper whose recovered signer is in the
        on-chain ``ValidatorRegistry`` for the target chain. The wrapper's
        ``plan_hash`` field is bound to ``compute_deploy_hash(bytecode, args)``
        — so a captured wrapper signature can't be replayed against different
        bytecode or different constructor args.

        Audit: 2026-05-25 found this endpoint fully unauthenticated. A 1-byte
        garbage contract was deployed to Base mainnet from the open relayer.
        Anyone could drain the gas wallet in a loop.
        """
        try:
            data = await request.json()
        except Exception as exc:
            return web.json_response(
                {"error": f"malformed JSON: {exc}"}, status=400,
            )

        bytecode = data.get("bytecode")
        constructor_args = data.get("constructor_args", []) or []
        chain_id_raw = data.get("chain_id")
        if not bytecode or chain_id_raw is None:
            return web.json_response(
                {"error": "bytecode + chain_id required"}, status=400,
            )
        try:
            chain_id = int(chain_id_raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"invalid chain_id: {chain_id_raw!r}"}, status=400,
            )

        chain_cfg = self.chains.get(chain_id)
        if chain_cfg is None:
            return web.json_response(
                {"error": f"unsupported chain_id {chain_id}"}, status=400,
            )

        # ── Safeguard 1: daily gas cap precheck ─────────────────────────
        ok, err = self.safeguards.check_daily_gas_room()
        if not ok:
            return web.json_response({"error": err}, status=429)

        # ── Safeguard 2: wrapper-sig verification ──────────────────────
        wrapper_data = data.get("wrapper")
        wrapper_sig = data.get("wrapper_signature")
        if not wrapper_data or not wrapper_sig:
            return web.json_response(
                {
                    "error": (
                        "missing wrapper or wrapper_signature — the caller must "
                        "sign a freshness wrapper around the deploy request "
                        "(see consensus.leader_wrapper.sign_wrapper with "
                        "plan_hash=compute_deploy_hash(bytecode, constructor_args))"
                    ),
                },
                status=400,
            )

        try:
            wrapper = WrapperPayload(
                plan_hash=wrapper_data["plan_hash"],
                submission_nonce=int(wrapper_data["submission_nonce"]),
                timestamp=int(wrapper_data["timestamp"]),
                chain_id=int(wrapper_data["chain_id"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response(
                {"error": f"malformed wrapper: {exc}"}, status=400,
            )

        try:
            expected_deploy_hash = compute_deploy_hash(bytecode, constructor_args)
        except Exception as exc:
            return web.json_response(
                {"error": f"bad bytecode/args: {exc}"}, status=400,
            )

        if wrapper.plan_hash != expected_deploy_hash:
            return web.json_response(
                {"error": "wrapper plan_hash doesn't match deploy params"},
                status=400,
            )
        if wrapper.chain_id != chain_id:
            return web.json_response(
                {"error": "wrapper chain_id doesn't match request"},
                status=400,
            )

        ok, err = is_wrapper_fresh(wrapper)
        if not ok:
            return web.json_response({"error": err}, status=400)

        try:
            wrapper_signer = recover_wrapper_signer(wrapper, wrapper_sig)
        except Exception as exc:
            return web.json_response(
                {"error": f"wrapper sig invalid: {exc}"}, status=400,
            )

        if not chain_cfg.validator_registry_address:
            return web.json_response(
                {"error": f"no ValidatorRegistry configured on chain {chain_id}"},
                status=500,
            )
        try:
            authorized = _read_authorized_validators(
                chain_cfg.rpc_url,
                chain_cfg.validator_registry_address,
            )
        except Exception as exc:
            return web.json_response(
                {"error": f"ValidatorRegistry read failed: {exc}"}, status=502,
            )
        authorized_lower = {addr.lower() for addr in authorized}

        if wrapper_signer.lower() not in authorized_lower:
            return web.json_response(
                {
                    "error": (
                        f"wrapper signer {wrapper_signer} not in ValidatorRegistry "
                        f"on chain {chain_id} — only registered validators can deploy"
                    ),
                },
                status=403,
            )

        # ── Safeguard 3: monotonic nonce ────────────────────────────────
        ok, err = self.safeguards.check_signer_nonce(wrapper_signer, wrapper.submission_nonce)
        if not ok:
            return web.json_response({"error": err}, status=409)

        # ── Safeguard 4: per-caller rate limit ─────────────────────────
        ok, err = self.safeguards.check_caller_rate(wrapper_signer)
        if not ok:
            return web.json_response({"error": err}, status=429)

        logger.info(
            "Relayer: deploy accepted (chain=%d wrapper-signer=%s nonce=%d "
            "bytecode_len=%d args=%d)",
            chain_id, wrapper_signer[:10], wrapper.submission_nonce,
            len(bytecode), len(constructor_args),
        )

        try:
            address, tx_hash = await self.relayer.deploy_contract(
                bytecode=bytecode,
                constructor_args=constructor_args,
                chain_id=chain_id,
            )
        except Exception as exc:
            logger.exception("Relayer: deploy_contract crashed on chain %d", chain_id)
            return web.json_response(
                {"error": f"deploy_contract crashed: {exc}"}, status=500,
            )

        return web.json_response({
            "status": "deployed",
            "address": address,
            "tx_hash": tx_hash,
        })

    async def handle_tx_status(self, request: web.Request) -> web.Response:
        """GET /status/{tx_hash} — check transaction status."""
        tx_hash = request.match_info["tx_hash"]
        chain_id = int(request.query.get("chain_id", "1"))
        result = await self.relayer.get_tx_status(tx_hash, chain_id)
        return web.json_response(result)

    async def handle_gas_balances(self, request: web.Request) -> web.Response:
        """GET /gas-balances — relayer wallet balances."""
        balances = await self.gas_manager.get_balances()
        return web.json_response({
            str(k): v for k, v in balances.items()
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health"""
        return web.json_response({
            "status": "ok",
            "service": "relayer",
            "chains": list(self.chains.keys()),
            "safeguards": self.safeguards.stats(),
        })


def create_app() -> web.Application:
    """Create the aiohttp application."""
    service = RelayerService()
    app = web.Application()

    # /signatures route removed in H3 audit fix; only /v1/submit-plan is
    # the canonical submission path now.
    app.router.add_post("/v1/submit-plan", service.handle_submit_plan)
    app.router.add_post("/v1/finalize-champion", service.handle_finalize_champion)
    app.router.add_post("/deploy", service.handle_deploy)
    app.router.add_get("/status/{tx_hash}", service.handle_tx_status)
    app.router.add_get("/gas-balances", service.handle_gas_balances)
    app.router.add_get("/health", service.handle_health)

    # Background refresh of ProtocolConfig so on-chain setQuorumBps changes
    # propagate without restarting the relayer.
    async def _start_protocol_refresh(_app: web.Application) -> None:
        _app["protocol_refresh"] = asyncio.create_task(
            service.protocol_config.refresh_loop()
        )

    async def _stop_protocol_refresh(_app: web.Application) -> None:
        task = _app.get("protocol_refresh")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_protocol_refresh)
    app.on_cleanup.append(_stop_protocol_refresh)

    return app


def main() -> None:
    """Run the relayer HTTP service."""
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Minotaur EVM Relayer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()

    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
