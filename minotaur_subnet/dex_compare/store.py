"""SQLite persistence for DEX comparisons.

Reuses the ``AppIntentStore`` connection pattern (WAL, ``busy_timeout``,
autocommit, short-lived per-op connection) so it is safe across the API's
threads. All token amounts are stored as TEXT decimal strings — SQLite INTEGER
is 64-bit and swap outputs routinely exceed it; magnitude math is done in Python.

Every method here is synchronous; callers on the event loop MUST wrap calls in
``asyncio.to_thread`` (see ``worker.py``) to avoid stalling the loop.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import ComparisonRow

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 3


class DexCompareStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        self._db_path = path if path.suffix == ".db" else path.with_suffix(".db")
        self._ensure_schema()

    # ── connection / schema ──────────────────────────────────────────────
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
                CREATE TABLE IF NOT EXISTS comparisons(
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at       REAL    NOT NULL,
                    chain_id         INTEGER NOT NULL,
                    order_id         TEXT,
                    app_id           TEXT,
                    intent_function  TEXT,
                    input_token      TEXT NOT NULL,
                    output_token     TEXT NOT NULL,
                    input_amount     TEXT NOT NULL,
                    input_decimals   INTEGER,
                    output_decimals  INTEGER,
                    input_symbol     TEXT,
                    output_symbol    TEXT,
                    input_is_native  INTEGER NOT NULL DEFAULT 0,
                    output_is_native INTEGER NOT NULL DEFAULT 0,
                    gas_price_wei    TEXT,
                    mino_status      TEXT NOT NULL,
                    mino_output      TEXT,
                    mino_gas_units   INTEGER,
                    mino_fee_wei     TEXT,
                    mino_dex         TEXT,
                    trade_source     TEXT,
                    notional_usd     REAL,
                    native_usd       REAL,
                    results_json     TEXT NOT NULL,
                    schema_version   INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_dc_chain_time
                    ON comparisons(chain_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_dc_time
                    ON comparisons(created_at);
                """
            )
            # Migrate pre-existing DBs (leader) that lack the newer columns.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(comparisons)")}
            for col in ("notional_usd", "native_usd"):
                if col not in existing:
                    conn.execute(f"ALTER TABLE comparisons ADD COLUMN {col} REAL")
            # ALTER ... ADD COLUMN (no default) is O(1) and back-fills NULL on
            # existing rows — NULL reads as legacy/"historical" downstream.
            if "trade_source" not in existing:
                conn.execute("ALTER TABLE comparisons ADD COLUMN trade_source TEXT")

    # ── writes ───────────────────────────────────────────────────────────
    def insert(self, row: ComparisonRow) -> int:
        results = {src: outcome.as_dict() for src, outcome in row.outcomes.items()}
        mino = row.outcomes.get("minotaur")
        trade = row.trade
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO comparisons(
                    created_at, chain_id, order_id, app_id, intent_function,
                    input_token, output_token, input_amount, input_decimals,
                    output_decimals, input_symbol, output_symbol,
                    input_is_native, output_is_native, gas_price_wei,
                    mino_status, mino_output, mino_gas_units, mino_fee_wei, mino_dex,
                    trade_source, notional_usd, native_usd, results_json, schema_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.created_at, trade.chain_id, trade.order_id, trade.app_id,
                    trade.intent_function, trade.input_token, trade.output_token,
                    trade.input_amount, trade.input_decimals, trade.output_decimals,
                    trade.input_symbol, trade.output_symbol,
                    1 if trade.input_is_native else 0,
                    1 if trade.output_is_native else 0,
                    row.gas_price_wei,
                    mino.status if mino else "error",
                    mino.output_raw if mino else None,
                    mino.gas_units if mino else None,
                    mino.fee_raw if mino else None,
                    mino.dex if mino else None,
                    trade.trade_source, trade.notional_usd, row.native_usd,
                    json.dumps(results, separators=(",", ":")),
                    _SCHEMA_VERSION,
                ),
            )
            return int(cur.lastrowid)

    def prune(self, older_than_ts: float, max_rows: int | None = None) -> int:
        """Delete rows older than ``older_than_ts`` and, if set, cap to newest
        ``max_rows``. Returns the number of rows deleted."""
        deleted = 0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM comparisons WHERE created_at < ?", (older_than_ts,),
            )
            deleted += cur.rowcount or 0
            if max_rows is not None and max_rows > 0:
                cur = conn.execute(
                    """
                    DELETE FROM comparisons WHERE id NOT IN (
                        SELECT id FROM comparisons ORDER BY created_at DESC LIMIT ?
                    )
                    """,
                    (max_rows,),
                )
                deleted += cur.rowcount or 0
        return deleted

    # ── reads ────────────────────────────────────────────────────────────
    def fetch_since(
        self,
        chain_id: int | None,
        since_ts: float,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return comparison rows newer than ``since_ts`` (newest-first)."""
        query = "SELECT * FROM comparisons WHERE created_at >= ?"
        params: list[Any] = [since_ts]
        if chain_id is not None:
            query += " AND chain_id = ?"
            params.append(chain_id)
        query += " ORDER BY created_at DESC"
        if limit is not None and limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def distinct_chains(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT chain_id FROM comparisons ORDER BY chain_id",
            ).fetchall()
        return [int(r["chain_id"]) for r in rows]

    def count(self, chain_id: int | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM comparisons"
        params: list[Any] = []
        if chain_id is not None:
            query += " WHERE chain_id = ?"
            params.append(chain_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            results = json.loads(row["results_json"])
        except (ValueError, TypeError):
            results = {}
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "chain_id": int(row["chain_id"]),
            "order_id": row["order_id"],
            "app_id": row["app_id"],
            "intent_function": row["intent_function"],
            "input_token": row["input_token"],
            "output_token": row["output_token"],
            "input_amount": row["input_amount"],
            "input_decimals": row["input_decimals"],
            "output_decimals": row["output_decimals"],
            "input_symbol": row["input_symbol"],
            "output_symbol": row["output_symbol"],
            "input_is_native": bool(row["input_is_native"]),
            "output_is_native": bool(row["output_is_native"]),
            "gas_price_wei": row["gas_price_wei"],
            "trade_source": row["trade_source"] if "trade_source" in row.keys() else None,
            "notional_usd": row["notional_usd"] if "notional_usd" in row.keys() else None,
            "native_usd": row["native_usd"] if "native_usd" in row.keys() else None,
            "results": results,
        }
