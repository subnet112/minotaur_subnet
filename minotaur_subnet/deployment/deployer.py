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


def resolve_fee_mode(app_fee_mode: str | None) -> tuple[str, int]:
    """Resolve the on-chain FeeMode for an App (#239).

    The App's own ``config.fee_mode`` wins; the operator-wide ``FEE_MODE_DEFAULT``
    is only the fallback when the App didn't choose one. Returns
    ``(mode_str, enum)`` where enum is the contract's FeeMode (USER=0, APP=1).

    Raises ``ValueError`` (with a source-attributed message) on an invalid value.
    """
    app_choice = (app_fee_mode or "").strip().upper()
    mode = app_choice or os.environ.get("FEE_MODE_DEFAULT", "USER").upper()
    if mode not in ("USER", "APP"):
        src = "App config fee_mode" if app_choice else "FEE_MODE_DEFAULT"
        raise ValueError(f"{src} must be USER or APP, got {mode!r}")
    return mode, (1 if mode == "APP" else 0)


def _detect_contract_name(solidity_source: str, fallback_name: str) -> str:
    """Detect the primary Solidity contract name from source.

    Anchored to a line-start declaration so a NatSpec comment that merely
    contains the word "contract" (e.g. "approve this contract to pull WETH")
    is not mistaken for the contract name. Prefers a contract that inherits
    (``contract X is ...``) — the deployable App — over a bare declaration.
    """
    inheriting = re.search(
        r"(?m)^\s*(?:abstract\s+)?contract\s+(\w+)\s+is\b", solidity_source,
    )
    if inheriting:
        return inheriting.group(1)
    decl = re.search(
        r"(?m)^\s*(?:abstract\s+)?contract\s+(\w+)", solidity_source,
    )
    if decl:
        return decl.group(1)
    return fallback_name.replace(" ", "").replace("-", "")


class DeployService:
    """Orchestrates: compile -> encode constructor args -> deploy -> result."""

    def __init__(
        self,
        compiler: ForgeCompiler,
        relayer: Any,
        registry_address: str = "",
        score_threshold: int = 5000,
    ) -> None:
        # Note: quorum_bps is no longer a deploy-time parameter. AppIntentBase
        # reads quorum from the ValidatorRegistry at execution time, so a
        # single owner tx on the registry reconfigures every App on the chain.
        self.compiler = compiler
        self.relayer = relayer
        self.registry_address = registry_address
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

        # Platform constructor args — ordered to match AppIntentBase constructor
        # signature in subnet112/minotaur_contracts/src/AppIntentBase.sol:
        #   (_relayer, _validatorRegistry, _scoreThreshold,
        #    _wrappedNativeToken, _platformFeeCollector,
        #    _minPlatformFeeWei, _maxPlatformFeeWei,
        #    _feeMode, _appPaymaster, _appRegistry)
        from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
        wnt = WRAPPED_NATIVE_TOKEN.get(chain_id, "0x" + "0" * 40)
        wnt_cs = Web3.to_checksum_address(wnt)
        platform_fee_collector = relayer_cs  # Relayer collects fees by default
        min_platform_fee = int(os.environ.get("MIN_PLATFORM_FEE_WEI", "0"))
        max_platform_fee = int(os.environ.get("MAX_PLATFORM_FEE_WEI", str(10**17)))  # 0.1 ETH
        # Per-App fee mode (#239): the App's own config choice wins; the
        # operator-wide FEE_MODE_DEFAULT is only the fallback. Bakes USER vs APP
        # into THIS App's contract.
        try:
            fee_mode_str, fee_mode = resolve_fee_mode(getattr(app.config, "fee_mode", ""))
        except ValueError as exc:
            return DeploymentResult(
                app_id=app.app_id,
                status=AppStatus.DRAFT,
                js_code_hash=js_hash,
                chain_id=chain_id,
                error=str(exc),
            )
        zero_addr = Web3.to_checksum_address("0x" + "0" * 40)
        app_paymaster = zero_addr  # informational; apps override via constructor_args if needed

        # AppRegistry address — when configured, the deployed App enforces
        # `_requireRegistered()` against it. Unset/zero disables the check,
        # which matches the contract's escape hatch.
        chain_cfg = self.relayer.chains.get(chain_id)
        app_registry_addr = (
            (chain_cfg.app_registry_address if chain_cfg and chain_cfg.app_registry_address else "")
            or os.environ.get(f"APP_REGISTRY_{chain_id}", "")
        )
        if app_registry_addr:
            app_registry_cs = Web3.to_checksum_address(app_registry_addr)
        else:
            logger.warning(
                "Deploy on chain %d: no AppRegistry configured (APP_REGISTRY_%d unset); "
                "deployed App will run with the on-chain registry gate disabled",
                chain_id, chain_id,
            )
            app_registry_cs = zero_addr

        arg_types = [
            "address",  # _relayer
            "address",  # _validatorRegistry
            "uint256",  # _scoreThreshold
            "address",  # _wrappedNativeToken
            "address",  # _platformFeeCollector
            "uint256",  # _minPlatformFeeWei
            "uint256",  # _maxPlatformFeeWei
            "uint8",    # _feeMode (FeeMode enum)
            "address",  # _appPaymaster
            "address",  # _appRegistry
        ]
        arg_values: list = [
            relayer_cs,
            registry_cs,
            self.score_threshold,
            wnt_cs,
            platform_fee_collector,
            min_platform_fee,
            max_platform_fee,
            fee_mode,
            app_paymaster,
            app_registry_cs,
        ]
        logger.info(
            "Deploy constructor: relayer=%s registry=%s scoreThreshold=%d "
            "wrappedNative=%s feeCollector=%s minFee=%d maxFee=%d "
            "feeMode=%s appPaymaster=%s appRegistry=%s",
            relayer_cs[:10], registry_cs[:10], self.score_threshold,
            wnt_cs[:10], platform_fee_collector[:10],
            min_platform_fee, max_platform_fee, fee_mode_str,
            app_paymaster[:10], app_registry_cs[:10],
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
