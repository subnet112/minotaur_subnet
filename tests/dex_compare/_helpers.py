"""Shared fakes for the dex_compare tests (offline, no network)."""

from __future__ import annotations

from typing import Any

from minotaur_subnet.dex_compare.models import (
    ComparisonRow,
    QuoteOutcome,
    TradeDescriptor,
)


class FakeResp:
    """Minimal stand-in for an aiohttp response used as an async ctx manager."""

    def __init__(self, status: int, text: str = "", headers: dict | None = None) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def text(self) -> str:
        return self._text


class _RaisingCtx:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    """Returns scripted responses (FakeResp) or raises scripted exceptions.

    ``session.request(...)`` returns the next item synchronously (it is used as
    ``async with session.request(...)``), mirroring aiohttp's ClientSession.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def request(self, method, url, headers=None, params=None, json=None):  # noqa: A002
        self.calls.append((method, url, headers, params, json))
        item = self._responses.pop(0) if self._responses else FakeResp(500)
        if isinstance(item, BaseException):
            return _RaisingCtx(item)
        return item


def make_trade(
    *,
    chain_id: int = 8453,
    input_token: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC (Base)
    output_token: str = "0x4200000000000000000000000000000000000006",  # WETH (Base)
    input_amount: str = "1000000000",
    input_decimals: int = 6,
    output_decimals: int = 18,
    input_is_native: bool = False,
    output_is_native: bool = True,
) -> TradeDescriptor:
    return TradeDescriptor(
        order_id="ord_1",
        app_id="app_abc123",
        intent_function="swap",
        chain_id=chain_id,
        input_token=input_token,
        output_token=output_token,
        input_amount=input_amount,
        input_decimals=input_decimals,
        output_decimals=output_decimals,
        input_symbol="USDC",
        output_symbol="WETH",
        input_is_native=input_is_native,
        output_is_native=output_is_native,
    )


def make_row(
    outcomes: dict[str, QuoteOutcome],
    *,
    created_at: float = 1_000_000.0,
    gas_price_wei: str | None = "1000000000",
    trade: TradeDescriptor | None = None,
) -> ComparisonRow:
    return ComparisonRow(
        created_at=created_at,
        trade=trade or make_trade(),
        gas_price_wei=gas_price_wei,
        outcomes=outcomes,
    )


def outcome(source: str, status: str = "ok", **kw: Any) -> QuoteOutcome:
    return QuoteOutcome(source=source, status=status, **kw)
