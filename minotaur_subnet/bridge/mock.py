"""Mock bridge adapter for local testnet testing.

Instantly completes bridge transfers with a configurable fee. Used for
end-to-end testing of the cross-chain pipeline without real bridges.
"""

from __future__ import annotations

from minotaur_subnet.bridge.base import (
    BridgeAdapter,
    BridgeQuote,
    BridgeStatus,
    BridgeStatusEnum,
)
from minotaur_subnet.shared.types import Interaction

# Default fee: 0.1% (10 basis points)
_DEFAULT_FEE_BPS = 10


class MockBridgeAdapter(BridgeAdapter):
    """Mock bridge that instantly completes transfers.

    Deducts a configurable fee (default 0.1%) and reports immediate
    completion. All chain pairs are supported.
    """

    PROTOCOL = "mock"

    def __init__(self, fee_bps: int = _DEFAULT_FEE_BPS) -> None:
        self._fee_bps = fee_bps
        self._transfers: dict[str, BridgeQuote] = {}

    async def quote(
        self,
        token_in: str,
        amount: int,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeQuote:
        fee = amount * self._fee_bps // 10_000
        return BridgeQuote(
            protocol=self.PROTOCOL,
            src_chain_id=src_chain_id,
            dst_chain_id=dst_chain_id,
            token_in=token_in,
            token_out=token_in,  # same token address on both sides (mock)
            amount_in=amount,
            estimated_output=amount - fee,
            fee=fee,
            estimated_duration_s=0,  # instant
            metadata={"fee_bps": self._fee_bps},
        )

    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        sender: str,
    ) -> list[Interaction]:
        # Mock: single no-op interaction representing the bridge deposit
        return [
            Interaction(
                target="0x" + "00" * 19 + "B1",  # mock bridge contract
                value="0",
                call_data=(
                    "0xbridge_mock"
                    f"_{quote.src_chain_id}"
                    f"_{quote.dst_chain_id}"
                    f"_{quote.amount_in}"
                ),
                chain_id=quote.src_chain_id,
            ),
        ]

    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        # Mock bridge always completes instantly
        return BridgeStatus(
            status=BridgeStatusEnum.COMPLETED,
            src_tx_hash=src_tx_hash,
            dst_tx_hash=f"0xmock_dst_{src_tx_hash[-8:]}",
            amount_received=None,  # caller should use quote.estimated_output
        )

    def mock_config(self, quote) -> dict:
        """Mock bridge: no real selectors to replace."""
        return {"selectors": [], "mock_type": "noop", "mock_token": "", "mock_amount": 0}

    def supported_routes(self) -> list[tuple[int, int]]:
        # Mock supports all routes
        all_chains = [1, 8453, 31337, 964, 42161, 10]
        return [
            (src, dst)
            for src in all_chains
            for dst in all_chains
            if src != dst
        ]
