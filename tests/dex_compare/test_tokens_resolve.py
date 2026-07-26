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


# ── SymbolCache (on-chain symbol resolution) ─────────────────────────────────
_UNKNOWN = "0x1111111111111111111111111111111111111111"


def test_symbol_registry_hit_skips_chain_call():
    from minotaur_subnet.dex_compare.tokens_resolve import SymbolCache
    mock = AsyncMock(return_value="NOPE")
    with patch("minotaur_subnet.dex_compare.tokens_resolve.get_erc20_symbol", new=mock):
        # USDC on Base is in the well-known registry — no RPC needed.
        assert _run(SymbolCache().get(_USDC_BASE, 8453)) == "USDC"
    assert mock.await_count == 0


def test_symbol_onchain_fallback_cached():
    from minotaur_subnet.dex_compare.tokens_resolve import SymbolCache
    cache = SymbolCache()
    mock = AsyncMock(return_value="PEPE")
    with patch("minotaur_subnet.dex_compare.tokens_resolve.get_erc20_symbol", new=mock):
        assert _run(cache.get(_UNKNOWN, 8453)) == "PEPE"
        assert _run(cache.get(_UNKNOWN, 8453)) == "PEPE"
    assert mock.await_count == 1  # second call served from cache


def test_symbol_failure_negative_cached():
    from minotaur_subnet.dex_compare.tokens_resolve import SymbolCache
    cache = SymbolCache()
    mock = AsyncMock(side_effect=RuntimeError("execution reverted"))
    with patch("minotaur_subnet.dex_compare.tokens_resolve.get_erc20_symbol", new=mock):
        assert _run(cache.get(_UNKNOWN, 8453)) is None
        assert _run(cache.get(_UNKNOWN, 8453)) is None
    assert mock.await_count == 1  # broken token not re-queried every cycle


def test_symbol_sanitized():
    from minotaur_subnet.dex_compare.tokens_resolve import _sanitize_symbol
    assert _sanitize_symbol(b"MKR\x00\x00\x00") == "MKR"       # bytes32-style
    assert _sanitize_symbol("  WETH \n") == "WETH"
    assert _sanitize_symbol("A" * 100) == "A" * 24              # length cap
    assert _sanitize_symbol("\x00\x01\x02") is None             # nothing printable
    assert _sanitize_symbol(123) is None


def test_resolve_trade_uses_symbol_cache():
    from minotaur_subnet.dex_compare.tokens_resolve import SymbolCache
    with (
        patch(
            "minotaur_subnet.dex_compare.tokens_resolve.get_erc20_decimals",
            new=AsyncMock(side_effect=[6, 18]),
        ),
        patch(
            "minotaur_subnet.dex_compare.tokens_resolve.get_erc20_symbol",
            new=AsyncMock(return_value="TOKA"),
        ),
    ):
        trade = _run(resolve_trade_tokens(
            _order(input_token=_UNKNOWN), DecimalsCache(), SymbolCache(),
        ))
    assert trade is not None
    assert trade.input_symbol == "TOKA"    # unknown token resolved on-chain
    assert trade.output_symbol == "WETH"   # wrapped native from the registry
