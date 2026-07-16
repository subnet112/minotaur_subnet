"""Tests for token/decimals resolution."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from minotaur_subnet.blockchain.tokens import WRAPPED_NATIVE_TOKEN
from minotaur_subnet.dex_compare.tokens_resolve import (
    DecimalsCache,
    _resolve_address,
    resolve_trade_tokens,
)

_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH_BASE = "0x4200000000000000000000000000000000000006"
_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def _run(coro):
    return asyncio.run(coro)


def _order(**params):
    base = {
        "input_token": _USDC_BASE,
        "output_token": _WETH_BASE,
        "input_amount": "1000000",
    }
    base.update(params)
    return {
        "order_id": "o1", "app_id": "app_1", "intent_function": "swap",
        "chain_id": 8453, "params": base,
    }


def test_native_sentinel_maps_to_wrapped():
    addr, is_native = _resolve_address(_SENTINEL, 8453)
    assert addr.lower() == WRAPPED_NATIVE_TOKEN[8453].lower()
    assert is_native is True


def test_wrapped_native_address_flagged_native():
    _addr, is_native = _resolve_address(_WETH_BASE, 8453)
    assert is_native is True


def test_resolve_trade_builds_descriptor():
    cache = DecimalsCache()
    with patch(
        "minotaur_subnet.dex_compare.tokens_resolve.get_erc20_decimals",
        new=AsyncMock(side_effect=[6, 18]),
    ):
        trade = _run(resolve_trade_tokens(_order(), cache))
    assert trade is not None
    assert trade.chain_id == 8453
    assert trade.input_decimals == 6 and trade.output_decimals == 18
    assert trade.output_is_native is True and trade.input_is_native is False
    assert trade.input_amount == "1000000"


def test_decimals_cache_reuses(tmp_path=None):
    cache = DecimalsCache()
    mock = AsyncMock(return_value=6)
    with patch("minotaur_subnet.dex_compare.tokens_resolve.get_erc20_decimals", new=mock):
        _run(cache.get(_USDC_BASE, 8453))
        _run(cache.get(_USDC_BASE, 8453))
    assert mock.await_count == 1  # second call served from cache


def test_unresolved_decimals_skips_order():
    cache = DecimalsCache()
    with patch(
        "minotaur_subnet.dex_compare.tokens_resolve.get_erc20_decimals",
        new=AsyncMock(side_effect=RuntimeError("rpc down")),
    ):
        trade = _run(resolve_trade_tokens(_order(), cache))
    assert trade is None


def test_missing_output_token_returns_none():
    order = {"chain_id": 8453, "params": {"input_token": _USDC_BASE, "input_amount": "1"}}
    assert _run(resolve_trade_tokens(order, DecimalsCache())) is None


def test_nonpositive_amount_returns_none():
    with patch(
        "minotaur_subnet.dex_compare.tokens_resolve.get_erc20_decimals",
        new=AsyncMock(side_effect=[6, 18]),
    ):
        assert _run(resolve_trade_tokens(_order(input_amount="0"), DecimalsCache())) is None
