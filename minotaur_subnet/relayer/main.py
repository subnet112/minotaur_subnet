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
from minotaur_subnet.relayer.chain_config import get_supported_chains
from minotaur_subnet.relayer.evm_relayer import EvmRelayer
from minotaur_subnet.relayer.signature_collector import SignatureCollector
from minotaur_subnet.relayer.gas_manager import GasManager
from minotaur_subnet.relayer.validator_sync import ValidatorSync

logger = logging.getLogger(__name__)


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
