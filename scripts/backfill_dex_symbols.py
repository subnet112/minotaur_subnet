"""Backfill token symbols on existing dex-compare rows.

Historic ``comparisons`` rows carry NULL ``input_symbol``/``output_symbol`` for
any token outside the small well-known registry — symbol resolution only queried
the chain from 2026-07-26 on. This resolves every distinct unlabelled token once
(registry, then on-chain ERC-20 ``symbol()``) and stamps all its rows, so the
stats/blindspots/worst_losses pair views label the whole window, not just fresh
rows.

Run INSIDE the leader api container (it has the chain RPC config):

    docker exec production-api-1 python -m scripts.backfill_dex_symbols [--dry-run]

Idempotent: only NULL symbol columns are touched; re-running is a no-op for
anything already labelled. Unresolvable tokens (bytes32 symbols, reverting or
dead contracts) are reported and left NULL.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys

from minotaur_subnet.dex_compare.tokens_resolve import SymbolCache


def collect_unlabelled(conn: sqlite3.Connection) -> dict[tuple[int, str], list[tuple[str, str]]]:
    """``(chain_id, address) -> [(column, stored_address_string), ...]`` for every
    token that appears with a NULL symbol. Stored strings are kept verbatim so
    UPDATEs match exactly (addresses are stored checksummed)."""
    targets: dict[tuple[int, str], list[tuple[str, str]]] = {}
    for addr_col, sym_col in (("input_token", "input_symbol"), ("output_token", "output_symbol")):
        rows = conn.execute(
            f"SELECT DISTINCT chain_id, {addr_col} AS addr FROM comparisons "
            f"WHERE {sym_col} IS NULL AND {addr_col} IS NOT NULL",
        ).fetchall()
        for chain_id, addr in rows:
            targets.setdefault((int(chain_id), str(addr).lower()), []).append((addr_col, str(addr)))
    return targets


async def resolve_all(
    targets: dict[tuple[int, str], list[tuple[str, str]]],
) -> dict[tuple[int, str], str]:
    cache = SymbolCache()
    resolved: dict[tuple[int, str], str] = {}
    for (chain_id, addr_lower), variants in sorted(targets.items()):
        symbol = await cache.get(variants[0][1], chain_id)
        if symbol:
            resolved[(chain_id, addr_lower)] = symbol
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="/data/dex_compare.db", help="store path")
    parser.add_argument("--dry-run", action="store_true", help="resolve + report, write nothing")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        targets = collect_unlabelled(conn)
        print(f"{len(targets)} distinct unlabelled (chain, token) entries")
        resolved = asyncio.run(resolve_all(targets))
        print(f"{len(resolved)} resolved, {len(targets) - len(resolved)} unresolvable (left NULL)")

        updated = 0
        for (chain_id, addr_lower), symbol in sorted(resolved.items()):
            for addr_col, stored in targets[(chain_id, addr_lower)]:
                sym_col = "input_symbol" if addr_col == "input_token" else "output_symbol"
                if args.dry_run:
                    print(f"  would set {sym_col}={symbol!r} for chain {chain_id} {stored}")
                    continue
                cur = conn.execute(
                    f"UPDATE comparisons SET {sym_col} = ? "
                    f"WHERE chain_id = ? AND {addr_col} = ? AND {sym_col} IS NULL",
                    (symbol, chain_id, stored),
                )
                updated += cur.rowcount or 0
        if not args.dry_run:
            print(f"updated {updated} row-columns")
        for (chain_id, addr_lower) in sorted(set(targets) - set(resolved)):
            print(f"  unresolvable: chain {chain_id} {addr_lower}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
