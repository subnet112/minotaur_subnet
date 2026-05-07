"""Tensorplex Bridge adapter — TAO bridging between Bittensor and Ethereum.

Tensorplex is an audited (Zellic + Quantstamp) bridge for TAO tokens
with a multi-relayer architecture, 0.1% fee, and ~30 minute settlement.

Supports two bridge directions:
  - Bittensor substrate → Ethereum: substrate balance transfer to lock address
    → wTAO released on Ethereum. This is the path for Alpha → USDC intents.
  - Ethereum → Bittensor EVM: ERC-20 wTAO deposit on Ethereum contract
    → TAO released on Bittensor. (EVM-to-substrate direction.)

Bridge details:
- Fee: 0.1% + gas cost flat fee
- Settlement: ~30 minutes (may extend during congestion)
- Docs: https://docs.tensorplex.ai/tensorplex-docs/tensorplex-tao-bridge
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from minotaur_subnet.bridge.base import (
    BridgeAdapter,
    BridgeQuote,
    BridgeStatus,
    BridgeStatusEnum,
)
from minotaur_subnet.shared.types import Interaction, SubstrateAction

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# Tensorplex fee: 0.1% (10 basis points)
_FEE_BPS = 10

# Estimated bridge settlement time
_ESTIMATED_DURATION_S = 1800  # 30 minutes

# wTAO address on Ethereum (the bridged ERC-20)
_WTAO_ETH = "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44"

# Tensorplex bridge lock address on Bittensor substrate.
# TAO sent here is locked and wTAO is minted on Ethereum.
_TENSORPLEX_LOCK_SS58 = os.environ.get(
    "TENSORPLEX_LOCK_SS58",
    # Default: Tensorplex's known lock address (verify before mainnet)
    "5DfhGyQdFobKM8NsWvEeAKk5EhQhro2FLCwMi8jNYxSYqQWj",
)

# Tensorplex status API endpoint
_TENSORPLEX_API_URL = os.environ.get(
    "TENSORPLEX_API_URL",
    "https://bridge-api.tensorplex.dev/v1",
)

# Bittensor substrate chain ID (virtual, for routing)
BITTENSOR_SUBSTRATE_CHAIN_ID = 0  # Convention: 0 = Bittensor substrate


class TensorplexAdapter(BridgeAdapter):
    """Tensorplex bridge adapter.

    Handles both directions:
      - Substrate → Ethereum: via build_bridge_substrate_actions() (SubstrateAction)
      - Ethereum → Bittensor: via build_bridge_interactions() (EVM Interaction)
    """

    PROTOCOL = "tensorplex"

    async def quote(
        self,
        token_in: str,
        amount: int,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeQuote:
        """Get a quote for bridging TAO/wTAO.

        For substrate→Ethereum: token_in is "TAO" (native), token_out is wTAO ERC-20.
        For Ethereum→substrate: token_in is wTAO ERC-20, token_out is "TAO".
        """
        fee = amount * _FEE_BPS // 10_000

        # Determine token_out based on direction
        if dst_chain_id == 1:  # Destination is Ethereum
            token_out = _WTAO_ETH
        else:
            token_out = token_in  # TAO native on Bittensor

        return BridgeQuote(
            protocol=self.PROTOCOL,
            src_chain_id=src_chain_id,
            dst_chain_id=dst_chain_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount,
            estimated_output=amount - fee,
            fee=fee,
            estimated_duration_s=_ESTIMATED_DURATION_S,
            metadata={
                "fee_bps": _FEE_BPS,
                "bridge": "tensorplex",
                "lock_address": _TENSORPLEX_LOCK_SS58,
            },
        )

    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        sender: str,
    ) -> list[Interaction]:
        """Build EVM interactions for Ethereum → Bittensor bridge direction.

        Approves wTAO and deposits to the Tensorplex bridge contract on Ethereum.
        """
        # Reference implementation: EVM-to-Bittensor bridge contract calls.
        # For now, the primary use case is substrate→Ethereum (alpha→USDC)
        raise NotImplementedError(
            "Tensorplex EVM→Bittensor bridge not yet implemented. "
            "Use substrate→Ethereum direction via build_bridge_substrate_actions()."
        )

    def build_bridge_substrate_actions(
        self,
        quote: BridgeQuote,
        owner_ss58: str,
    ) -> list[SubstrateAction]:
        """Build substrate actions for Bittensor → Ethereum bridge.

        Transfers TAO to the Tensorplex lock address on Bittensor substrate.
        The bridge relayers detect the deposit and release wTAO on Ethereum.
        """
        return [
            SubstrateAction(
                action="bridge_deposit",
                owner_ss58=owner_ss58,
                amount_rao=quote.amount_in,
                dest_address=_TENSORPLEX_LOCK_SS58,
                metadata={
                    "bridge": "tensorplex",
                    "expected_output": quote.estimated_output,
                    "fee": quote.fee,
                    "token_out": quote.token_out,
                    "dst_chain_id": quote.dst_chain_id,
                },
            )
        ]

    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        """Poll Tensorplex API for bridge transfer status.

        The src_tx_hash is the substrate extrinsic hash from the bridge
        deposit transaction.
        """
        try:
            url = f"{_TENSORPLEX_API_URL}/transfers/{src_tx_hash}"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            api_status = data.get("status", "").lower()
            dst_tx_hash = data.get("destination_tx_hash")
            amount_received = data.get("amount_received")

            status_map = {
                "pending": BridgeStatusEnum.PENDING,
                "processing": BridgeStatusEnum.IN_TRANSIT,
                "confirming": BridgeStatusEnum.IN_TRANSIT,
                "completed": BridgeStatusEnum.COMPLETED,
                "success": BridgeStatusEnum.COMPLETED,
                "failed": BridgeStatusEnum.FAILED,
                "expired": BridgeStatusEnum.FAILED,
            }

            bridge_status = status_map.get(api_status, BridgeStatusEnum.PENDING)

            return BridgeStatus(
                status=bridge_status,
                src_tx_hash=src_tx_hash,
                dst_tx_hash=dst_tx_hash,
                amount_received=int(amount_received) if amount_received else None,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Transfer not yet indexed — treat as pending
                return BridgeStatus(
                    status=BridgeStatusEnum.PENDING,
                    src_tx_hash=src_tx_hash,
                )
            logger.warning("Tensorplex API error: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=src_tx_hash,
                error=f"API error: {exc.code}",
            )
        except Exception as exc:
            logger.warning("Tensorplex status check failed: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=src_tx_hash,
                error=str(exc),
            )

    def supported_routes(self) -> list[tuple[int, int]]:
        """Supported routes: Bittensor ↔ Ethereum."""
        return [
            (BITTENSOR_SUBSTRATE_CHAIN_ID, 1),  # Bittensor substrate → Ethereum
            (1, BITTENSOR_SUBSTRATE_CHAIN_ID),  # Ethereum → Bittensor substrate
            (1, 964),                            # Ethereum → Bittensor EVM
            (964, 1),                            # Bittensor EVM → Ethereum
        ]
