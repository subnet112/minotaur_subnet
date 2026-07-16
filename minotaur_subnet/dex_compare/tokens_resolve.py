"""Resolve an order's trade params into a fully-typed :class:`TradeDescriptor`.

Token decimals are NOT stored on the order record, so they are resolved on-chain
(via ``blockchain.tokens.get_erc20_decimals``) and cached. The native sentinel
``0xEeee…eEEeE`` (and a native symbol) is mapped to the chain's wrapped native
token — every source is quoted with the wrapped ERC-20 so the comparison is
apples-to-apples (no wrap/unwrap gas asymmetry).
"""

from __future__ import annotations

import logging
from typing import Any

from web3 import Web3

from minotaur_subnet.blockchain.tokens import (
    WRAPPED_NATIVE_TOKEN,
    get_erc20_decimals,
    get_token_symbol,
    resolve_token,
)

from .models import TradeDescriptor

logger = logging.getLogger(__name__)

# Same sentinel the /quote endpoint recognises (orders.py:272).
_NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_NATIVE_SYMBOLS = frozenset({"eth", "tao", "native"})


class DecimalsCache:
    """Process-lifetime cache of ``(chain_id, address) -> decimals``."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int, str], int] = {}

    async def get(self, address: str, chain_id: int) -> int | None:
        key = (chain_id, address.lower())
        if key in self._cache:
            return self._cache[key]
        try:
            decimals = await get_erc20_decimals(address, chain_id)
        except Exception as exc:  # noqa: BLE001 — any RPC/decoding failure -> skip order
            logger.debug("decimals resolve failed for %s on %d: %s", address, chain_id, exc)
            return None
        decimals = int(decimals)
        self._cache[key] = decimals
        return decimals


def _resolve_address(token: str, chain_id: int) -> tuple[str, bool]:
    """Return ``(checksummed_address, is_wrapped_native)`` for a token identifier.

    Raises ``ValueError`` when the token cannot be resolved.
    """
    raw = (token or "").strip()
    if not raw:
        raise ValueError("empty token")

    wrapped = WRAPPED_NATIVE_TOKEN.get(chain_id)
    if raw.lower() == _NATIVE_SENTINEL or raw.lower() in _NATIVE_SYMBOLS:
        if not wrapped:
            raise ValueError(f"no wrapped native token for chain {chain_id}")
        return Web3.to_checksum_address(wrapped), True

    address, _resolved_chain = resolve_token(raw, fallback_chain_id=chain_id)
    checksummed = Web3.to_checksum_address(address)
    is_native = bool(wrapped) and checksummed.lower() == wrapped.lower()
    return checksummed, is_native


async def resolve_trade_tokens(
    order: dict[str, Any], decimals_cache: DecimalsCache,
) -> TradeDescriptor | None:
    """Build a :class:`TradeDescriptor` from an order, or ``None`` if unresolvable.

    Returns ``None`` (skip the order) when the trade triple is missing, a token
    cannot be resolved, the amount is non-positive, or decimals can't be fetched
    — never guesses decimals (a wrong value silently corrupts the comparison).
    """
    params = order.get("params") or {}
    try:
        chain_id = int(order.get("chain_id"))
    except (TypeError, ValueError):
        return None

    raw_in = params.get("input_token")
    raw_out = params.get("output_token")
    raw_amount = params.get("input_amount")
    if not (raw_in and raw_out and raw_amount is not None):
        return None

    try:
        amount = int(str(raw_amount).strip())
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    try:
        in_addr, in_native = _resolve_address(raw_in, chain_id)
        out_addr, out_native = _resolve_address(raw_out, chain_id)
    except Exception as exc:  # noqa: BLE001 — unresolvable token -> skip
        logger.debug("token resolve failed for order %s: %s", order.get("order_id"), exc)
        return None
    if in_addr.lower() == out_addr.lower():
        return None  # degenerate same-token swap

    in_dec = await decimals_cache.get(in_addr, chain_id)
    out_dec = await decimals_cache.get(out_addr, chain_id)
    if in_dec is None or out_dec is None:
        return None

    return TradeDescriptor(
        order_id=str(order.get("order_id") or ""),
        app_id=str(order.get("app_id") or ""),
        intent_function=str(order.get("intent_function") or "swap"),
        chain_id=chain_id,
        input_token=in_addr,
        output_token=out_addr,
        input_amount=str(amount),
        input_decimals=in_dec,
        output_decimals=out_dec,
        input_symbol=get_token_symbol(in_addr, chain_id),
        output_symbol=get_token_symbol(out_addr, chain_id),
        input_is_native=in_native,
        output_is_native=out_native,
    )
