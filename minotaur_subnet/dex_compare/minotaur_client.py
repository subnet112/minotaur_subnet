"""Fetch a Minotaur quote for a trade via the public ``/quote`` endpoint.

Called over loopback (``http://127.0.0.1:8080``) so it exercises exactly the
same handler users hit. The comparison basis is ``estimated_output_gross``
(raw swap output before our platform fee).
"""

from __future__ import annotations

import logging
import time

import aiohttp

from .backoff import request_with_backoff
from .config import DexCompareConfig
from .models import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARMING_UP,
    QuoteOutcome,
    TradeDescriptor,
    str_or_none,
    to_int,
)

logger = logging.getLogger(__name__)

_SOURCE = "minotaur"


async def fetch_minotaur_quote(
    session: aiohttp.ClientSession,
    cfg: DexCompareConfig,
    trade: TradeDescriptor,
) -> QuoteOutcome:
    """Quote the Minotaur solver for ``trade``. Never raises."""
    url = f"{cfg.api_base_url.rstrip('/')}/v1/apps/{trade.app_id}/quote"
    body = {
        "intent_function": trade.intent_function,
        # ALWAYS send chain_id — the endpoint defaults to 1 (Ethereum).
        "chain_id": trade.chain_id,
        "params": {
            "input_token": trade.input_token,
            "output_token": trade.output_token,
            "input_amount": trade.input_amount,
        },
        "slippage_bps": cfg.slippage_bps,
    }

    started = time.monotonic()
    try:
        result = await request_with_backoff(
            session, "POST", url, json_body=body, max_retries=cfg.max_retries,
        )
    except Exception as exc:  # noqa: BLE001 — defensive; must never propagate
        return QuoteOutcome(_SOURCE, STATUS_ERROR, error=f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - started) * 1000)

    if not result.ok:
        # 503 = solver not wired yet (warming up). Signalled so the worker can
        # abort the whole cycle rather than record a bogus Minotaur failure.
        status = STATUS_WARMING_UP if result.status == 503 else STATUS_ERROR
        return QuoteOutcome(_SOURCE, status, latency_ms=latency, error=result.error)

    data = result.data or {}
    gross = data.get("estimated_output_gross")
    if gross is None:
        gross = data.get("estimated_output")
    gross_int = to_int(gross)
    if gross_int is None or gross_int <= 0:
        return QuoteOutcome(
            _SOURCE, STATUS_FAILED, output_raw="0", latency_ms=latency,
            error="no route / zero output",
        )

    metadata = data.get("metadata") or {}
    dex = metadata.get("dex") or data.get("route_summary")
    return QuoteOutcome(
        _SOURCE,
        STATUS_OK,
        output_raw=str(gross_int),
        gas_units=to_int(data.get("gas_estimate")),
        fee_raw=str_or_none(data.get("platform_fee_wei")),
        is_net_of_gas=False,
        dex=str_or_none(dex),
        latency_ms=latency,
    )
