"""Pluggable trade SOURCES for the DEX-compare worker.

A source yields AT MOST one order-shaped dict per chain per cycle. Everything
downstream (``resolve_trade_tokens`` → ``fetch_minotaur_quote`` → ``_fan_out`` →
``store``) is source-agnostic and unchanged — a source's whole job is to answer
"what trade should we quote next on this chain?".

Two sources:

* ``HistoricalOrderSource`` — the original behaviour: sample our own terminal
  orders (one random candidate per chain).
* ``CowOnchainSource`` — sample REAL executed trades from CoW Protocol's
  ``GPv2Settlement`` ``Trade`` events. We take ONLY (sellToken, buyToken,
  sellAmount) from each event and requote every source live, so the delivered
  amount is irrelevant and there is no block/time mismatch. Because the trade
  actually executed on-chain it is liquid at that size by construction, so no
  USD-normalization is applied. Whether the Minotaur solver can requote it is a
  genuine COVERAGE signal (see ``stats._top_unservable`` / the coverage block).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Protocol, runtime_checkable

from web3 import Web3

from minotaur_subnet.blockchain.chains import get_web3

from .config import DexCompareConfig
from .models import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# CoW Protocol GPv2Settlement — the SAME canonical address on every chain
# (Ethereum 1, Base 8453, …). It emits one Trade event per filled order.
GPV2_SETTLEMENT = Web3.to_checksum_address("0x9008D19f58AAbD9eD0D60971565AA8510560ab41")
# keccak("Trade(address,address,address,uint256,uint256,uint256,bytes)")
TRADE_TOPIC0 = "0xa07a543ab8a018198e99ca0184c93fe9050a79400a0a723441f84de1d972cc17"

OrderLike = dict[str, Any]  # {order_id, app_id, chain_id, intent_function, status, params:{...}}


@runtime_checkable
class TradeSource(Protocol):
    name: str  # "historical" | "cow_onchain" — persisted on every row

    async def sample(self, chains: tuple[int, ...]) -> list[OrderLike]:
        ...


# ── candidate filter (was DexCompareWorker._is_candidate) ────────────────────
def is_candidate(order: dict[str, Any], supported: tuple[int, ...]) -> bool:
    """A same-chain terminal swap order we can quote apples-to-apples."""
    if str(order.get("status", "")).lower() not in TERMINAL_STATUSES:
        return False
    try:
        chain_id = int(order.get("chain_id"))
    except (TypeError, ValueError):
        return False
    if chain_id not in supported:
        return False
    params = order.get("params") or {}
    if not (
        params.get("input_token")
        and params.get("output_token")
        and params.get("input_amount") is not None
    ):
        return False
    # Cross-chain orders can't be quoted by same-chain aggregators.
    if params.get("dest_chain_id") is not None:
        return False
    in_chain = params.get("input_chain_id")
    out_chain = params.get("output_chain_id")
    if in_chain is not None and out_chain is not None:
        try:
            if int(in_chain) != int(out_chain):
                return False
        except (TypeError, ValueError):
            return False
    return True


class HistoricalOrderSource:
    """Sample our own terminal orders — one random candidate per chain."""

    name = "historical"

    def __init__(self, app_store: Any, cfg: DexCompareConfig, rng: random.Random) -> None:
        self._app_store = app_store
        self._cfg = cfg
        self._rng = rng

    async def sample(self, chains: tuple[int, ...]) -> list[OrderLike]:
        orders = await asyncio.to_thread(self._app_store.list_orders)
        by_chain: dict[int, list[dict[str, Any]]] = {}
        for order in orders:
            if is_candidate(order, self._cfg.supported_chain_ids):
                by_chain.setdefault(int(order["chain_id"]), []).append(order)
        out: list[OrderLike] = []
        for chain_id in chains:  # per-chain draw (minority chains never starve)
            pool = by_chain.get(chain_id)
            if pool:
                out.append(self._rng.choice(pool))
        return out


class CowOnchainSource:
    """Sample real executed trades from CoW GPv2Settlement Trade events."""

    name = "cow_onchain"

    def __init__(self, app_store: Any, cfg: DexCompareConfig, rng: random.Random) -> None:
        self._app_store = app_store
        self._cfg = cfg
        self._rng = rng
        # chain -> (app_id, intent_function) to requote a real CoW trade through.
        self._surface_cache: dict[int, tuple[str, str]] = {}

    # -- the app surface to requote against (per chain) -----------------------
    async def _app_surface(self, chain_id: int) -> tuple[str, str] | None:
        """(app_id, intent_function) to POST /apps/{app_id}/quote for this chain.

        A DexAggregator app is deployed per chain, so we need a CHAIN-MATCHING
        surface. Prefer an explicit override; else borrow one from a recent
        same-chain swap order (guaranteed quotable). Coverage then reads "can the
        current solver requote this real trade through the surface users use".
        """
        override = self._cfg.cow_app_ids.get(chain_id)
        if override:
            return override, "swap"
        cached = self._surface_cache.get(chain_id)
        if cached:
            return cached
        orders = await asyncio.to_thread(self._app_store.list_orders)
        for order in orders:
            try:
                if int(order.get("chain_id")) != chain_id:
                    continue
            except (TypeError, ValueError):
                continue
            aid = order.get("app_id")
            params = order.get("params") or {}
            if aid and params.get("input_token") and params.get("output_token"):
                surface = (str(aid), str(order.get("intent_function") or "swap"))
                self._surface_cache[chain_id] = surface
                return surface
        return None

    async def sample(self, chains: tuple[int, ...]) -> list[OrderLike]:
        out: list[OrderLike] = []
        for chain_id in chains:
            try:
                surface = await self._app_surface(chain_id)
                if surface is None:
                    logger.warning(
                        "cow-source: no app surface to requote against (chain %d)", chain_id
                    )
                    continue
                events = await self._fetch_events(chain_id)
            except Exception as exc:  # noqa: BLE001 — a source must never kill the loop
                logger.warning("cow-source: sample failed on chain %d: %s", chain_id, exc)
                continue

            candidates = self._collect(events)
            if not candidates:
                continue
            sell, buy, amount = self._rng.choice(candidates)
            app_id, intent_function = surface
            out.append(
                {
                    "order_id": f"cow:{chain_id}:{sell}:{buy}:{amount}",
                    "app_id": app_id,
                    "chain_id": chain_id,
                    "intent_function": intent_function,
                    "status": "filled",
                    "params": {
                        "input_token": sell,
                        "output_token": buy,
                        "input_amount": str(amount),
                    },
                }
            )
        return out

    def _collect(self, events: list) -> list[tuple[str, str, int]]:
        """Decode + filter + dedup raw logs into distinct (sell, buy, amount)."""
        seen: dict[tuple, tuple[str, str, int]] = {}
        for log in events:
            decoded = decode_trade(log)
            if decoded is None:
                continue
            sell, buy, amount = decoded
            if amount <= 0 or sell.lower() == buy.lower():
                continue
            if self._cfg.cow_dedup_by_pair:
                key: tuple = (sell.lower(), buy.lower())  # keep the fattest per pair
                prev = seen.get(key)
                if prev is not None and prev[2] >= amount:
                    continue
            else:
                key = (sell.lower(), buy.lower(), amount)  # collapse batch duplicates only
            seen[key] = (sell, buy, amount)
        return list(seen.values())

    async def _fetch_events(self, chain_id: int) -> list:
        w3 = get_web3(chain_id)
        head = await asyncio.to_thread(lambda: int(w3.eth.block_number))
        lookback = self._cfg.cow_lookback_blocks.get(chain_id, self._cfg.cow_lookback_default)
        span = max(1, self._cfg.cow_max_block_span)
        lo = max(0, head - lookback)
        events: list = []
        while lo <= head:
            hi = min(lo + span - 1, head)
            try:
                chunk = await asyncio.to_thread(
                    lambda a=lo, b=hi: w3.eth.get_logs(
                        {
                            "address": GPV2_SETTLEMENT,
                            "topics": [TRADE_TOPIC0],
                            "fromBlock": a,
                            "toBlock": b,
                        }
                    )
                )
                events.extend(chunk)
                lo = hi + 1
            except Exception as exc:  # noqa: BLE001
                if _is_range_cap(exc) and span > self._cfg.cow_min_block_span:
                    span = max(self._cfg.cow_min_block_span, span // 2)
                    continue  # retry the SAME lo with a smaller window
                logger.warning(
                    "cow-source: get_logs [%d,%d] chain %d failed: %s", lo, hi, chain_id, exc
                )
                break  # a partial window is fine — sample from what we got
        return events


def build_source(cfg: DexCompareConfig, app_store: Any, rng: random.Random) -> TradeSource:
    if cfg.source.strip().lower() == "cow_onchain":
        return CowOnchainSource(app_store, cfg, rng)
    return HistoricalOrderSource(app_store, cfg, rng)


# ── raw eth_getLogs decode ───────────────────────────────────────────────────
# web3 returns AttributeDict entries whose ``data`` is HexBytes (a bytes
# subclass) and ``topics`` are HexBytes; raw JSON-RPC returns plain hex strings.
# The helpers below accept either shape.
def _to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):  # HexBytes is a bytes subclass
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if hasattr(value, "hex"):
        return bytes.fromhex(value.hex().removeprefix("0x"))
    raise TypeError(f"undecodable log field: {type(value)!r}")


def _topic0_hex(log: Any) -> str:
    topics = (log.get("topics") if hasattr(log, "get") else log["topics"]) or []
    if not topics:
        return ""
    first = topics[0]
    h = first.hex() if hasattr(first, "hex") else str(first)
    return (h if h.startswith("0x") else "0x" + h).lower()


def decode_trade(log: Any) -> tuple[str, str, int] | None:
    """Decode one Trade log → (sellToken, buyToken, sellAmount); None if invalid.

    Non-indexed data layout: word0=sellToken, word1=buyToken, word2=sellAmount,
    word3=buyAmount, word4=feeAmount, then dynamic orderUid. ``owner`` is indexed
    (topics[1]). We use only sell/buy tokens + sellAmount — everything else
    (buyAmount, feeAmount, orderUid, owner) is intentionally ignored (we requote).
    """
    if _topic0_hex(log) != TRADE_TOPIC0:  # defense-in-depth; already topic-filtered
        return None
    data = _to_bytes(log.get("data") if hasattr(log, "get") else log["data"])
    if len(data) < 96:  # need words 0..2
        return None
    sell = "0x" + data[12:32].hex()   # word0, low 20 bytes
    buy = "0x" + data[44:64].hex()    # word1, low 20 bytes
    amount = int.from_bytes(data[64:96], "big")  # word2, uint256 big-endian
    return Web3.to_checksum_address(sell), Web3.to_checksum_address(buy), amount


_RANGE_CAP_MARKERS = (
    "query returned more than",
    "more than 10000 results",
    "block range is too large",
    "exceed maximum block range",
    "response size exceeded",
    "-32005",
    "limit exceeded",
)


def _is_range_cap(exc: Exception) -> bool:
    """True when an RPC provider rejected an eth_getLogs range as too large."""
    message = str(exc).lower()
    return any(marker in message for marker in _RANGE_CAP_MARKERS)
