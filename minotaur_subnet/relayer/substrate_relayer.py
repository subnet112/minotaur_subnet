"""Substrate relayer — executes Bittensor substrate extrinsics.

Wraps ``BittensorProxyExecutor`` with a submission interface that the
BlockLoop can use alongside the EVM relayer. Routes substrate legs
(unstake alpha, bridge deposit) through the proxy delegation pattern.

Usage::

    relayer = SubstrateRelayer(proxy_executor, permission_store)
    result = await relayer.submit_action(substrate_action)
    # result.success, result.tx_hash (= extrinsic hash)
"""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import (
    NativeBittensorAction,
    NativeBittensorActionRequest,
    NativeBittensorExecutionStatus,
    NativeBittensorPermission,
    SubstrateAction,
)

logger = logging.getLogger(__name__)


class SubmitResult:
    """Result of a substrate extrinsic submission."""

    def __init__(
        self,
        success: bool,
        tx_hash: str = "",
        error: str = "",
        block_number: int | None = None,
    ) -> None:
        self.success = success
        self.tx_hash = tx_hash
        self.error = error
        self.block_number = block_number


class SubstrateRelayer:
    """Routes SubstrateAction objects to the BittensorProxyExecutor.

    Looks up the user's NativeBittensorPermission, validates the action,
    and executes via the delegated proxy pattern.
    """

    def __init__(
        self,
        proxy_executor: Any,
        permission_lookup: Any = None,
    ) -> None:
        """
        Args:
            proxy_executor: BittensorProxyExecutor instance.
            permission_lookup: Callable(owner_ss58) -> NativeBittensorPermission | None.
                If None, uses a default that accepts all actions (testnet mode).
        """
        self.proxy_executor = proxy_executor
        self._permission_lookup = permission_lookup

    async def submit_action(self, action: SubstrateAction) -> SubmitResult:
        """Execute a substrate action via the proxy executor.

        Converts SubstrateAction → NativeBittensorActionRequest, looks up
        the user's permission, validates, and executes.

        Returns SubmitResult with extrinsic_hash as tx_hash.
        """
        # Map action string to NativeBittensorAction enum
        action_map = {
            "remove_stake": NativeBittensorAction.REMOVE_STAKE,
            "add_stake": NativeBittensorAction.ADD_STAKE,
            "move_stake": NativeBittensorAction.MOVE_STAKE,
            "bridge_deposit": None,  # Handled separately
        }

        bt_action = action_map.get(action.action)

        # Bridge deposit is a plain balance transfer, not a staking action
        if action.action == "bridge_deposit":
            return await self._submit_bridge_deposit(action)

        # Stake/unstake: try proxy executor first (with active permission),
        # fall back to direct SDK for testnet convenience.
        if action.action in ("remove_stake", "add_stake"):
            # Try permission-based proxy first
            permission = self._get_permission(action.owner_ss58)
            if permission is not None and bt_action is not None:
                request = NativeBittensorActionRequest(
                    permission_id=permission.permission_id,
                    action=bt_action,
                    owner_ss58=action.owner_ss58,
                    delegate_ss58=permission.delegate_ss58,
                    amount_rao=action.amount_rao,
                    netuid=action.netuid if action.netuid else None,
                    hotkey_ss58=action.hotkey_ss58,
                    reason=f"intent-execution:{action.action}",
                    metadata=action.metadata,
                )
                valid, msg = self.proxy_executor.validate_request(permission, request)
                if valid:
                    try:
                        record = self.proxy_executor.execute_request(permission, request)
                        success = record.status is NativeBittensorExecutionStatus.CONFIRMED
                        return SubmitResult(
                            success=success,
                            tx_hash=record.extrinsic_hash or record.call_hash or "",
                            error=record.error or ("" if success else "Extrinsic failed"),
                        )
                    except Exception as exc:
                        logger.warning("Proxy execution failed, falling back to direct: %s", exc)

            # Fallback: direct SDK execution (testnet)
            return await self._direct_stake(action)

        if bt_action is None:
            return SubmitResult(
                success=False,
                error=f"Unknown substrate action: {action.action}",
            )

        # For other actions, try the proxy executor path
        permission = self._get_permission(action.owner_ss58)
        if permission is None:
            return SubmitResult(
                success=False,
                error=f"No substrate permission found for {action.owner_ss58}",
            )

        request = NativeBittensorActionRequest(
            permission_id=permission.permission_id,
            action=bt_action,
            owner_ss58=action.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=action.amount_rao,
            netuid=action.netuid if action.netuid else None,
            hotkey_ss58=action.hotkey_ss58,
            reason=f"intent-execution:{action.action}",
            metadata=action.metadata,
        )

        valid, msg = self.proxy_executor.validate_request(permission, request)
        if not valid:
            return SubmitResult(success=False, error=f"Validation failed: {msg}")

        try:
            record = self.proxy_executor.execute_request(permission, request)
        except Exception as exc:
            logger.error("Substrate action failed: %s", exc)
            return SubmitResult(success=False, error=str(exc))

        success = record.status is NativeBittensorExecutionStatus.CONFIRMED
        return SubmitResult(
            success=success,
            tx_hash=record.extrinsic_hash or record.call_hash or "",
            error=record.error or ("" if success else "Extrinsic failed"),
        )

    async def _direct_stake(self, action: SubstrateAction) -> SubmitResult:
        """Execute stake/unstake directly via bittensor SDK (testnet mode).

        Supports both add_stake (TAO → Alpha) and remove_stake (Alpha → TAO).
        Production would use delegated proxy execution.
        """
        try:
            import bittensor as bt

            network = getattr(self.proxy_executor, 'network', 'ws://subtensor:9944')
            sub = bt.Subtensor(network=network)

            # Load the wallet for the owner
            wallet = None
            for wallet_name in ['alice_testnet_init', 'validator', 'default']:
                try:
                    w = bt.Wallet(name=wallet_name)
                    if w.coldkey.ss58_address == action.owner_ss58:
                        wallet = w
                        break
                    if w.hotkey.ss58_address == action.hotkey_ss58:
                        wallet = w
                        break
                except Exception:
                    continue

            if wallet is None:
                return SubmitResult(
                    success=False,
                    error=f"No wallet found for owner {action.owner_ss58}",
                )

            hotkey = action.hotkey_ss58 or wallet.hotkey.ss58_address
            amount = bt.Balance.from_rao(action.amount_rao)

            if action.action == "add_stake":
                result = sub.add_stake(
                    wallet=wallet,
                    hotkey_ss58=hotkey,
                    netuid=action.netuid,
                    amount=amount,
                )
            else:
                result = sub.unstake(
                    wallet=wallet,
                    hotkey_ss58=hotkey,
                    netuid=action.netuid,
                    amount=amount,
                )

            success = getattr(result, 'success', False)
            extrinsic_hash = ""
            if hasattr(result, 'extrinsic') and isinstance(result.extrinsic, dict):
                receipt = result.extrinsic.get('extrinsic_receipt')
                if receipt:
                    extrinsic_hash = getattr(receipt, 'extrinsic_hash', '') or ''

            logger.info(
                "Direct %s: netuid=%d amount=%d success=%s",
                action.action, action.netuid, action.amount_rao, success,
            )
            return SubmitResult(
                success=success,
                tx_hash=extrinsic_hash or f"{action.action}-ok",
                error="" if success else str(getattr(result, 'error', f'{action.action} failed')),
            )
        except Exception as exc:
            logger.error("Direct %s failed: %s", action.action, exc)
            return SubmitResult(success=False, error=str(exc))

    async def _submit_bridge_deposit(self, action: SubstrateAction) -> SubmitResult:
        """Submit a TAO bridge deposit.

        On testnet: simulates the bridge deposit (no real Tensorplex bridge).
        The bridge tracker uses the mock adapter which completes instantly,
        so the wTAO appears on the Ethereum side immediately.

        On production: would execute a substrate balance.transfer to the
        Tensorplex lock address and monitor via their API.
        """
        logger.info(
            "Bridge deposit: %d RAO from %s (testnet mode — mock completion)",
            action.amount_rao, action.owner_ss58[:16],
        )
        # For testnet, the mock bridge adapter will complete instantly.
        # Return a fake tx hash that the bridge tracker can use.
        import hashlib
        fake_hash = hashlib.sha256(
            f"bridge:{action.owner_ss58}:{action.amount_rao}".encode()
        ).hexdigest()

        return SubmitResult(
            success=True,
            tx_hash=fake_hash,
            error="",
        )

    def _get_permission(self, owner_ss58: str) -> NativeBittensorPermission | None:
        """Look up substrate permission for a user."""
        if self._permission_lookup is not None:
            try:
                return self._permission_lookup(owner_ss58)
            except Exception:
                return None

        # Testnet fallback: create a permissive permission
        return NativeBittensorPermission(
            permission_id="testnet-auto",
            owner_ss58=owner_ss58,
            delegate_ss58=owner_ss58,  # Self-delegation for testnet
            enabled_actions={
                NativeBittensorAction.ADD_STAKE,
                NativeBittensorAction.MOVE_STAKE,
                NativeBittensorAction.REMOVE_STAKE,
            },
        )
