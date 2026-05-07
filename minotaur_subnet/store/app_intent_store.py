"""
Persistent in-memory store for App Intent definitions and wallet info.

Stores data in a JSON file so state survives server restarts.
All mutations flush to disk immediately.
"""

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
    NativeBittensorAction,
    NativeBittensorExecutionRecord,
    NativeBittensorExecutionStatus,
    NativeBittensorPermission,
    NativeBittensorPermissionStatus,
    PolicyTier,
    TriggerType,
    WalletInfo,
)

# Persistent storage path
_STORE_DIR = Path(__file__).parent / "data"
_STORE_PATH = _STORE_DIR / "store.json"


def _trigger_type_from_str(value: str) -> TriggerType:
    """Convert a string back to a TriggerType enum."""
    for member in TriggerType:
        if member.value == value:
            return member
    return TriggerType.USER_TRIGGERED


def _policy_tier_from_str(value: str) -> PolicyTier:
    """Convert a string back to a PolicyTier enum."""
    for member in PolicyTier:
        if member.value == value:
            return member
    return PolicyTier.HYBRID


def _app_status_from_str(value: str) -> AppStatus:
    """Convert a string back to an AppStatus enum."""
    for member in AppStatus:
        if member.value == value:
            return member
    return AppStatus.DRAFT


def _native_action_from_str(value: str) -> NativeBittensorAction:
    """Convert a string back to a NativeBittensorAction enum."""
    for member in NativeBittensorAction:
        if member.value == value:
            return member
    return NativeBittensorAction.ADD_STAKE


def _native_permission_status_from_str(value: str) -> NativeBittensorPermissionStatus:
    """Convert a string back to a NativeBittensorPermissionStatus enum."""
    for member in NativeBittensorPermissionStatus:
        if member.value == value:
            return member
    return NativeBittensorPermissionStatus.PENDING


def _native_execution_status_from_str(value: str) -> NativeBittensorExecutionStatus:
    """Convert a string back to a NativeBittensorExecutionStatus enum."""
    for member in NativeBittensorExecutionStatus:
        if member.value == value:
            return member
    return NativeBittensorExecutionStatus.PENDING


def _config_from_dict(d: dict[str, Any]) -> AppIntentConfig:
    """Reconstruct an AppIntentConfig from a plain dict."""
    supported_policy_tiers = [
        _policy_tier_from_str(v) for v in d.get(
            "supported_policy_tiers", ["strict", "hybrid", "expert"]
        )
    ]
    return AppIntentConfig(
        supported_chains=d.get("supported_chains", []),
        score_threshold=d.get("score_threshold", 0.5),
        on_chain_threshold=d.get("on_chain_threshold", 5000),
        trigger_type=_trigger_type_from_str(d.get("trigger_type", "user_triggered")),
        max_gas=d.get("max_gas", 500_000),
        policy_tier=_policy_tier_from_str(d.get("policy_tier", "hybrid")),
        supported_policy_tiers=supported_policy_tiers,
        manifest_version=d.get("manifest_version", "v1"),
    )


def _definition_from_dict(d: dict[str, Any]) -> AppIntentDefinition:
    """Reconstruct an AppIntentDefinition from a plain dict."""
    config = _config_from_dict(d.get("config", {}))
    return AppIntentDefinition(
        app_id=d["app_id"],
        name=d["name"],
        version=d.get("version", "1.0.0"),
        intent_type=d["intent_type"],
        js_code=d.get("js_code", ""),
        solidity_code=d.get("solidity_code"),
        config=config,
        deployer=d.get("deployer", ""),
        description=d.get("description", ""),
        manifest=d.get("manifest"),
        constructor_args=d.get("constructor_args"),
        schema_id=d.get("schema_id", ""),
        policy_metadata=d.get("policy_metadata", {}),
    )


def _wallet_from_dict(d: dict[str, Any]) -> WalletInfo:
    """Reconstruct a WalletInfo from a plain dict."""
    return WalletInfo(
        address=d["address"],
        chain_ids=d.get("chain_ids", [1]),
        wallet_type=d.get("wallet_type", "local"),
        created_at=d.get("created_at", 0.0),
        policy_tier=_policy_tier_from_str(d.get("policy_tier", "hybrid")),
        policy_id=d.get("policy_id", ""),
        policy_overrides=d.get("policy_overrides", {}),
    )


