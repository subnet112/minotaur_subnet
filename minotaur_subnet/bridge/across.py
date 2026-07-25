"""Across Protocol bridge adapter — Base ↔ Ethereum value rail.

Across is an intent-based bridge: the user deposits into the origin-chain
SpokePool escrow, permissionless relayers front destination liquidity from
their own capital (repaid later via UMA optimistic settlement), and fills
must match the deposit params exactly — a relayer cannot alter recipient or
amount. If no relayer fills by ``fillDeadline``, the depositor is refunded
on the origin chain. See docs/architecture/cross-chain-intents.md §4 for
why this is the primary Base↔Ethereum rail.

Deposit flow (V3):
  1. Approve input token to the origin SpokePool
  2. Call depositV3(depositor, recipient, inputToken, outputToken,
     inputAmount, outputAmount, destinationChainId, exclusiveRelayer,
     quoteTimestamp, fillDeadline, exclusivityDeadline, message)
  3. Relayer fills on destination (seconds–minutes) or the deposit
     expires and is refunded on the origin chain (hours)

Quoting uses the Across API (``suggested-fees``); the fee and the
``quoteTimestamp`` MUST come from the API — the SpokePool rejects
deposits whose fee/timestamp don't match a recent quote.

SpokePool addresses default to the canonical mainnet deployments but are
env-overridable (ACROSS_SPOKE_POOL_<chain_id>) — verify against
https://docs.across.to before first mainnet use, and note the API's
``spokePoolAddress`` response field is preferred over the static map.
"""

from __future__ import annotations

import logging
import os
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

# ── SpokePool deployments (env-overridable) ──────────────────────────────────

SPOKE_POOLS: dict[int, str] = {
    1: os.environ.get(
        "ACROSS_SPOKE_POOL_1",
        "0x5c7BCd6E7De5423a257D81B442095A1a6ced35C5",
    ),
    8453: os.environ.get(
        "ACROSS_SPOKE_POOL_8453",
        "0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64",
    ),
}

# ── Canonical token pairs Base ↔ Ethereum ────────────────────────────────────
# token address on each chain, keyed by symbol. Across fills deliver the
# canonical asset on the destination (no synthetic wrappers).

