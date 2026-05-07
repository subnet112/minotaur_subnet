"""Bridge adapter registry — discovers and manages available bridges."""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.bridge.base import BridgeAdapter, BridgeQuote

logger = logging.getLogger(__name__)


class BridgeRegistry:
    """Registry of available bridge adapters.

    Provides lookup by protocol name and route discovery for finding
    which bridges support a given source→destination chain pair.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, BridgeAdapter] = {}

    def register(self, adapter: BridgeAdapter) -> None:
        """Register a bridge adapter."""
        self._adapters[adapter.PROTOCOL] = adapter
        logger.info("Registered bridge adapter: %s", adapter.PROTOCOL)

    def get(self, protocol: str) -> BridgeAdapter | None:
        """Get an adapter by protocol name."""
        return self._adapters.get(protocol)

    def find_bridge(
        self, src_chain_id: int, dst_chain_id: int,
    ) -> list[BridgeAdapter]:
        """Find all adapters that support the given route."""
        result = []
        for adapter in self._adapters.values():
            if (src_chain_id, dst_chain_id) in adapter.supported_routes():
                result.append(adapter)
        return result

    async def best_quote(
        self,
        token_in: str,
        amount: int,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeQuote | None:
        """Get the best quote across all adapters for a route.

        Accepts token_in as plain 0x address or CAIP-10 format
        (``eip155:chain_id:0xaddress``). If CAIP-10, extracts chain context.

        Queries all adapters that support the route and returns the one
        with the highest estimated output.  Returns ``None`` if no adapters
        support the route or if all adapter quotes fail.
        """
        # Parse CAIP-10 if present
        if isinstance(token_in, str) and token_in.startswith("eip155:"):
            try:
                from minotaur_subnet.shared.interop_address import InteropAddress
                ia = InteropAddress.parse(token_in)
                token_in = ia.address
                if ia.chain_id is not None:
                    src_chain_id = ia.chain_id
            except ValueError:
                pass

        adapters = self.find_bridge(src_chain_id, dst_chain_id)
        if not adapters:
            return None

        best: BridgeQuote | None = None
        for adapter in adapters:
            try:
                quote = await adapter.quote(
                    token_in, amount, src_chain_id, dst_chain_id,
                )
                if best is None or quote.estimated_output > best.estimated_output:
                    best = quote
            except Exception as exc:
                logger.warning(
                    "Bridge quote failed for %s: %s", adapter.PROTOCOL, exc,
                )
        return best

    @property
    def protocols(self) -> list[str]:
        """List registered protocol names."""
        return list(self._adapters.keys())

    def __len__(self) -> int:
        return len(self._adapters)