def _deployment_from_dict(d: dict[str, Any]) -> DeploymentResult:
    """Reconstruct a DeploymentResult from a plain dict."""
    return DeploymentResult(
        app_id=d["app_id"],
        status=_app_status_from_str(d.get("status", "draft")),
        contract_address=d.get("contract_address"),
        js_code_hash=d.get("js_code_hash", ""),
        chain_id=d.get("chain_id", 1),
        error=d.get("error"),
    )


def _native_permission_from_dict(d: dict[str, Any]) -> NativeBittensorPermission:
    """Reconstruct a NativeBittensorPermission from a plain dict."""
    enabled_actions = [
        _native_action_from_str(value)
        for value in d.get("enabled_actions", [])
    ]
    return NativeBittensorPermission(
        permission_id=d["permission_id"],
        owner_ss58=d["owner_ss58"],
        delegate_ss58=d["delegate_ss58"],
        proxy_type=d.get("proxy_type", "Staking"),
        proxy_delay_blocks=d.get("proxy_delay_blocks", 0),
        status=_native_permission_status_from_str(d.get("status", "pending")),
        enabled_actions=enabled_actions or [
            NativeBittensorAction.ADD_STAKE,
            NativeBittensorAction.MOVE_STAKE,
        ],
        allowed_netuids=d.get("allowed_netuids", []),
        allowed_hotkeys=d.get("allowed_hotkeys", []),
        max_rao_per_action=d.get("max_rao_per_action"),
        max_rao_per_day=d.get("max_rao_per_day"),
        max_slippage_bps=d.get("max_slippage_bps"),
        cooldown_seconds=d.get("cooldown_seconds"),
        expires_at=d.get("expires_at"),
        policy_tier=_policy_tier_from_str(d.get("policy_tier", "strict")),
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        metadata=d.get("metadata", {}),
    )


def _native_execution_from_dict(d: dict[str, Any]) -> NativeBittensorExecutionRecord:
    """Reconstruct a NativeBittensorExecutionRecord from a plain dict."""
    return NativeBittensorExecutionRecord(
        execution_id=d["execution_id"],
        permission_id=d["permission_id"],
        action=_native_action_from_str(d.get("action", "add_stake")),
        owner_ss58=d["owner_ss58"],
        delegate_ss58=d["delegate_ss58"],
        amount_rao=d.get("amount_rao", 0),
        status=_native_execution_status_from_str(d.get("status", "pending")),
        netuid=d.get("netuid"),
        hotkey_ss58=d.get("hotkey_ss58", ""),
        origin_netuid=d.get("origin_netuid"),
        origin_hotkey_ss58=d.get("origin_hotkey_ss58", ""),
        destination_netuid=d.get("destination_netuid"),
        destination_hotkey_ss58=d.get("destination_hotkey_ss58", ""),
        call_hash=d.get("call_hash", ""),
        extrinsic_hash=d.get("extrinsic_hash", ""),
        error=d.get("error", ""),
        reason=d.get("reason", ""),
        submitted_at=d.get("submitted_at", 0.0),
        finalized_at=d.get("finalized_at"),
        metadata=d.get("metadata", {}),
    )


class _Serializer(json.JSONEncoder):
    """JSON encoder that handles dataclasses and enums."""

    def default(self, o: Any) -> Any:
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        if isinstance(
            o,
            AppStatus
            | TriggerType
            | PolicyTier
            | NativeBittensorAction
            | NativeBittensorPermissionStatus
            | NativeBittensorExecutionStatus,
        ):
            return o.value
        return super().default(o)