TOKEN_MAP: dict[str, dict[int, str]] = {
    "WETH": {
        1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        8453: "0x4200000000000000000000000000000000000006",
    },
    "USDC": {
        1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
}

# ── ABI fragments ────────────────────────────────────────────────────────────

# depositV3(address,address,address,address,uint256,uint256,uint256,address,
#           uint32,uint32,uint32,bytes)
DEPOSIT_V3_SIGNATURE = (
    "depositV3(address,address,address,address,uint256,uint256,uint256,"
    "address,uint32,uint32,uint32,bytes)"
)
DEPOSIT_V3_SELECTOR = keccak(DEPOSIT_V3_SIGNATURE.encode())[:4]

# ERC-20 approve
APPROVE_SELECTOR = bytes.fromhex("095ea7b3")

# V3FundsDeposited event — depositId is the second indexed param (topics[2])
V3_FUNDS_DEPOSITED_TOPIC = keccak(
    b"V3FundsDeposited(address,address,uint256,uint256,uint256,uint32,"
    b"uint32,uint32,uint32,address,address,address,bytes)"
).hex()

ZERO_ADDRESS = "0x" + "00" * 20

# Across REST API (quotes + deposit status)
ACROSS_API = os.environ.get("ACROSS_API", "https://app.across.to/api")

# Fill deadline: how long relayers have before the deposit expires and is
# refunded on the origin chain. Bounded on-chain by the SpokePool's
# fillDeadlineBuffer; 1h leaves ample fill headroom (fills take seconds)
# while capping the user's worst-case wait for the expiry→refund path.
FILL_DEADLINE_S = int(os.environ.get("ACROSS_FILL_DEADLINE_S", "3600"))

# Typical relayer fill time — used for quote ETA display only.
ESTIMATED_DURATION_S = 60


class AcrossAdapter(BridgeAdapter):
    """Bridge adapter for Across V3 — WETH/USDC between Base and Ethereum."""

    PROTOCOL = "across"

    def _resolve_pair(
        self, token_in: str, src_chain_id: int, dst_chain_id: int,
    ) -> tuple[str, str, str] | None:
        """Map an input token to (symbol, canonical_in, canonical_out)."""
        for symbol, by_chain in TOKEN_MAP.items():
            src_addr = by_chain.get(src_chain_id)
            dst_addr = by_chain.get(dst_chain_id)
            if not src_addr or not dst_addr:
                continue
            if token_in.lower() in (src_addr.lower(), symbol.lower()):
                return symbol, src_addr, dst_addr
        return None

    def supported_routes(self) -> list[tuple[int, int]]:
        return [(1, 8453), (8453, 1)]

    def mock_config(self, quote: BridgeQuote) -> dict[str, Any]:
        """Across simulation mock: replace depositV3 with an ERC-20 transfer.

        The approve interaction is left intact (plain ERC-20 call, simulable);
        only the SpokePool deposit — which needs live relayer infrastructure —
        is mocked, matching the contract's tokens-left-the-proxy invariant.
        """
        return {
            "selectors": [DEPOSIT_V3_SELECTOR.hex()],
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
        """Quote via the Across ``suggested-fees`` API.

        Fails loud (raises) on any API problem — the BridgeRegistry treats a
        raising adapter as "no quote" and the compiler fails closed. Never
        fabricate a fee: the SpokePool rejects deposits whose relay fee and
        quoteTimestamp don't match a recent API quote, so a made-up fallback
        would produce an unfillable (expiry→refund) deposit at best.
        """
        pair = self._resolve_pair(token_in, src_chain_id, dst_chain_id)
        if pair is None:
            raise ValueError(
                f"No Across route for {token_in} "
                f"from chain {src_chain_id} to {dst_chain_id}"
            )
        symbol, input_token, output_token = pair

        data = await self._fetch_suggested_fees(
            input_token, output_token, src_chain_id, dst_chain_id, amount,
        )

        if data.get("isAmountTooLow"):
            raise ValueError(
                f"Across: amount {amount} below route minimum for {symbol}"
            )

        total_fee = int(data["totalRelayFee"]["total"])
        quote_timestamp = int(data["timestamp"])
        estimated_output = amount - total_fee
        if estimated_output <= 0:
            raise ValueError(
                f"Across: fee {total_fee} consumes the whole amount {amount}"
            )

        spoke_pool = data.get("spokePoolAddress") or SPOKE_POOLS.get(src_chain_id)
        if not spoke_pool:
            raise ValueError(f"No Across SpokePool for chain {src_chain_id}")

        return BridgeQuote(
            protocol=self.PROTOCOL,
            src_chain_id=src_chain_id,
            dst_chain_id=dst_chain_id,
            token_in=input_token,
            token_out=output_token,
            amount_in=amount,
            estimated_output=estimated_output,
            fee=total_fee,
            estimated_duration_s=ESTIMATED_DURATION_S,
            metadata={
                "spoke_pool": spoke_pool,
                "quote_timestamp": quote_timestamp,
                "fill_deadline": quote_timestamp + FILL_DEADLINE_S,
                "token_symbol": symbol,
            },
        )

    async def _fetch_suggested_fees(
        self,
        input_token: str,
        output_token: str,
        src_chain_id: int,
        dst_chain_id: int,
        amount: int,
    ) -> dict[str, Any]:
        """GET suggested-fees from the Across API. Raises on non-200."""
        url = f"{ACROSS_API}/suggested-fees"
        params = {
            "inputToken": input_token,
            "outputToken": output_token,
            "originChainId": str(src_chain_id),
            "destinationChainId": str(dst_chain_id),
            "amount": str(amount),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise ValueError(
                        f"Across suggested-fees HTTP {resp.status}: {body}"
                    )
                return await resp.json()

    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        sender: str,
    ) -> list[Interaction]:
        """Build approve + depositV3 interactions for the origin SpokePool.

        ``sender`` (the compiler's safe_recipient — always the user/escrow,
        never solver-controlled) is pinned as BOTH depositor and recipient:
        the depositor receives the origin-chain refund if the deposit
        expires, and the recipient receives the destination fill. Fills must
        match these params exactly, so nobody downstream can redirect them.
        """
        spoke_pool = quote.metadata["spoke_pool"]
        quote_timestamp = quote.metadata["quote_timestamp"]
        fill_deadline = quote.metadata["fill_deadline"]

        approve_data = APPROVE_SELECTOR + abi_encode(
            ["address", "uint256"],
            [spoke_pool, quote.amount_in],
        )

        deposit_data = DEPOSIT_V3_SELECTOR + abi_encode(
            [
                "address", "address", "address", "address",
                "uint256", "uint256", "uint256", "address",
                "uint32", "uint32", "uint32", "bytes",
            ],
            [
                sender,                    # depositor (refund target on expiry)
                sender,                    # recipient (destination fill target)
                quote.token_in,
                quote.token_out,
                quote.amount_in,
                quote.estimated_output,    # outputAmount = amount minus relay fee
                quote.dst_chain_id,
                ZERO_ADDRESS,              # exclusiveRelayer: open to all relayers
                quote_timestamp,
                fill_deadline,
                0,                         # exclusivityDeadline
                b"",                       # message: plain transfer (no handler yet)
            ],
        )

        return [
            Interaction(
                target=quote.token_in,
                value="0",
                call_data="0x" + approve_data.hex(),
                chain_id=quote.src_chain_id,
            ),
            Interaction(
                target=spoke_pool,
                value="0",
                call_data="0x" + deposit_data.hex(),
                chain_id=quote.src_chain_id,
            ),
        ]

    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        """Check fill status via depositId → Across deposit/status API.

        The depositId is extracted from the V3FundsDeposited event in the
        source receipt. Any failure to extract or query resolves to PENDING
        (the BridgeTracker keeps polling): safety never depends on this —
        an unfilled deposit expires and refunds on the origin chain.
        """
        if not src_tx_hash:
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING, src_tx_hash=src_tx_hash,
            )
        tx_hash = src_tx_hash if src_tx_hash.startswith("0x") else f"0x{src_tx_hash}"

        try:
            deposit_id = self._extract_deposit_id(tx_hash, src_chain_id)
        except Exception as exc:
            logger.debug("Across depositId extraction failed: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=tx_hash,
                error=str(exc),
            )
        if deposit_id is None:
            return BridgeStatus(
                status=BridgeStatusEnum.FAILED,
                src_tx_hash=tx_hash,
                error="Source TX failed or no V3FundsDeposited event",
            )

        try:
            return await self._check_deposit_status(
                tx_hash, src_chain_id, deposit_id,
            )
        except Exception as exc:
            logger.warning("Across status check failed: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=tx_hash,
                error=str(exc),
            )

    def _extract_deposit_id(
        self, tx_hash: str, src_chain_id: int,
    ) -> int | None:
        """Extract the depositId from the source receipt's deposit event.

        Returns None when the TX failed or carries no deposit event (a
        terminal state, unlike transient RPC errors which raise).
        """
        from minotaur_subnet.blockchain.chains import get_web3

        w3 = get_web3(src_chain_id)
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if not receipt or receipt.get("status") != 1:
            return None

        for log_entry in receipt.get("logs", []):
            topics = log_entry.get("topics", [])
            if len(topics) < 3:
                continue
            raw_t0 = topics[0]
            t0_hex = (
                raw_t0.hex() if isinstance(raw_t0, bytes)
                else str(raw_t0).replace("0x", "")
            )
            if t0_hex != V3_FUNDS_DEPOSITED_TOPIC:
                continue
            # topics: [0]=event, [1]=destinationChainId, [2]=depositId
            raw_id = topics[2]
            id_hex = (
                raw_id.hex() if isinstance(raw_id, bytes)
                else str(raw_id).replace("0x", "")
            )
            return int(id_hex, 16)
        return None

    async def _check_deposit_status(
        self, tx_hash: str, src_chain_id: int, deposit_id: int,
    ) -> BridgeStatus:
        """Query the Across deposit/status endpoint for a fill verdict."""
        url = f"{ACROSS_API}/deposit/status"
        params = {
            "originChainId": str(src_chain_id),
            "depositId": str(deposit_id),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return BridgeStatus(
                        status=BridgeStatusEnum.PENDING, src_tx_hash=tx_hash,
                    )
                data = await resp.json()

        status = str(data.get("status", "")).lower()
        fill_tx = data.get("fillTx") or data.get("fillTxHash")

        if status == "filled":
            return BridgeStatus(
                status=BridgeStatusEnum.COMPLETED,
                src_tx_hash=tx_hash,
                dst_tx_hash=fill_tx,
            )
        if status == "expired":
            # Deposit expired unfilled — funds refund to the depositor on
            # the origin chain. FAILED here means "this bridge attempt is
            # over", which routes the order into the recovery path.
            return BridgeStatus(
                status=BridgeStatusEnum.FAILED,
                src_tx_hash=tx_hash,
                error="Across deposit expired unfilled (origin-chain refund)",
            )
        return BridgeStatus(
            status=BridgeStatusEnum.IN_TRANSIT, src_tx_hash=tx_hash,
        )
