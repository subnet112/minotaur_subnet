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

import hashlib
import json
import logging
import os
import urllib.request
from typing import Any

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak

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

# wTAO address on Ethereum (the bridged ERC-20).
#
# NINE decimals, not eighteen — verified on mainnet. It matches substrate rao
# 1:1, so amounts cross this bridge without conversion. Do not reach for the
# 1e18 assumption that every other ERC-20 on Ethereum invites.
_WTAO_ETH = "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44"
_WTAO_DECIMALS = 9

# Ethereum -> Bittensor is a BURN ON THE TOKEN ITSELF, not a deposit into a
# separate bridge contract: `bridgeBack(uint256 amount, string ss58Dest)` burns
# the caller's wTAO and Tensorplex's relayers credit `ss58Dest` on Bittensor.
#
# Read off mainnet rather than from documentation — decoded from a real burn
# (0xbee4795c…): selector 0x2a383090, args (2419041000, "5CZtoc8i…egkV3"), and
# the matching Transfer to the zero address. Two consequences the previous stub
# had backwards: there is NO separate bridge contract to deposit into, and NO
# approve is needed, because the token burns from msg.sender directly.
_BRIDGE_BACK_SIGNATURE = "bridgeBack(uint256,string)"
_BRIDGE_BACK_SELECTOR = keccak(_BRIDGE_BACK_SIGNATURE.encode())[:4]

# Bittensor uses the generic substrate SS58 prefix.
_BITTENSOR_SS58_FORMAT = 42

# What arrives on the Bittensor side is NATIVE TAO, not an ERC-20 — the bridge
# credits a substrate account. The repo's native sentinel (orders.py) names it
# so a destination leg reads "native", never a token address that exists on the
# wrong chain.
_NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

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

# Bittensor EVM
BITTENSOR_EVM_CHAIN_ID = 964


def evm_to_ss58(h160: str) -> str:
    """The substrate account an EVM address controls on Bittensor.

    Frontier's HashedAddressMapping: ``blake2_256("evm:" || address)``. This is
    what makes an Ethereum -> chain-964 bridge leg possible at all. Tensorplex
    releases to a SUBSTRATE account, not to an EVM address — but on Bittensor
    the EVM's view of an H160's balance IS the balance of its mapped account,
    so bridging to the mapped ss58 of an H160 credits native TAO to that
    address on 964, spendable as ``msg.value``.
    """
    addr = h160.lower().removeprefix("0x")
    if len(addr) != 40:
        raise ValueError(f"not an H160 address: {h160!r}")
    raw = hashlib.blake2b(b"evm:" + bytes.fromhex(addr), digest_size=32).digest()
    try:
        from scalecodec.utils.ss58 import ss58_encode
    except ImportError as exc:  # pragma: no cover - production always has it
        raise RuntimeError(
            "scalecodec is required to derive the substrate destination for a "
            "Bittensor bridge leg",
        ) from exc
    return ss58_encode(raw, _BITTENSOR_SS58_FORMAT)


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

        # Determine token_out based on direction.
        #
        # Bittensor-bound legs land NATIVE TAO. Echoing token_in — which for an
        # Ethereum origin is wTAO's ETHEREUM address — would name a contract
        # that does not exist on 964, and that address is what seeds the
        # destination fork, so the leg would have nothing to spend.
        if dst_chain_id == 1:  # Destination is Ethereum
            token_out = _WTAO_ETH
        else:
            token_out = _NATIVE_SENTINEL

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
        recipient: str,
        refund_recipient: str | None = None,
    ) -> list[Interaction]:
        """Build the Ethereum-side leg: burn wTAO, naming the substrate destination.

        ONE interaction, on the wTAO token itself. There is no approve and no
        separate bridge contract — ``bridgeBack`` burns from ``msg.sender``, so
        an allowance would be dead weight and a deposit target does not exist.

        ``recipient`` may be either an H160 or an ss58 string:

        * an H160 is mapped through :func:`evm_to_ss58`, which is what makes a
          chain-964 destination work — the bridge releases to a substrate
          account, and on Bittensor an H160's EVM balance IS its mapped
          account's balance. The bridged TAO therefore arrives as native value
          the destination leg can spend.
        * an ss58 is passed through, for a plain substrate destination.

        There is no refund path on this direction: a burn is irreversible and
        Tensorplex credits the named destination or nothing. ``refund_recipient``
        is accepted for interface compatibility and deliberately unused — a
        caller expecting origin-chain refund semantics like Across's should not
        get them silently.
        """
        src = int(quote.src_chain_id)
        if src != 1:
            raise ValueError(
                f"Tensorplex EVM bridging originates on Ethereum (chain 1), got {src}. "
                "The Bittensor -> Ethereum direction is a substrate extrinsic; use "
                "build_bridge_substrate_actions().",
            )
        if not recipient:
            raise ValueError("recipient is required: bridgeBack burns to a named destination")

        # An H160 is the common case for a 964 destination; anything else is
        # taken to be an ss58 already.
        dest = (
            evm_to_ss58(recipient)
            if recipient.startswith("0x") and len(recipient) == 42
            else recipient
        )

        call_data = _BRIDGE_BACK_SELECTOR + abi_encode(
            ["uint256", "string"],
            [int(quote.amount_in), dest],
        )
        return [
            Interaction(
                target=_WTAO_ETH,
                value="0",
                call_data="0x" + call_data.hex(),
                chain_id=src,
            ),
        ]

    def mock_config(self, quote: BridgeQuote) -> dict[str, Any]:
        """Simulation mock: bridgeBack has no on-fork counterpart.

        The burn is real ERC-20 machinery and would execute on a fork, but its
        EFFECT — TAO appearing on Bittensor — depends on relayers that no fork
        can run. Mocking it as the ERC-20 transfer it economically is keeps the
        "tokens left the proxy" invariant the destination measurement relies on.
        """
        return {
            "selectors": [_BRIDGE_BACK_SELECTOR.hex()],
            "mock_type": "erc20_transfer",
            "mock_token": _WTAO_ETH,
            "mock_amount": int(quote.amount_in),
        }

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