class AppIntentStore:
    """
    Persistent store for App Intent definitions, wallets, deployments, and
    execution statistics.

    Data is held in memory for fast access and flushed to a JSON file on
    every mutation so that it survives server restarts.

    Deployments are keyed as ``{app_id: {chain_id: DeploymentResult}}``,
    supporting per-chain deployment tracking. Legacy single-deployment
    entries are auto-migrated on load.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = store_path or _STORE_PATH
        self._mtime_ns: int | None = None
        self._apps: dict[str, AppIntentDefinition] = {}
        self._wallets: dict[str, WalletInfo] = {}
        self._deployments: dict[str, dict[int, DeploymentResult]] = {}
        self._app_stats: dict[str, dict[str, Any]] = {}
        self._quote_stats: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._native_permissions: dict[str, NativeBittensorPermission] = {}
        self._native_executions: dict[str, NativeBittensorExecutionRecord] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load state from disk if the file exists."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            apps: dict[str, AppIntentDefinition] = {}
            wallets: dict[str, WalletInfo] = {}
            deployments: dict[str, dict[int, DeploymentResult]] = {}
            native_permissions: dict[str, NativeBittensorPermission] = {}
            native_executions: dict[str, NativeBittensorExecutionRecord] = {}
            for app_id, d in raw.get("apps", {}).items():
                apps[app_id] = _definition_from_dict(d)
            for addr, d in raw.get("wallets", {}).items():
                wallets[addr] = _wallet_from_dict(d)
            for app_id, d in raw.get("deployments", {}).items():
                if isinstance(d, dict) and "app_id" in d:
                    # Legacy format: single DeploymentResult dict
                    dep = _deployment_from_dict(d)
                    deployments[app_id] = {dep.chain_id: dep}
                elif isinstance(d, dict):
                    # New format: {chain_id_str: DeploymentResult dict}
                    deployments[app_id] = {}
                    for chain_str, dep_dict in d.items():
                        dep = _deployment_from_dict(dep_dict)
                        deployments[app_id][dep.chain_id] = dep
            for permission_id, d in raw.get("native_permissions", {}).items():
                native_permissions[permission_id] = _native_permission_from_dict(d)
            for execution_id, d in raw.get("native_executions", {}).items():
                native_executions[execution_id] = _native_execution_from_dict(d)
            self._apps = apps
            self._wallets = wallets
            self._deployments = deployments
            self._app_stats = raw.get("app_stats", {})
            self._quote_stats = raw.get("quote_stats", {})
            self._orders = raw.get("orders", {})
            self._native_permissions = native_permissions
            self._native_executions = native_executions
            self._mtime_ns = self._path.stat().st_mtime_ns
        except (json.JSONDecodeError, KeyError) as exc:
            # Corrupt file -- start fresh but don't delete it
            print(f"[store] WARNING: failed to load {self._path}: {exc}")

    def _flush(self) -> None:
        """Write current state to disk."""
        os.makedirs(self._path.parent, exist_ok=True)
        payload = {
            "apps": {k: asdict(v) for k, v in self._apps.items()},
            "wallets": {k: asdict(v) for k, v in self._wallets.items()},
            "deployments": {
                app_id: {
                    str(chain_id): asdict(dep)
                    for chain_id, dep in chain_map.items()
                }
                for app_id, chain_map in self._deployments.items()
            },
            "app_stats": self._app_stats,
            "quote_stats": self._quote_stats,
            "orders": self._orders,
            "native_permissions": {
                key: asdict(value)
                for key, value in self._native_permissions.items()
            },
            "native_executions": {
                key: asdict(value)
                for key, value in self._native_executions.items()
            },
        }
        self._path.write_text(json.dumps(payload, cls=_Serializer, indent=2))
        self._mtime_ns = self._path.stat().st_mtime_ns

    def _maybe_reload(self) -> None:
        """Refresh state when another process writes the shared store file."""
        if not self._path.exists():
            return
        try:
            current_mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            return
        if self._mtime_ns is None or current_mtime_ns > self._mtime_ns:
            self._load()

    # ── app definitions ──────────────────────────────────────────────────

    def save_app(self, definition: AppIntentDefinition) -> None:
        self._maybe_reload()
        self._apps[definition.app_id] = definition
        self._flush()

    def get_app(self, app_id: str) -> AppIntentDefinition | None:
        self._maybe_reload()
        return self._apps.get(app_id)

    def list_apps(self, deployer: str | None = None) -> list[AppIntentDefinition]:
        self._maybe_reload()
        apps = list(self._apps.values())
        if deployer:
            apps = [a for a in apps if a.deployer.lower() == deployer.lower()]
        return apps

    def delete_app(self, app_id: str) -> bool:
        self._maybe_reload()
        if app_id in self._apps:
            del self._apps[app_id]
            self._flush()
            return True
        return False

    # ── wallets ──────────────────────────────────────────────────────────

    def save_wallet(self, wallet: WalletInfo) -> None:
        self._maybe_reload()
        self._wallets[wallet.address] = wallet
        self._flush()

    def get_wallet(self, address: str) -> WalletInfo | None:
        self._maybe_reload()
        return self._wallets.get(address)

    def list_wallets(self) -> list[WalletInfo]:
        self._maybe_reload()
        return list(self._wallets.values())

    # ── deployments ──────────────────────────────────────────────────────

    def save_deployment(self, result: DeploymentResult) -> None:
        self._maybe_reload()
        if result.app_id not in self._deployments:
            self._deployments[result.app_id] = {}
        self._deployments[result.app_id][result.chain_id] = result
        self._flush()

    def get_deployment(
        self, app_id: str, chain_id: int | None = None,
    ) -> DeploymentResult | None:
        """Return deployment for an app, optionally for a specific chain.

        If chain_id is None, returns the first order-ready deployment (or first
        operational, or first overall).
        """
        self._maybe_reload()
        chain_map = self._deployments.get(app_id)
        if chain_map is None:
            return None
        if chain_id is not None:
            return chain_map.get(chain_id)
        # Prefer order-ready (SOLVED/ACTIVE), then operational, then any
        for dep in chain_map.values():
            if dep.status.is_order_ready():
                return dep
        for dep in chain_map.values():
            if dep.status.is_operational():
                return dep
        return next(iter(chain_map.values()), None)

    def update_deployment_status(
        self, app_id: str, chain_id: int, status: AppStatus,
    ) -> bool:
        """Update a deployment's status without replacing the record."""
        self._maybe_reload()
        chain_map = self._deployments.get(app_id)
        if chain_map is None or chain_id not in chain_map:
            return False
        chain_map[chain_id].status = status
        self._flush()
        return True

    def get_deployments(self, app_id: str) -> dict[int, DeploymentResult]:
        """Return all per-chain deployments for an app."""
        self._maybe_reload()
        return dict(self._deployments.get(app_id, {}))

    # ── orders (OrderBook persistence) ──────────────────────────────────

    def save_order(self, order_dict: dict[str, Any]) -> None:
        """Save or update an order."""
        self._maybe_reload()
        order_id = order_dict["order_id"]
        self._orders[order_id] = order_dict
        self._flush()

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Return an order by ID, or None if not found."""
        self._maybe_reload()
        return self._orders.get(order_id)

    def list_orders(
        self, app_id: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List orders, optionally filtered."""
        self._maybe_reload()
        orders = list(self._orders.values())
        if app_id:
            orders = [o for o in orders if o.get("app_id") == app_id]
        if status:
            orders = [o for o in orders if o.get("status") == status]
        return orders

    def update_order(self, order_id: str, updates: dict[str, Any]) -> bool:
        """Apply partial updates to an order. Returns True if found."""
        self._maybe_reload()
        order = self._orders.get(order_id)
        if order is None:
            return False
        order.update(updates)
        self._flush()
        return True

    # ── native bittensor permissions / executions ────────────────────────

    def save_native_permission(self, permission: NativeBittensorPermission) -> None:
        """Save or update a native Bittensor delegated permission."""
        self._maybe_reload()
        self._native_permissions[permission.permission_id] = permission
        self._flush()

    def get_native_permission(self, permission_id: str) -> NativeBittensorPermission | None:
        """Return a native Bittensor delegated permission by ID."""
        self._maybe_reload()
        return self._native_permissions.get(permission_id)

    def list_native_permissions(
        self,
        owner_ss58: str | None = None,
        delegate_ss58: str | None = None,
        status: NativeBittensorPermissionStatus | None = None,
    ) -> list[NativeBittensorPermission]:
        """List native Bittensor delegated permissions with optional filters."""
        self._maybe_reload()
        permissions = list(self._native_permissions.values())
        if owner_ss58:
            permissions = [p for p in permissions if p.owner_ss58 == owner_ss58]
        if delegate_ss58:
            permissions = [p for p in permissions if p.delegate_ss58 == delegate_ss58]
        if status is not None:
            permissions = [p for p in permissions if p.status is status]
        return permissions

    def save_native_execution(self, record: NativeBittensorExecutionRecord) -> None:
        """Save or update a native Bittensor execution audit record."""
        self._maybe_reload()
        self._native_executions[record.execution_id] = record
        self._flush()

    def get_native_execution(self, execution_id: str) -> NativeBittensorExecutionRecord | None:
        """Return a native Bittensor execution audit record by ID."""
        self._maybe_reload()
        return self._native_executions.get(execution_id)

    def list_native_executions(
        self,
        permission_id: str | None = None,
        owner_ss58: str | None = None,
        status: NativeBittensorExecutionStatus | None = None,
    ) -> list[NativeBittensorExecutionRecord]:
        """List native Bittensor execution audit records with optional filters."""
        self._maybe_reload()
        records = list(self._native_executions.values())
        if permission_id:
            records = [r for r in records if r.permission_id == permission_id]
        if owner_ss58:
            records = [r for r in records if r.owner_ss58 == owner_ss58]
        if status is not None:
            records = [r for r in records if r.status is status]
        return sorted(records, key=lambda r: (r.submitted_at, r.execution_id))

    # ── execution stats ──────────────────────────────────────────────────

    def record_execution(
        self, app_id: str, score: float, success: bool
    ) -> None:
        """Record an execution event for statistics."""
        self._maybe_reload()
        if app_id not in self._app_stats:
            self._app_stats[app_id] = {
                "total_executions": 0,
                "successful_executions": 0,
                "total_score": 0.0,
                "best_score": 0.0,
                "last_triggered": 0.0,
                "recent_scores": [],
            }
        stats = self._app_stats[app_id]
        stats["total_executions"] += 1
        if success:
            stats["successful_executions"] += 1
        stats["total_score"] += score
        stats["best_score"] = max(stats["best_score"], score)
        stats["last_triggered"] = time.time()
        # Keep last 50 scores
        stats["recent_scores"].append(score)
        stats["recent_scores"] = stats["recent_scores"][-50:]
        self._flush()

    def get_stats(self, app_id: str) -> dict[str, Any]:
        """Return execution statistics for an app."""
        self._maybe_reload()
        stats = self._app_stats.get(app_id, {})
        total = stats.get("total_executions", 0)
        return {
            "total_executions": total,
            "successful_executions": stats.get("successful_executions", 0),
            "avg_score": (
                stats["total_score"] / total if total > 0 else 0.0
            ),
            "best_score": stats.get("best_score", 0.0),
            "last_triggered": stats.get("last_triggered", 0.0),
            "recent_scores": stats.get("recent_scores", []),
        }

    # ── quote demand stats ────────────────────────────────────────────────

    def record_quote_attempt(
        self, app_id: str, success: bool, error: str = "",
    ) -> None:
        """Record a quote attempt for demand tracking."""
        self._maybe_reload()
        if app_id not in self._quote_stats:
            self._quote_stats[app_id] = {
                "total_quotes": 0,
                "failed_quotes": 0,
                "last_quote_at": 0.0,
                "recent_errors": [],
            }
        qs = self._quote_stats[app_id]
        qs["total_quotes"] += 1
        qs["last_quote_at"] = time.time()
        if not success:
            qs["failed_quotes"] += 1
            if error:
                qs["recent_errors"].append(error)
                qs["recent_errors"] = qs["recent_errors"][-20:]
        self._flush()

    def get_quote_stats(self, app_id: str) -> dict[str, Any]:
        """Return quote demand statistics for an app."""
        self._maybe_reload()
        qs = self._quote_stats.get(app_id, {})
        total = qs.get("total_quotes", 0)
        failed = qs.get("failed_quotes", 0)
        return {
            "total_quotes": total,
            "failed_quotes": failed,
            "success_rate": (total - failed) / total if total > 0 else 0.0,
            "last_quote_at": qs.get("last_quote_at", 0.0),
            "recent_errors": qs.get("recent_errors", []),
        }
