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

from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.signatures import hash_plan, verify_plan_approval
from minotaur_subnet.relayer.chain_config import get_supported_chains
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.signature_collector import SignatureCollector
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
        )
        self.collector = SignatureCollector(
            protocol_config=self.protocol_config,
            validators=[],  # Populated by validator_sync
        )
        self.gas_manager = GasManager(chains=self.chains)
        self.validator_sync = ValidatorSync(chains=self.chains)

    async def handle_submit_signature(self, request: web.Request) -> web.Response:
        """POST /signatures — validator submits a plan approval signature."""
        try:
            data = await request.json()
            plan_hash = data["plan_hash"]
            validator_address = data["validator_address"]
            signature = bytes.fromhex(data["signature"].replace("0x", ""))
            order_id = data.get("order_id", "")
            score = data.get("score", 0.0)

            result = self.collector.add_signature(
                plan_hash=plan_hash,
                validator_address=validator_address,
                signature=signature,
                order_id=order_id,
                score=score,
            )

            if result is not None:
                # Quorum reached — submit to chain
                submit_result = await self.relayer.submit_plan(
                    result.order, result.plan, result.score,
                )
                return web.json_response({
                    "status": "submitted",
                    "tx_hash": submit_result.tx_hash,
                    "success": submit_result.success,
                })

            return web.json_response({
                "status": "collected",
                "pending": self.collector.pending_count,
            })

        except Exception as exc:
            return web.json_response(
                {"error": str(exc)}, status=400,
            )

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

        # Determine the contract_address that the signatures bind to.
        # Same precedence the EvmRelayer uses internally — explicit
        # override beats per-app default beats chain-level zero address.
        contract_for_domain = (
            (contract_address_override or "").strip()
            or order_data.get("contract_address", "")
            or ""
        )

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

        logger.info(
            "Relayer: submit-plan accepted (order=%s chain=%d signers=%d/%d required=%d)",
            order_data.get("order_id", "")[:12],
            chain_id,
            len(verified_signers),
            total_validators,
            quorum_required,
        )

        # Wrap order_data as a light attribute-bag so EvmRelayer can
        # read .chain_id / .order_id / .user_signature / .params off it.
        light_order = _LightOrder(
            chain_id=chain_id,
            order_id=order_data.get("order_id", ""),
            user_signature=order_data.get("user_signature", ""),
            params=order_data.get("params", {}) or {},
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

        return web.json_response({
            "success": submit_result.success,
            "tx_hash": submit_result.tx_hash,
            "chain_id": submit_result.chain_id,
            "block_number": submit_result.block_number,
            "gas_used": submit_result.gas_used,
            "error": submit_result.error,
        })

    async def handle_deploy(self, request: web.Request) -> web.Response:
        """POST /deploy — deploy an App contract."""
        try:
            data = await request.json()
            address, tx_hash = await self.relayer.deploy_contract(
                bytecode=data["bytecode"],
                constructor_args=data.get("constructor_args", []),
                chain_id=data["chain_id"],
            )
            return web.json_response({
                "status": "deployed",
                "address": address,
                "tx_hash": tx_hash,
            })
        except Exception as exc:
            return web.json_response(
                {"error": str(exc)}, status=400,
            )

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
        })


def create_app() -> web.Application:
    """Create the aiohttp application."""
    service = RelayerService()
    app = web.Application()

    app.router.add_post("/signatures", service.handle_submit_signature)
    app.router.add_post("/v1/submit-plan", service.handle_submit_plan)
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
