"""Policy-bounded proxy execution for native Bittensor staking actions.

This module intentionally scaffolds only the first delegated execution slice:

- verify a user's `Staking` proxy relationship still exists
- validate a Minotaur-side permission policy
- execute proxied `add_stake`
- execute proxied `move_stake`

It does not yet expose API routes or production key management.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from minotaur_subnet.shared.types import (
    NativeBittensorAction,
    NativeBittensorActionRequest,
    NativeBittensorExecutionRecord,
    NativeBittensorExecutionStatus,
    NativeBittensorPermission,
    NativeBittensorPermissionStatus,
)

logger = logging.getLogger(__name__)

_RATE_LIMITED_STATUSES = {
    NativeBittensorExecutionStatus.SUBMITTED,
    NativeBittensorExecutionStatus.CONFIRMED,
}


@dataclass
class ProxyVerificationResult:
    """Result of checking whether the expected proxy relationship exists."""

    valid: bool
    matched_delegate: bool = False
    matched_proxy_type: bool = False
    matched_delay: bool = False
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class BittensorProxyExecutor:
    """Execute native Bittensor staking actions through a delegated proxy.

    Args:
        network: Subtensor network identifier or URL.
        subtensor: Optional injected Subtensor client for tests.
        wallet_loader: Callback resolving a delegate ss58 to a loaded bittensor wallet.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        *,
        network: str = "finney",
        subtensor: Any | None = None,
        wallet_loader: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.network = network
        self._subtensor = subtensor
        self._wallet_loader = wallet_loader
        self._clock = clock

    def _get_subtensor(self) -> Any:
        if self._subtensor is None:
            import bittensor as bt

            self._subtensor = bt.Subtensor(network=self.network)
        return self._subtensor

    def _get_delegate_wallet(self, delegate_ss58: str) -> Any:
        if self._wallet_loader is None:
            raise RuntimeError("No delegate wallet loader configured")
        wallet = self._wallet_loader(delegate_ss58)
        if wallet is None:
            raise KeyError(f"Delegate wallet not found: {delegate_ss58}")
        return wallet

    @staticmethod
    def _extract_proxy_entries(raw: Any) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, dict):
            if "proxies" in raw:
                raw = raw["proxies"]
            elif "delegate" in raw or "proxy_type" in raw:
                raw = [raw]
            else:
                raw = list(raw.values())
        if isinstance(raw, tuple):
            if len(raw) >= 1 and isinstance(raw[0], list):
                raw = raw[0]
            else:
                raw = list(raw)
        if isinstance(raw, list):
            return raw
        return [raw]

    @staticmethod
    def _get_field(entry: Any, *names: str, default: Any = None) -> Any:
        if isinstance(entry, dict):
            for name in names:
                if name in entry:
                    return entry[name]
            return default
        for name in names:
            if hasattr(entry, name):
                return getattr(entry, name)
        return default

    @staticmethod
    def _normalize_enum_like(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "value"):
            return str(value.value)
        if hasattr(value, "name"):
            return str(value.name)
        return str(value)

    def verify_proxy(self, permission: NativeBittensorPermission) -> ProxyVerificationResult:
        """Check whether the expected proxy relationship exists on-chain."""
        try:
            raw_entries = self._get_subtensor().get_proxies_for_real_account(permission.owner_ss58)
        except Exception as exc:
            return ProxyVerificationResult(
                valid=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        entries = []
        matched_delegate = False
        matched_proxy_type = False
        matched_delay = False
        expected_proxy_type = permission.proxy_type.casefold()

        for entry in self._extract_proxy_entries(raw_entries):
            delegate = self._get_field(entry, "delegate", "delegate_ss58", "delegate_address", default="")
            proxy_type = self._normalize_enum_like(
                self._get_field(entry, "proxy_type", "type", default="")
            )
            delay = int(self._get_field(entry, "delay", "delay_blocks", default=0) or 0)
            normalized = {
                "delegate_ss58": str(delegate),
                "proxy_type": proxy_type,
                "delay_blocks": delay,
            }
            entries.append(normalized)
            if str(delegate) != permission.delegate_ss58:
                continue
            matched_delegate = True
            if proxy_type.casefold() == expected_proxy_type:
                matched_proxy_type = True
            if delay == permission.proxy_delay_blocks:
                matched_delay = True

        return ProxyVerificationResult(
            valid=matched_delegate and matched_proxy_type and matched_delay,
            matched_delegate=matched_delegate,
            matched_proxy_type=matched_proxy_type,
            matched_delay=matched_delay,
            entries=entries,
        )

    def validate_request(
        self,
        permission: NativeBittensorPermission,
        request: NativeBittensorActionRequest,
        *,
        recent_executions: Iterable[NativeBittensorExecutionRecord] | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Validate a delegated action request against Minotaur policy."""
        current_time = now if now is not None else self._clock()

        if permission.permission_id != request.permission_id:
            return False, "Permission ID mismatch"
        if request.owner_ss58 != permission.owner_ss58:
            return False, "Owner ss58 does not match delegated permission"
        if request.delegate_ss58 != permission.delegate_ss58:
            return False, "Delegate ss58 does not match delegated permission"
        if permission.status is not NativeBittensorPermissionStatus.ACTIVE:
            return False, f"Delegated permission is not active: {permission.status.value}"
        if permission.expires_at is not None and current_time > permission.expires_at:
            return False, "Delegated permission has expired"
        if request.amount_rao <= 0:
            return False, "amount_rao must be positive"
        if request.action not in permission.enabled_actions:
            return False, f"Action not allowed by delegated permission: {request.action.value}"
        if request.action is NativeBittensorAction.ADD_STAKE:
            if request.netuid is None or not request.hotkey_ss58:
                return False, "add_stake requires netuid and hotkey_ss58"
        elif request.action is NativeBittensorAction.MOVE_STAKE:
            if (
                request.origin_netuid is None
                or request.destination_netuid is None
                or not request.origin_hotkey_ss58
                or not request.destination_hotkey_ss58
            ):
                return False, "move_stake requires origin/destination hotkeys and netuids"
        elif request.action is NativeBittensorAction.REMOVE_STAKE:
            if request.netuid is None or not request.hotkey_ss58:
                return False, "remove_stake requires netuid and hotkey_ss58"
        else:
            return False, f"Unsupported delegated action: {request.action.value}"

        if (
            permission.max_rao_per_action is not None
            and request.amount_rao > permission.max_rao_per_action
        ):
            return False, "Requested amount exceeds per-action policy cap"

        if permission.allowed_netuids:
            disallowed = [
                netuid
                for netuid in request.related_netuids()
                if netuid not in permission.allowed_netuids
            ]
            if disallowed:
                return False, f"Request touches disallowed netuids: {sorted(set(disallowed))}"

        if permission.allowed_hotkeys:
            disallowed_hotkeys = [
                hotkey
                for hotkey in request.related_hotkeys()
                if hotkey not in permission.allowed_hotkeys
            ]
            if disallowed_hotkeys:
                return False, "Request touches disallowed validator hotkeys"

        records = [
            record
            for record in (recent_executions or [])
            if record.permission_id == permission.permission_id
            and record.status in _RATE_LIMITED_STATUSES
        ]

        if permission.cooldown_seconds:
            last_submission = max((record.submitted_at for record in records), default=0.0)
            if last_submission and last_submission > current_time - permission.cooldown_seconds:
                return False, "Delegated permission is in cooldown"

        if permission.max_rao_per_day is not None:
            window_start = current_time - 86400
            daily_total = sum(
                record.amount_rao
                for record in records
                if record.submitted_at >= window_start
            )
            if daily_total + request.amount_rao > permission.max_rao_per_day:
                return False, "Requested amount exceeds daily policy cap"

        return True, ""

    def _compose_call(self, function_name: str, call_params: dict[str, Any]) -> Any:
        subtensor = self._get_subtensor()
        if hasattr(subtensor, "compose_call"):
            return subtensor.compose_call(
                call_module="SubtensorModule",
                call_function=function_name,
                call_params=call_params,
            )
        raise RuntimeError("Subtensor client does not expose compose_call()")

    def _compose_action_call(self, request: NativeBittensorActionRequest) -> Any:
        if request.action is NativeBittensorAction.ADD_STAKE:
            return self._compose_call(
                "add_stake",
                {
                    "netuid": request.netuid,
                    "hotkey": request.hotkey_ss58,
                    "amount_staked": request.amount_rao,
                },
            )
        if request.action is NativeBittensorAction.MOVE_STAKE:
            return self._compose_call(
                "move_stake",
                {
                    "origin_netuid": request.origin_netuid,
                    "origin_hotkey_ss58": request.origin_hotkey_ss58,
                    "destination_netuid": request.destination_netuid,
                    "destination_hotkey_ss58": request.destination_hotkey_ss58,
                    "alpha_amount": request.amount_rao,
                },
            )
        if request.action is NativeBittensorAction.REMOVE_STAKE:
            return self._compose_call(
                "remove_stake",
                {
                    "netuid": request.netuid,
                    "hotkey": request.hotkey_ss58,
                    "amount_unstaked": request.amount_rao,
                },
            )
        raise NotImplementedError(f"Unsupported action: {request.action.value}")

    @staticmethod
    def _hash_call(call: Any) -> str:
        payload = getattr(call, "data", None)
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            if payload.startswith("0x"):
                raw = bytes.fromhex(payload[2:])
            else:
                raw = payload.encode()
        else:
            raw = repr(call).encode()
        return "0x" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _extract_extrinsic_hash(response: Any) -> str:
        if response is None:
            return ""
        direct = getattr(response, "extrinsic_hash", None)
        if direct:
            return str(direct)
        receipt = getattr(response, "extrinsic_receipt", None)
        if receipt is not None:
            for name in ("extrinsic_hash", "hash"):
                value = getattr(receipt, name, None)
                if value:
                    return str(value)
        if isinstance(response, dict):
            return str(response.get("extrinsic_hash", ""))
        return ""

    @staticmethod
    def _response_success(response: Any) -> bool:
        if response is None:
            return False
        if hasattr(response, "success"):
            return bool(response.success)
        if isinstance(response, dict):
            return bool(response.get("success", False))
        return bool(response)

    @staticmethod
    def _response_error(response: Any) -> str:
        if response is None:
            return "Missing response from subtensor proxy submission"
        if hasattr(response, "error") and response.error:
            return str(response.error)
        if hasattr(response, "message") and response.message and not getattr(response, "success", True):
            return str(response.message)
        if isinstance(response, dict):
            return str(response.get("error", ""))
        return ""

    @staticmethod
    def _response_summary(response: Any) -> dict[str, Any]:
        if response is None:
            return {}
        summary = {
            "success": BittensorProxyExecutor._response_success(response),
            "extrinsic_hash": BittensorProxyExecutor._extract_extrinsic_hash(response),
        }
        error = BittensorProxyExecutor._response_error(response)
        if error:
            summary["error"] = error
        message = getattr(response, "message", None)
        if message:
            summary["message"] = str(message)
        if isinstance(response, dict) and response.get("message"):
            summary["message"] = str(response["message"])
        return summary

    def _new_record(self, request: NativeBittensorActionRequest) -> NativeBittensorExecutionRecord:
        return NativeBittensorExecutionRecord(
            execution_id=f"native_exec_{uuid.uuid4().hex[:16]}",
            permission_id=request.permission_id,
            action=request.action,
            owner_ss58=request.owner_ss58,
            delegate_ss58=request.delegate_ss58,
            amount_rao=request.amount_rao,
            netuid=request.netuid,
            hotkey_ss58=request.hotkey_ss58,
            origin_netuid=request.origin_netuid,
            origin_hotkey_ss58=request.origin_hotkey_ss58,
            destination_netuid=request.destination_netuid,
            destination_hotkey_ss58=request.destination_hotkey_ss58,
            reason=request.reason,
            metadata=dict(request.metadata or {}),
        )

    def execute_request(
        self,
        permission: NativeBittensorPermission,
        request: NativeBittensorActionRequest,
        *,
        recent_executions: Iterable[NativeBittensorExecutionRecord] | None = None,
    ) -> NativeBittensorExecutionRecord:
        """Validate and execute a delegated native Bittensor action."""
        record = self._new_record(request)
        allowed, error = self.validate_request(
            permission,
            request,
            recent_executions=recent_executions,
        )
        if not allowed:
            record.status = NativeBittensorExecutionStatus.REJECTED
            record.error = error
            return record

        verification = self.verify_proxy(permission)
        record.metadata["proxy_verification"] = asdict(verification)
        if not verification.valid:
            record.status = NativeBittensorExecutionStatus.REJECTED
            record.error = (
                verification.error
                or "Expected delegated staking proxy is not active on-chain"
            )
            return record

        try:
            delegate_wallet = self._get_delegate_wallet(request.delegate_ss58)
            call = self._compose_action_call(request)
            record.call_hash = self._hash_call(call)
            record.status = NativeBittensorExecutionStatus.SUBMITTED
            record.submitted_at = self._clock()

            response = self._get_subtensor().proxy(
                wallet=delegate_wallet,
                real_account_ss58=request.owner_ss58,
                force_proxy_type=permission.proxy_type,
                call=call,
            )
            record.extrinsic_hash = self._extract_extrinsic_hash(response)
            record.metadata["response_summary"] = self._response_summary(response)

            if self._response_success(response):
                record.status = NativeBittensorExecutionStatus.CONFIRMED
                record.finalized_at = self._clock()
            else:
                record.status = NativeBittensorExecutionStatus.FAILED
                record.error = self._response_error(response) or "Proxy submission failed"
            return record
        except Exception as exc:
            logger.error("Native Bittensor proxy execution failed: %s", exc)
            record.status = NativeBittensorExecutionStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            return record

    def execute_add_stake(
        self,
        permission: NativeBittensorPermission,
        *,
        netuid: int,
        hotkey_ss58: str,
        amount_rao: int,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        recent_executions: Iterable[NativeBittensorExecutionRecord] | None = None,
    ) -> NativeBittensorExecutionRecord:
        """Execute a proxied `add_stake` request."""
        request = NativeBittensorActionRequest(
            permission_id=permission.permission_id,
            action=NativeBittensorAction.ADD_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            reason=reason,
            metadata=metadata or {},
        )
        return self.execute_request(
            permission,
            request,
            recent_executions=recent_executions,
        )

    def execute_move_stake(
        self,
        permission: NativeBittensorPermission,
        *,
        origin_netuid: int,
        origin_hotkey_ss58: str,
        destination_netuid: int,
        destination_hotkey_ss58: str,
        amount_rao: int,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        recent_executions: Iterable[NativeBittensorExecutionRecord] | None = None,
    ) -> NativeBittensorExecutionRecord:
        """Execute a proxied `move_stake` request."""
        request = NativeBittensorActionRequest(
            permission_id=permission.permission_id,
            action=NativeBittensorAction.MOVE_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            origin_netuid=origin_netuid,
            origin_hotkey_ss58=origin_hotkey_ss58,
            destination_netuid=destination_netuid,
            destination_hotkey_ss58=destination_hotkey_ss58,
            reason=reason,
            metadata=metadata or {},
        )
        return self.execute_request(
            permission,
            request,
            recent_executions=recent_executions,
        )

    def execute_remove_stake(
        self,
        permission: NativeBittensorPermission,
        *,
        netuid: int,
        hotkey_ss58: str,
        amount_rao: int,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        recent_executions: Iterable[NativeBittensorExecutionRecord] | None = None,
    ) -> NativeBittensorExecutionRecord:
        """Execute a proxied `remove_stake` (unstake alpha → TAO) request."""
        request = NativeBittensorActionRequest(
            permission_id=permission.permission_id,
            action=NativeBittensorAction.REMOVE_STAKE,
            owner_ss58=permission.owner_ss58,
            delegate_ss58=permission.delegate_ss58,
            amount_rao=amount_rao,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            reason=reason,
            metadata=metadata or {},
        )
        return self.execute_request(
            permission,
            request,
            recent_executions=recent_executions,
        )


__all__ = [
    "ProxyVerificationResult",
    "BittensorProxyExecutor",
]
