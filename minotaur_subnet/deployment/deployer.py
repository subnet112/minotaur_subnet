"""Deploy service — compile, encode constructor args, deploy on-chain.

Orchestrates the full deployment pipeline for App Intent contracts:
compile Solidity via Forge, ABI-encode constructor arguments for
AppIntentBase, and deploy via the EVM relayer.
"""

from __future__ import annotations

import hashlib
import os
import logging
import re
from typing import Any

from eth_abi import encode as abi_encode

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from .compiler import ForgeCompiler

logger = logging.getLogger(__name__)


def _detect_contract_name(solidity_source: str, fallback_name: str) -> str:
    """Detect the primary Solidity contract name from source."""
    match = re.search(r"\bcontract\s+(\w+)", solidity_source)
    if match:
        return match.group(1)
    return fallback_name.replace(" ", "").replace("-", "")


class DeployService:
    """Orchestrates: compile -> encode constructor args -> deploy -> result."""

    def __init__(
        self,
        compiler: ForgeCompiler,
        relayer: Any,
        registry_address: str = "",
        quorum_bps: int | None = None,
        score_threshold: int = 5000,
    ) -> None:
        self.compiler = compiler
        self.relayer = relayer
        self.registry_address = registry_address
        import os as _os
        self.quorum_bps = quorum_bps if quorum_bps is not None else int(
            _os.environ.get("QUORUM_BPS", _os.environ.get("ORDER_CONSENSUS_QUORUM_BPS", "6666"))
        )
        self.score_threshold = score_threshold

    async def deploy(
        self,
        app: AppIntentDefinition,
        chain_id: int,
    ) -> DeploymentResult:
        """Full pipeline: compile -> constructor encode -> deploy_contract().

        Active apps must provide their own Solidity source. This keeps the
        deploy path aligned with the current canonical runtime contract
        (`DexAggregatorApp` or another explicit `AppIntentBase` app) instead of
        silently falling back to the legacy `SwapApp` example contract.
        """
        js_hash = hashlib.sha256(app.js_code.encode()).hexdigest()

        # 1. Compile / extract bytecode
        has_real_solidity = (
            app.solidity_code
            and "contract " in app.solidity_code
            and "pragma solidity" in app.solidity_code
        )
        if has_real_solidity:
            contract_name = _detect_contract_name(app.solidity_code or "", app.name)
            result = self.compiler.compile(contract_name, app.solidity_code)
        else:
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=(
                    "No Solidity code provided. Active apps must provide an "
                    "explicit AppIntentBase-derived contract source."
                ),
            )

        if result.error:
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=f"Compilation failed: {result.error}",
            )

        # 2. Resolve relayer address for target chain
        relayer_addr = self._get_relayer_address(chain_id)
        if not relayer_addr:
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=f"No relayer wallet configured for chain {chain_id}",
            )

        # 3. ABI-encode constructor args
        registry_addr = self.registry_address
        if not registry_addr:
            # Fall back to chain-level registry if available
            chain_cfg = self.relayer.chains.get(chain_id)
            if chain_cfg and chain_cfg.validator_registry_address:
                registry_addr = chain_cfg.validator_registry_address

        if not registry_addr:
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=f"No ValidatorRegistry address for chain {chain_id}",
            )

        from web3 import Web3
        relayer_cs = Web3.to_checksum_address(relayer_addr)
        registry_cs = Web3.to_checksum_address(registry_addr)

        # Platform fee constructor args (AppIntentBase params 5-7)
        from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
        wnt = WRAPPED_NATIVE_TOKEN.get(chain_id, "0x" + "0" * 40)
        wnt_cs = Web3.to_checksum_address(wnt)
        platform_fee_collector = relayer_cs  # Relayer collects fees by default
        max_platform_fee = int(os.environ.get("MAX_PLATFORM_FEE_WEI", str(10**17)))  # 0.1 ETH

        arg_types = ["address", "address", "uint256", "uint256",
                     "address", "address", "uint256"]
        arg_values: list = [relayer_cs, registry_cs, self.quorum_bps, self.score_threshold,
                            wnt_cs, platform_fee_collector, max_platform_fee]
        logger.info(
            "Deploy constructor: relayer=%s registry=%s quorum=%d score_threshold=%d "
            "wrappedNative=%s feeCollector=%s maxFee=%d",
            relayer_cs[:10], registry_cs[:10], self.quorum_bps, self.score_threshold,
            wnt_cs[:10], platform_fee_collector[:10], max_platform_fee,
        )

        if app.constructor_args:
            for abi_type, value in app.constructor_args:
                arg_types.append(abi_type)
                if abi_type == "address":
                    arg_values.append(Web3.to_checksum_address(value))
                elif abi_type.startswith("uint") or abi_type.startswith("int"):
                    arg_values.append(int(value))
                else:
                    arg_values.append(value)

        encoded_args = abi_encode(arg_types, arg_values)

        full_bytecode = result.bytecode + encoded_args.hex()

        # 4. Deploy via relayer
        try:
            address, tx_hash = await self.relayer.deploy_contract(
                full_bytecode, [], chain_id,
            )
        except Exception as exc:
            logger.error("Deploy failed on chain %d: %s", chain_id, exc)
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=f"On-chain deployment failed: {exc}",
            )

        logger.info(
            "Deployed %s on chain %d at %s (tx: %s)",
            app.app_id, chain_id, address, tx_hash,
        )

        return DeploymentResult(
            app_id=app.app_id,
            status=AppStatus.SOLVING,
            contract_address=address,
            js_code_hash=js_hash,
            chain_id=chain_id,
            tx_hash=tx_hash,
            abi=result.abi,
        )

    def _get_relayer_address(self, chain_id: int) -> str:
        """Get the relayer wallet address for a chain."""
        chain_cfg = self.relayer.chains.get(chain_id)
        if chain_cfg and chain_cfg.relayer_wallet:
            return chain_cfg.relayer_wallet
        return ""
