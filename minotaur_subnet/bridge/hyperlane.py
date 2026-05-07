"""Hyperlane Warp Route bridge adapter.

Bridges tokens between EVM chains via Hyperlane's Warp Route protocol.
Currently supports USDC bridging between Base and Bittensor EVM using
the Astrid Bridge (formerly TaoFi) Warp Routes.

Warp Route flow:
  1. Approve underlying token to collateral contract on source chain
  2. Call transferRemote(destDomain, recipient, amount) with IGP fee
  3. Hyperlane validators sign + relayers deliver message (~1-2 min)
  4. Synthetic token minted on destination chain

Contract addresses from: https://github.com/hyperlane-xyz/hyperlane-registry
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp
from eth_abi import encode as abi_encode
from eth_hash.auto import keccak

from minotaur_subnet.bridge.base import (
    BridgeAdapter,
    BridgeQuote,
    BridgeStatus,
    BridgeStatusEnum,
)
from minotaur_subnet.shared.types import Interaction

logger = logging.getLogger(__name__)

# ── Hyperlane domain IDs (match EVM chain IDs for these chains) ──────────────

DOMAIN_IDS: dict[int, int] = {
    1: 1,         # Ethereum
    8453: 8453,   # Base
    964: 964,     # Bittensor EVM (subtensor)
}

# ── USDC Warp Route deployments ──────────────────────────────────────────────
# Source: hyperlane-registry/deployments/warp_routes/USDC/

@dataclass
class WarpRoute:
    """A deployed Hyperlane Warp Route for a specific token + chain pair."""
    src_chain_id: int
    dst_chain_id: int
    token_symbol: str
    collateral_address: str   # On source chain — call transferRemote here
    synthetic_address: str    # On dest chain — tokens arrive here
    underlying_address: str   # The actual ERC-20 on source chain

WARP_ROUTES: list[WarpRoute] = [
    # USDC: Base → Bittensor EVM
    WarpRoute(
        src_chain_id=8453,
        dst_chain_id=964,
        token_symbol="USDC",
        collateral_address="0x26af973A5b256F9B9bc0B1A3c566de1566568a87",
        synthetic_address="0xB833E8137FEDf80de7E908dc6fea43a029142F20",
        underlying_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ),
    # USDC: Bittensor EVM → Base (reverse direction)
    WarpRoute(
        src_chain_id=964,
        dst_chain_id=8453,
        token_symbol="USDC",
        collateral_address="0xB833E8137FEDf80de7E908dc6fea43a029142F20",
        synthetic_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        underlying_address="0xB833E8137FEDf80de7E908dc6fea43a029142F20",
    ),
]

# ── Hyperlane infrastructure addresses ───────────────────────────────────────

MAILBOX: dict[int, str] = {
    8453: "0xeA87ae93Fa0019a82A727bfd3eBd1cFCa8f64f1D",
    964: "0xF767D698c510FE5E53b46BA6Fd1174F5271e390A",
}

IGP: dict[int, str] = {
    8453: "0xc3F23848Ed2e04C0c6d41bd7804fa8f89F940B94",
}

# ── ABI fragments ────────────────────────────────────────────────────────────

# Warp Route: transferRemote(uint32 destination, bytes32 recipient, uint256 amount)
TRANSFER_REMOTE_SELECTOR = keccak(
    b"transferRemote(uint32,bytes32,uint256)"
)[:4]

# ERC-20 approve
APPROVE_SELECTOR = bytes.fromhex("095ea7b3")

# IGP: quoteGasPayment(uint32 destinationDomain) → uint256
QUOTE_GAS_PAYMENT_SELECTOR = keccak(
    b"quoteGasPayment(uint32)"
)[:4]

# Hyperlane Explorer API
EXPLORER_API = os.environ.get(
    "HYPERLANE_EXPLORER_API",
    "https://explorer.hyperlane.xyz/api",
)

# Default estimated bridge duration
ESTIMATED_DURATION_S = 120  # ~2 minutes for Hyperlane


class HyperlaneAdapter(BridgeAdapter):
    """Bridge adapter for Hyperlane Warp Routes.

    Supports USDC bridging between Base and Bittensor EVM via the
    Astrid Bridge (formerly TaoFi) Warp Route contracts.
    """

    PROTOCOL = "hyperlane"

    def _find_route(
        self, src_chain_id: int, dst_chain_id: int, token: str = "",
    ) -> WarpRoute | None:
        """Find a Warp Route for the given chain pair and optional token."""
        for route in WARP_ROUTES:
            if route.src_chain_id == src_chain_id and route.dst_chain_id == dst_chain_id:
                if not token or token.lower() in (
                    route.underlying_address.lower(),
                    route.token_symbol.lower(),
                ):
                    return route
        return None

    def supported_routes(self) -> list[tuple[int, int]]:
        """Return supported (src, dst) chain pairs."""
        return list({(r.src_chain_id, r.dst_chain_id) for r in WARP_ROUTES})

    def mock_config(self, quote: BridgeQuote) -> dict[str, Any]:
        """Hyperlane simulation mock: replace transferRemote with ERC-20 transfer."""
        return {
            "selectors": [TRANSFER_REMOTE_SELECTOR.hex()],
            "mock_type": "erc20_transfer",
            "mock_token": quote.token_in,
            "mock_amount": quote.amount_in,
        }

    async def quote(
        self,
        token_in: str,
        amount: int,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeQuote:
        """Get a quote for bridging via Hyperlane Warp Route.

        Hyperlane Warp Routes transfer 1:1 (no bridge fee on the token).
        The cost is the Interchain Gas Payment (IGP) paid in native ETH.
        """
        route = self._find_route(src_chain_id, dst_chain_id, token_in)
        if route is None:
            raise ValueError(
                f"No Hyperlane Warp Route for {token_in} "
                f"from chain {src_chain_id} to {dst_chain_id}"
            )

        # Estimate IGP fee via on-chain query (or use default)
        igp_fee = await self._estimate_igp_fee(src_chain_id, dst_chain_id)

        return BridgeQuote(
            protocol=self.PROTOCOL,
            src_chain_id=src_chain_id,
            dst_chain_id=dst_chain_id,
            token_in=route.underlying_address,
            token_out=route.synthetic_address,
            amount_in=amount,
            estimated_output=amount,  # 1:1 transfer, no token fee
            fee=igp_fee,              # IGP fee in native ETH (wei)
            estimated_duration_s=ESTIMATED_DURATION_S,
            metadata={
                "collateral": route.collateral_address,
                "domain_id": DOMAIN_IDS.get(dst_chain_id, dst_chain_id),
                "igp_fee_wei": igp_fee,
                "token_symbol": route.token_symbol,
            },
        )

    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        sender: str,
    ) -> list[Interaction]:
        """Build approve + transferRemote interactions for the Warp Route.

        Args:
            quote: Quote from this adapter's quote() method.
            sender: Address initiating the bridge (token holder / proxy).

        Returns:
            Two interactions:
            1. Approve underlying token to Warp Route collateral contract
            2. Call transferRemote with IGP fee as msg.value
        """
        collateral = quote.metadata["collateral"]
        domain_id = quote.metadata["domain_id"]
        igp_fee = quote.metadata.get("igp_fee_wei", 0)

        # Encode recipient as bytes32 (left-padded address)
        recipient_bytes32 = b"\x00" * 12 + bytes.fromhex(
            sender.replace("0x", "")
        )

        # 1. Approve underlying token to collateral contract
        approve_data = APPROVE_SELECTOR + abi_encode(
            ["address", "uint256"],
            [collateral, quote.amount_in],
        )

        # 2. Call transferRemote(uint32 dest, bytes32 recipient, uint256 amount)
        transfer_data = TRANSFER_REMOTE_SELECTOR + abi_encode(
            ["uint32", "bytes32", "uint256"],
            [domain_id, recipient_bytes32, quote.amount_in],
        )

        return [
            Interaction(
                target=quote.token_in,  # Underlying token (USDC)
                value="0",
                call_data="0x" + approve_data.hex(),
                chain_id=quote.src_chain_id,
            ),
            Interaction(
                target=collateral,  # Warp Route collateral contract
                value=str(igp_fee),  # Pay IGP fee in native ETH
                call_data="0x" + transfer_data.hex(),
                chain_id=quote.src_chain_id,
            ),
        ]

    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        """Check Hyperlane message delivery status.

        Two-phase check:
        1. Extract messageId from the Dispatch event in the source TX receipt.
        2. Call Mailbox.delivered(messageId) on the destination chain.

        Falls back to the Explorer API if on-chain check is unavailable.
        """
        if not src_tx_hash:
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=src_tx_hash,
            )

        tx_hash = src_tx_hash if src_tx_hash.startswith("0x") else f"0x{src_tx_hash}"

        # Phase 1: On-chain check via Mailbox.delivered(messageId)
        if dst_chain_id:
            try:
                result = await self._check_mailbox_delivered(
                    tx_hash, src_chain_id, dst_chain_id,
                )
                if result is not None:
                    return result
            except Exception as exc:
                logger.debug("On-chain bridge check failed: %s", exc)

        # Phase 2: Fallback to Explorer API
        try:
            return await self._check_explorer_api(tx_hash)
        except Exception as exc:
            logger.warning("Hyperlane status check failed: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=tx_hash,
                error=str(exc),
            )

    async def _check_mailbox_delivered(
        self,
        tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeStatus | None:
        """Check delivery via Mailbox.delivered(messageId) on destination chain.

        Extracts the messageId from the Dispatch event log in the source TX,
        then queries the destination Mailbox contract.

        Returns None if unable to determine status (missing data).
        """
        from minotaur_subnet.blockchain.chains import get_web3

        # Get source TX receipt to extract Dispatch event
        src_w3 = get_web3(src_chain_id)
        receipt = src_w3.eth.get_transaction_receipt(tx_hash)
        if not receipt or receipt.get("status") != 1:
            return BridgeStatus(
                status=BridgeStatusEnum.FAILED,
                src_tx_hash=tx_hash,
                error="Source TX failed or not found",
            )

        # Look for DispatchId(bytes32 messageId) event from the Mailbox
        dispatch_id_topic = keccak(b"DispatchId(bytes32)").hex()

        message_id = None
        for log_entry in receipt.get("logs", []):
            topics = log_entry.get("topics", [])
            if not topics:
                continue
            # Normalize: strip 0x prefix for comparison
            raw_t0 = topics[0]
            t0_hex = raw_t0.hex() if isinstance(raw_t0, bytes) else str(raw_t0).replace("0x", "")
            if t0_hex == dispatch_id_topic:
                # messageId is in topics[1]
                if len(topics) > 1:
                    mid = topics[1]
                    message_id = mid.hex() if isinstance(mid, bytes) else str(mid).replace("0x", "")
                break

        if not message_id:
            logger.debug("No DispatchId event found in TX %s", tx_hash[:16])
            return None

        # Ensure clean hex without 0x prefix for ABI encoding, then add 0x for display
        message_id = message_id.replace("0x", "")
        message_id_display = "0x" + message_id

        # Query Mailbox.delivered(bytes32) on destination chain
        dst_mailbox = MAILBOX.get(dst_chain_id)
        if not dst_mailbox:
            logger.debug("No Mailbox address for chain %d", dst_chain_id)
            return None

        dst_w3 = get_web3(dst_chain_id)
        delivered_selector = keccak(b"delivered(bytes32)")[:4]
        call_data = "0x" + delivered_selector.hex() + message_id.ljust(64, "0")

        result = dst_w3.eth.call({
            "to": dst_w3.to_checksum_address(dst_mailbox),
            "data": call_data,
        })
        is_delivered = int.from_bytes(result, "big") == 1

        if is_delivered:
            logger.info(
                "Hyperlane message %s delivered on chain %d",
                message_id_display[:18], dst_chain_id,
            )
            return BridgeStatus(
                status=BridgeStatusEnum.COMPLETED,
                src_tx_hash=tx_hash,
            )

        return BridgeStatus(
            status=BridgeStatusEnum.IN_TRANSIT,
            src_tx_hash=tx_hash,
        )

    async def _check_explorer_api(self, tx_hash: str) -> BridgeStatus:
        """Fallback: check via Hyperlane Explorer REST API."""
        url = f"{EXPLORER_API}/v1/messages"
        params = {"origin-tx-hash": tx_hash}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (404, 502, 503):
                    return BridgeStatus(
                        status=BridgeStatusEnum.PENDING,
                        src_tx_hash=tx_hash,
                    )

                if resp.status != 200:
                    return BridgeStatus(
                        status=BridgeStatusEnum.PENDING,
                        src_tx_hash=tx_hash,
                    )

                data = await resp.json()
                messages = data if isinstance(data, list) else data.get("messages", [])

                if not messages:
                    return BridgeStatus(
                        status=BridgeStatusEnum.PENDING,
                        src_tx_hash=tx_hash,
                    )

                msg = messages[0]
                delivered = msg.get("is_delivered", False)
                dst_tx = msg.get("destination", {}).get("transaction", {}).get("hash")

                if delivered:
                    return BridgeStatus(
                        status=BridgeStatusEnum.COMPLETED,
                        src_tx_hash=tx_hash,
                        dst_tx_hash=dst_tx,
                    )

                return BridgeStatus(
                    status=BridgeStatusEnum.IN_TRANSIT,
                    src_tx_hash=tx_hash,
                )

    async def _estimate_igp_fee(
        self, src_chain_id: int, dst_chain_id: int,
    ) -> int:
        """Estimate bridge fee by querying the Warp Route's quoteGasPayment.

        The Warp Route contract inherits quoteGasPayment(uint32) which returns
        the total fee including IGP + hooks. Falls back to a conservative default.
        """
        route = self._find_route(src_chain_id, dst_chain_id)
        if not route:
            return 500_000_000_000_000  # 0.0005 ETH default

        dst_domain = DOMAIN_IDS.get(dst_chain_id, dst_chain_id)

        try:
            from minotaur_subnet.blockchain.chains import get_web3
            w3 = get_web3(src_chain_id)

            call_data = QUOTE_GAS_PAYMENT_SELECTOR + abi_encode(
                ["uint32"], [dst_domain],
            )
            result = w3.eth.call({
                "to": w3.to_checksum_address(route.collateral_address),
                "data": "0x" + call_data.hex(),
            })
            fee = int.from_bytes(result, "big")
            # Add 20% buffer
            return int(fee * 1.2)

        except Exception as exc:
            logger.warning("Warp Route fee query failed: %s — using default", exc)
            return 500_000_000_000_000  # 0.0005 ETH fallback
