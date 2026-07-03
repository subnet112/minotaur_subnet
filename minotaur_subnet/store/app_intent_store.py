"""
Persistent store for App Intent definitions, wallets, deployments, orders,
and execution statistics.

Backed by SQLite (one row per record, WAL mode) so state survives restarts
and is safe under the validator + API both writing the shared store. Each
mutation is a targeted, transactional row upsert — no whole-store rewrite —
which removes the cross-process clobbering the previous single-JSON-file
backend was prone to (orders silently lost when one process flushed a stale
snapshot over another's write).

Backwards compatible: the public API is unchanged, the constructor still
accepts a ``store_path`` (a legacy ``*.json`` path is mapped to a sibling
``*.db`` and its contents imported once), so existing call sites, the
``--store-path`` flag, and tests keep working.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

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
_STORE_PATH = _STORE_DIR / "store.db"


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
        fee_mode=d.get("fee_mode", ""),
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
        # NOTE: a legacy row may still carry a stale "shadow_js_code" key (the old
        # dual-slot scorer); it is intentionally NOT read — the relative rule now
        # reads the LIVE scorer's metadata.raw_output, so the field was removed.
        solidity_code=d.get("solidity_code"),
        config=config,
        deployer=d.get("deployer", ""),
        description=d.get("description", ""),
        manifest=d.get("manifest"),
        constructor_args=d.get("constructor_args"),
        schema_id=d.get("schema_id", ""),
        policy_metadata=d.get("policy_metadata", {}),
        contract_version=d.get("contract_version", ""),
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
        tx_hash=d.get("tx_hash"),
        abi=d.get("abi"),
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


def _dumps(value: Any) -> str:
    """Serialize a record (dataclass or plain dict) to a JSON string."""
    return json.dumps(value, cls=_Serializer)


def _enum_value(value: Any) -> Any:
    """Return ``.value`` for enums, otherwise the value unchanged.

    Used for the denormalized index columns (orders.status etc.) so they
    always hold the plain string form regardless of whether a caller passed
    an enum or a string.
    """
    return value.value if hasattr(value, "value") else value


class AppIntentStore:
    """
    Persistent store for App Intent definitions, wallets, deployments, orders,
    and execution statistics.

    Each record is one row in SQLite; mutations are individual transactional
    upserts and reads query the database directly, so state survives restarts
    and concurrent writers (validator + API) never clobber each other.

    Deployments are keyed as ``(app_id, chain_id)``, supporting per-chain
    deployment tracking. Legacy single-deployment JSON entries are migrated on
    first load.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        path = Path(store_path) if store_path is not None else _STORE_PATH
        # Accept a legacy ``*.json`` path and use a sibling ``*.db`` for the
        # SQLite database; the JSON contents (if present) are imported once.
        self._legacy_json_path: Path | None = (
            path if path.suffix == ".json" else None
        )
        self._db_path = path if path.suffix == ".db" else path.with_suffix(".db")
        self._ensure_schema()
        self._migrate_from_json_if_needed()

    # ── connection / schema ────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection (WAL, autocommit).

        A fresh connection per operation keeps the store safe across threads
        (FastAPI workers) and processes (validator + API) without a shared
        mutable cache. WAL mode allows concurrent readers with a single
        writer; ``busy_timeout`` rides out brief write contention.
        """
        conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        os.makedirs(self._db_path.parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS apps(
                    app_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wallets(
                    address TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deployments(
                    app_id TEXT NOT NULL, chain_id INTEGER NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(app_id, chain_id));
                CREATE TABLE IF NOT EXISTS orders(
                    order_id TEXT PRIMARY KEY, app_id TEXT, status TEXT,
                    created_at REAL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_orders_app ON orders(app_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
                CREATE TABLE IF NOT EXISTS solver_rounds(
                    round_id TEXT PRIMARY KEY, status TEXT,
                    opened_epoch INTEGER, created_at REAL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_solver_rounds_status ON solver_rounds(status);
                CREATE INDEX IF NOT EXISTS idx_solver_rounds_created ON solver_rounds(created_at);
                CREATE TABLE IF NOT EXISTS app_stats(
                    app_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quote_stats(
                    app_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_permissions(
                    permission_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_executions(
                    execution_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS developer_nonces(
                    app_id TEXT NOT NULL, deployer TEXT NOT NULL,
                    nonce INTEGER NOT NULL, PRIMARY KEY(app_id, deployer));
                CREATE TABLE IF NOT EXISTS developer_links(
                    app_id TEXT PRIMARY KEY, evm_deployer TEXT NOT NULL,
                    ss58 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consumed_payments(
                    payment_ref TEXT PRIMARY KEY, app_id TEXT NOT NULL,
                    consumed_at REAL);
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT);
                """
            )

    def _migrate_from_json_if_needed(self) -> None:
        """One-time import of a legacy ``store.json`` into the database.

        Idempotent and concurrency-safe: guarded by a ``meta`` flag set inside
        a write transaction, so a second process (or boot) skips it.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                done = conn.execute(
                    "SELECT value FROM meta WHERE key='json_migrated'"
                ).fetchone()
                if done is not None:
                    conn.execute("COMMIT")
                    return
                imported = 0
                legacy = self._legacy_json_path
                if legacy is not None and legacy.exists():
                    try:
                        raw = json.loads(legacy.read_text())
                    except (json.JSONDecodeError, OSError) as exc:
                        print(f"[store] WARNING: could not import {legacy}: {exc}")
                        raw = None
                    if isinstance(raw, dict):
                        imported = self._import_raw(conn, raw)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('json_migrated', ?)",
                    (str(imported),),
                )
                conn.execute("COMMIT")
                if imported:
                    print(f"[store] migrated {imported} records from {legacy} → {self._db_path}")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _import_raw(self, conn: sqlite3.Connection, raw: dict[str, Any]) -> int:
        """Insert records from a legacy JSON payload. Returns count imported."""
        n = 0
        for _id, d in raw.get("apps", {}).items():
            defn = _definition_from_dict(d)
            conn.execute(
                "INSERT OR REPLACE INTO apps(app_id, data) VALUES(?, ?)",
                (defn.app_id, _dumps(defn)),
            )
            n += 1
        for _addr, d in raw.get("wallets", {}).items():
            wallet = _wallet_from_dict(d)
            conn.execute(
                "INSERT OR REPLACE INTO wallets(address, data) VALUES(?, ?)",
                (wallet.address, _dumps(wallet)),
            )
            n += 1
        for _app_id, d in raw.get("deployments", {}).items():
            if isinstance(d, dict) and "app_id" in d:
                # Legacy format: single DeploymentResult dict
                deps = [_deployment_from_dict(d)]
            elif isinstance(d, dict):
                # New format: {chain_id_str: DeploymentResult dict}
                deps = [_deployment_from_dict(dep) for dep in d.values()]
            else:
                deps = []
            for dep in deps:
                conn.execute(
                    "INSERT OR REPLACE INTO deployments(app_id, chain_id, data) "
                    "VALUES(?, ?, ?)",
                    (dep.app_id, dep.chain_id, _dumps(dep)),
                )
                n += 1
        for app_id, stats in raw.get("app_stats", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO app_stats(app_id, data) VALUES(?, ?)",
                (app_id, _dumps(stats)),
            )
            n += 1
        for app_id, stats in raw.get("quote_stats", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO quote_stats(app_id, data) VALUES(?, ?)",
                (app_id, _dumps(stats)),
            )
            n += 1
        for order_id, order in raw.get("orders", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO orders(order_id, app_id, status, created_at, data) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    order_id,
                    order.get("app_id"),
                    _enum_value(order.get("status")),
                    order.get("created_at"),
                    _dumps(order),
                ),
            )
            n += 1
        for _pid, d in raw.get("native_permissions", {}).items():
            perm = _native_permission_from_dict(d)
            conn.execute(
                "INSERT OR REPLACE INTO native_permissions(permission_id, data) "
                "VALUES(?, ?)",
                (perm.permission_id, _dumps(perm)),
            )
            n += 1
        for _eid, d in raw.get("native_executions", {}).items():
            rec = _native_execution_from_dict(d)
            conn.execute(
                "INSERT OR REPLACE INTO native_executions(execution_id, data) "
                "VALUES(?, ?)",
                (rec.execution_id, _dumps(rec)),
            )
            n += 1
        return n

    # ── app definitions ──────────────────────────────────────────────────

    def save_app(self, definition: AppIntentDefinition) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO apps(app_id, data) VALUES(?, ?) "
                "ON CONFLICT(app_id) DO UPDATE SET data=excluded.data",
                (definition.app_id, _dumps(definition)),
            )

    def get_app(self, app_id: str) -> AppIntentDefinition | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM apps WHERE app_id=?", (app_id,)
            ).fetchone()
        return _definition_from_dict(json.loads(row["data"])) if row else None

    def list_apps(self, deployer: str | None = None) -> list[AppIntentDefinition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM apps").fetchall()
        apps = [_definition_from_dict(json.loads(r["data"])) for r in rows]
        if deployer:
            apps = [a for a in apps if a.deployer.lower() == deployer.lower()]
        return apps

    def delete_app(self, app_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM apps WHERE app_id=?", (app_id,))
            return cur.rowcount > 0

    # ── developer-auth nonces ─────────────────────────────────────────────
    #
    # Monotonic, single-use nonces for EIP-712 developer authorizations (see
    # ``api/services/developer_auth``). One counter per ``(app_id, deployer)``;
    # the last *consumed* value is stored, so the next valid nonce is +1.

    def get_developer_nonce(self, app_id: str, deployer: str) -> int:
        """Last consumed developer-auth nonce for ``(app, deployer)`` (0 if none).

        The next nonce a signer should use is this value + 1.
        """
        dep = (deployer or "").strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT nonce FROM developer_nonces WHERE app_id=? AND deployer=?",
                (app_id, dep),
            ).fetchone()
        return int(row["nonce"]) if row else 0

    def consume_developer_nonce(
        self, app_id: str, deployer: str, nonce: int,
    ) -> tuple[bool, str]:
        """Atomically consume ``nonce`` for ``(app, deployer)``.

        Valid iff ``nonce == last_consumed + 1``; on success the counter
        advances. Serialized via ``BEGIN IMMEDIATE`` so two concurrent requests
        can never both consume the same nonce (replay protection). Returns
        ``(ok, error)``.
        """
        dep = (deployer or "").strip().lower()
        try:
            n = int(nonce)
        except (TypeError, ValueError):
            return False, f"invalid nonce: {nonce!r}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT nonce FROM developer_nonces WHERE app_id=? AND deployer=?",
                    (app_id, dep),
                ).fetchone()
                current = int(row["nonce"]) if row else 0
                expected = current + 1
                if n != expected:
                    conn.execute("ROLLBACK")
                    return False, f"bad nonce: got {n}, expected {expected}"
                conn.execute(
                    "INSERT INTO developer_nonces(app_id, deployer, nonce) VALUES(?,?,?) "
                    "ON CONFLICT(app_id, deployer) DO UPDATE SET nonce=excluded.nonce",
                    (app_id, dep, n),
                )
                conn.execute("COMMIT")
                return True, ""
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ── developer EVM↔SS58 links ──────────────────────────────────────────
    #
    # The coldkey SS58 bound to an app's EVM deployer (see
    # ``api/services/developer_link``). One link per app; ``evm_deployer``
    # records which EVM address authorized it (for audit / re-link detection).

    def get_payer_ss58(self, app_id: str) -> str:
        """The SS58 coldkey linked to ``app_id``'s deployer, or "" if none."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ss58 FROM developer_links WHERE app_id=?", (app_id,)
            ).fetchone()
        return str(row["ss58"]) if row else ""

    def set_payer_ss58(self, app_id: str, evm_deployer: str, ss58: str) -> None:
        """Record (or replace) the deployer↔coldkey link for ``app_id``."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO developer_links(app_id, evm_deployer, ss58) VALUES(?,?,?) "
                "ON CONFLICT(app_id) DO UPDATE SET "
                "evm_deployer=excluded.evm_deployer, ss58=excluded.ss58",
                (app_id, (evm_deployer or "").strip().lower(), ss58),
            )

    # ── consumed deploy-fee payments ──────────────────────────────────────
    #
    # One on-chain payment authorizes exactly one deploy. The payment verifier
    # consumes the payment reference here after confirming it on-chain.

    def consume_payment_ref(self, payment_ref: str, app_id: str) -> tuple[bool, str]:
        """Atomically mark ``payment_ref`` spent. Rejects if already consumed,
        so a single payment can't authorize a second deploy. Serialized via
        ``BEGIN IMMEDIATE``. Returns ``(ok, error)``.
        """
        ref = (payment_ref or "").strip()
        if not ref:
            return False, "payment_ref is required"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT app_id FROM consumed_payments WHERE payment_ref=?", (ref,)
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    return False, "payment already used for a deploy"
                conn.execute(
                    "INSERT INTO consumed_payments(payment_ref, app_id, consumed_at) "
                    "VALUES(?,?,?)",
                    (ref, app_id, time.time()),
                )
                conn.execute("COMMIT")
                return True, ""
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ── wallets ──────────────────────────────────────────────────────────

    def save_wallet(self, wallet: WalletInfo) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO wallets(address, data) VALUES(?, ?) "
                "ON CONFLICT(address) DO UPDATE SET data=excluded.data",
                (wallet.address, _dumps(wallet)),
            )

    def get_wallet(self, address: str) -> WalletInfo | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM wallets WHERE address=?", (address,)
            ).fetchone()
        return _wallet_from_dict(json.loads(row["data"])) if row else None

    def list_wallets(self) -> list[WalletInfo]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM wallets").fetchall()
        return [_wallet_from_dict(json.loads(r["data"])) for r in rows]

    # ── deployments ──────────────────────────────────────────────────────

    def save_deployment(self, result: DeploymentResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(app_id, chain_id, data) VALUES(?, ?, ?) "
                "ON CONFLICT(app_id, chain_id) DO UPDATE SET data=excluded.data",
                (result.app_id, result.chain_id, _dumps(result)),
            )

    def get_deployment(
        self, app_id: str, chain_id: int | None = None,
    ) -> DeploymentResult | None:
        """Return deployment for an app, optionally for a specific chain.

        If chain_id is None, returns the first order-ready deployment (or first
        operational, or first overall).
        """
        chain_map = self.get_deployments(app_id)
        if not chain_map:
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data FROM deployments WHERE app_id=? AND chain_id=?",
                    (app_id, chain_id),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                dep = _deployment_from_dict(json.loads(row["data"]))
                dep.status = status
                conn.execute(
                    "UPDATE deployments SET data=? WHERE app_id=? AND chain_id=?",
                    (_dumps(dep), app_id, chain_id),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_deployments(self, app_id: str) -> dict[int, DeploymentResult]:
        """Return all per-chain deployments for an app."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chain_id, data FROM deployments WHERE app_id=?", (app_id,)
            ).fetchall()
        return {
            int(r["chain_id"]): _deployment_from_dict(json.loads(r["data"]))
            for r in rows
        }

    # ── orders (OrderBook persistence) ──────────────────────────────────

    def save_order(self, order_dict: dict[str, Any]) -> None:
        """Save or update an order."""
        order_id = order_dict["order_id"]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO orders(order_id, app_id, status, created_at, data) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "app_id=excluded.app_id, status=excluded.status, "
                "created_at=excluded.created_at, data=excluded.data",
                (
                    order_id,
                    order_dict.get("app_id"),
                    _enum_value(order_dict.get("status")),
                    order_dict.get("created_at"),
                    _dumps(order_dict),
                ),
            )

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Return an order by ID, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_orders(
        self, app_id: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List orders, optionally filtered."""
        query = "SELECT data FROM orders"
        clauses: list[str] = []
        params: list[Any] = []
        if app_id:
            clauses.append("app_id=?")
            params.append(app_id)
        if status:
            clauses.append("status=?")
            params.append(_enum_value(status))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ── solver rounds (history mirror of RoundStore) ────────────────────

    def save_round(self, round_dict: dict[str, Any]) -> None:
        """Save or update a solver round record (round history)."""
        round_id = round_dict["round_id"]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO solver_rounds(round_id, status, opened_epoch, created_at, data) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(round_id) DO UPDATE SET "
                "status=excluded.status, opened_epoch=excluded.opened_epoch, "
                "created_at=excluded.created_at, data=excluded.data",
                (
                    round_id,
                    _enum_value(round_dict.get("status")),
                    round_dict.get("opened_epoch"),
                    round_dict.get("created_at"),
                    _dumps(round_dict),
                ),
            )

    def list_rounds(
        self, limit: int = 50, offset: int = 0, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Solver rounds newest-first (by created_at), paginated."""
        query = "SELECT data FROM solver_rounds"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(_enum_value(status))
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def count_rounds(self, status: str | None = None) -> int:
        """Total number of solver rounds (optionally filtered by status)."""
        query = "SELECT COUNT(*) AS n FROM solver_rounds"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(_enum_value(status))
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def update_order(self, order_id: str, updates: dict[str, Any]) -> bool:
        """Apply partial updates to an order. Returns True if found."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data FROM orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                order = json.loads(row["data"])
                order.update(updates)
                conn.execute(
                    "UPDATE orders SET app_id=?, status=?, created_at=?, data=? "
                    "WHERE order_id=?",
                    (
                        order.get("app_id"),
                        _enum_value(order.get("status")),
                        order.get("created_at"),
                        _dumps(order),
                        order_id,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def count_orders_by_status(self, app_id: str | None = None) -> dict[str, int]:
        """Return a ``{status: count}`` map over all persisted orders.

        Durable counterpart to ``IntentOrderBook.stats()`` — counts every
        persisted order (not just the live in-memory working set), so the
        ``orderbook_stats`` "total" stays consistent with ``list_orders``
        across restarts.
        """
        query = "SELECT status, COUNT(*) AS n FROM orders"
        params: list[Any] = []
        if app_id:
            query += " WHERE app_id=?"
            params.append(app_id)
        query += " GROUP BY status"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {(r["status"] if r["status"] is not None else "unknown"): int(r["n"]) for r in rows}

    def count_orders_since(self, cutoff_ts: float, app_id: str | None = None) -> int:
        """Return the number of orders created since ``cutoff_ts`` (Unix seconds).

        Counts every order that entered the system in the window — i.e. that
        "went through" Minotaur — regardless of terminal status. A generic
        order-throughput metric; the ``created_at`` index keeps it a cheap COUNT
        even on a large orders table.
        """
        query = "SELECT COUNT(*) AS n FROM orders WHERE created_at > ?"
        params: list[Any] = [float(cutoff_ts)]
        if app_id:
            query += " AND app_id=?"
            params.append(app_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0

    # ── native bittensor permissions / executions ────────────────────────

    def save_native_permission(self, permission: NativeBittensorPermission) -> None:
        """Save or update a native Bittensor delegated permission."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO native_permissions(permission_id, data) VALUES(?, ?) "
                "ON CONFLICT(permission_id) DO UPDATE SET data=excluded.data",
                (permission.permission_id, _dumps(permission)),
            )

    def get_native_permission(self, permission_id: str) -> NativeBittensorPermission | None:
        """Return a native Bittensor delegated permission by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM native_permissions WHERE permission_id=?",
                (permission_id,),
            ).fetchone()
        return _native_permission_from_dict(json.loads(row["data"])) if row else None

    def list_native_permissions(
        self,
        owner_ss58: str | None = None,
        delegate_ss58: str | None = None,
        status: NativeBittensorPermissionStatus | None = None,
    ) -> list[NativeBittensorPermission]:
        """List native Bittensor delegated permissions with optional filters."""
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM native_permissions").fetchall()
        permissions = [
            _native_permission_from_dict(json.loads(r["data"])) for r in rows
        ]
        if owner_ss58:
            permissions = [p for p in permissions if p.owner_ss58 == owner_ss58]
        if delegate_ss58:
            permissions = [p for p in permissions if p.delegate_ss58 == delegate_ss58]
        if status is not None:
            permissions = [p for p in permissions if p.status is status]
        return permissions

    def save_native_execution(self, record: NativeBittensorExecutionRecord) -> None:
        """Save or update a native Bittensor execution audit record."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO native_executions(execution_id, data) VALUES(?, ?) "
                "ON CONFLICT(execution_id) DO UPDATE SET data=excluded.data",
                (record.execution_id, _dumps(record)),
            )

    def get_native_execution(self, execution_id: str) -> NativeBittensorExecutionRecord | None:
        """Return a native Bittensor execution audit record by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM native_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        return _native_execution_from_dict(json.loads(row["data"])) if row else None

    def list_native_executions(
        self,
        permission_id: str | None = None,
        owner_ss58: str | None = None,
        status: NativeBittensorExecutionStatus | None = None,
    ) -> list[NativeBittensorExecutionRecord]:
        """List native Bittensor execution audit records with optional filters."""
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM native_executions").fetchall()
        records = [
            _native_execution_from_dict(json.loads(r["data"])) for r in rows
        ]
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
        """Record an execution event for statistics.

        Post relative-cutover, callers (the block loop) pass the on-chain
        scoreIntent BPS (0..10000) here — the delivered-quality signal — NOT the
        legacy JS 0..1 score (which is now a validity sentinel). So ``avg_score``
        / ``best_score`` / ``recent_scores`` exposed by ``get_stats`` are in BPS
        units. The store is unit-agnostic — it records whatever it's given — so
        this stays a pure storage method.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data FROM app_stats WHERE app_id=?", (app_id,)
                ).fetchone()
                stats = json.loads(row["data"]) if row else {
                    "total_executions": 0,
                    "successful_executions": 0,
                    "total_score": 0.0,
                    "successful_score": 0.0,
                    "best_score": 0.0,
                    "last_triggered": 0.0,
                    "recent_scores": [],
                }
                # Migrate pre-existing rows: seed the successful-only score sum
                # from total_score so the lifetime average stays consistent with
                # the lifetime ``successful_executions`` count. Hard failures
                # record 0.0, so total_score ≈ the successful sum already; the
                # small bias from rejected-but-scored attempts dilutes as new
                # fills accumulate. Done once, before this execution is added.
                if "successful_score" not in stats:
                    stats["successful_score"] = float(stats.get("total_score", 0.0))
                stats["total_executions"] += 1
                if success:
                    stats["successful_executions"] += 1
                    stats["successful_score"] += score
                stats["total_score"] += score
                stats["best_score"] = max(stats["best_score"], score)
                stats["last_triggered"] = time.time()
                # Keep last 50 scores
                stats["recent_scores"].append(score)
                stats["recent_scores"] = stats["recent_scores"][-50:]
                conn.execute(
                    "INSERT INTO app_stats(app_id, data) VALUES(?, ?) "
                    "ON CONFLICT(app_id) DO UPDATE SET data=excluded.data",
                    (app_id, _dumps(stats)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_stats(self, app_id: str) -> dict[str, Any]:
        """Return execution statistics for an app."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM app_stats WHERE app_id=?", (app_id,)
            ).fetchone()
        stats = json.loads(row["data"]) if row else {}
        total = stats.get("total_executions", 0)
        successful = stats.get("successful_executions", 0)
        # successful-only score sum; fall back to total_score for un-migrated rows
        # (hard failures contribute 0.0, so total_score ≈ the successful sum).
        successful_score = stats.get("successful_score", stats.get("total_score", 0.0))
        return {
            "total_executions": total,
            "successful_executions": successful,
            "avg_score": (
                stats["total_score"] / total if total > 0 else 0.0
            ),
            # Avg delivered quality over SUCCESSFUL (filled) executions only — the
            # "how good were the fills" signal, separate from the success-blended
            # ``avg_score`` (which folds in the ~0-scored failures).
            "avg_success_score": (
                successful_score / successful if successful > 0 else 0.0
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data FROM quote_stats WHERE app_id=?", (app_id,)
                ).fetchone()
                qs = json.loads(row["data"]) if row else {
                    "total_quotes": 0,
                    "failed_quotes": 0,
                    "last_quote_at": 0.0,
                    "recent_errors": [],
                }
                qs["total_quotes"] += 1
                qs["last_quote_at"] = time.time()
                if not success:
                    qs["failed_quotes"] += 1
                    if error:
                        qs["recent_errors"].append(error)
                        qs["recent_errors"] = qs["recent_errors"][-20:]
                conn.execute(
                    "INSERT INTO quote_stats(app_id, data) VALUES(?, ?) "
                    "ON CONFLICT(app_id) DO UPDATE SET data=excluded.data",
                    (app_id, _dumps(qs)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_quote_stats(self, app_id: str) -> dict[str, Any]:
        """Return quote demand statistics for an app."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM quote_stats WHERE app_id=?", (app_id,)
            ).fetchone()
        qs = json.loads(row["data"]) if row else {}
        total = qs.get("total_quotes", 0)
        failed = qs.get("failed_quotes", 0)
        return {
            "total_quotes": total,
            "failed_quotes": failed,
            "success_rate": (total - failed) / total if total > 0 else 0.0,
            "last_quote_at": qs.get("last_quote_at", 0.0),
            "recent_errors": qs.get("recent_errors", []),
        }
