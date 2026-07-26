"""Circle CCTP v2 bridge adapter — USDC Base ↔ Ethereum (Fast Transfer).

CCTP is burn-and-mint: USDC is burned on the source chain, Circle's
attestation service (Iris) signs the burn message, and canonical USDC is
minted on the destination. The ``mintRecipient`` is fixed inside the burn
message — nobody (solver, relayer, or Circle's transport) can redirect the
mint. The only added trust is Circle itself, already implied by holding
USDC. See docs/architecture/cross-chain-intents.md §4.

Fast Transfer (v2): attestation at soft finality — sub-30s both directions,
fee 1–1.3 bps (live via the Iris fees API). If ``maxFee`` is below the
current fast fee, CCTP silently downgrades to a Standard transfer
(~13–19 min hard finality) — the quote fetches the live fee and adds
headroom so a downgrade only happens if fees move sharply after quoting.

SELF-RELAY: unlike Across/Hyperlane there is no permissionless relayer
network delivering the mint — the platform submits
``receiveMessage(message, attestation)`` on the destination chain once
Iris attests. ``check_status`` surfaces the attested message in
``BridgeStatus.metadata`` (``ready_to_mint``); ``BridgeTracker`` polls for
it and self-relays the mint via ``_submit_cctp_mint`` (relayer wallet →
destination MessageTransmitterV2), then runs the destination leg. Still
gated behind ``CCTP_ENABLED`` (default OFF) as a kill switch.

Contract addresses are the CCTP v2 deployments (same address across EVM
chains), env-overridable — verify against developers.circle.com before
first mainnet use.
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

# ── CCTP v2 deployments (env-overridable) ────────────────────────────────────

TOKEN_MESSENGER_V2 = os.environ.get(
    "CCTP_TOKEN_MESSENGER_V2", "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d",
)
MESSAGE_TRANSMITTER_V2 = os.environ.get(
    "CCTP_MESSAGE_TRANSMITTER_V2", "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64",
)

# CCTP domain IDs (NOT chain ids)
CCTP_DOMAINS: dict[int, int] = {
    1: 0,      # Ethereum
    8453: 6,   # Base
}

USDC: dict[int, str] = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

# ── ABI fragments ────────────────────────────────────────────────────────────

# v2: depositForBurn(uint256 amount, uint32 destinationDomain,
#     bytes32 mintRecipient, address burnToken, bytes32 destinationCaller,
#     uint256 maxFee, uint32 minFinalityThreshold)
DEPOSIT_FOR_BURN_SELECTOR = keccak(
    b"depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
)[:4]

# v2: receiveMessage(bytes message, bytes attestation) — destination mint
RECEIVE_MESSAGE_SELECTOR = keccak(b"receiveMessage(bytes,bytes)")[:4]

APPROVE_SELECTOR = bytes.fromhex("095ea7b3")

# Finality thresholds: 1000 = confirmed (Fast Transfer), 2000 = finalized
FAST_FINALITY_THRESHOLD = 1000

ZERO_BYTES32 = b"\x00" * 32

IRIS_API = os.environ.get("CCTP_IRIS_API", "https://iris-api.circle.com")

# Headroom multiplier on the live fast fee so a fee tick between quote and
# execution doesn't silently downgrade the transfer to Standard finality.
# PERCENT, not bps: 150 means maxFee = 1.5x the quoted fast fee. (The old
# CCTP_MAX_FEE_HEADROOM_BPS name is still read for compatibility — it always
# carried a percent, the name was simply wrong.)
MAX_FEE_HEADROOM_PCT = int(
    os.environ.get("CCTP_MAX_FEE_HEADROOM_PCT")
    or os.environ.get("CCTP_MAX_FEE_HEADROOM_BPS")
    or "150"
)

ESTIMATED_DURATION_S = 30  # Fast Transfer target


def cctp_enabled() -> bool:
    """CCTP registers only when explicitly enabled.

    The destination mint is self-relayed by BridgeTracker (see the module
    docstring), so this is no longer a "not implemented" gate — it is the
    kill switch for a rail whose delivery depends on OUR relayer rather than
    a permissionless network. Note that ``BridgeRegistry.best_quote`` ranks
    purely on output, and CCTP's ~1.0 bps beats Across on every USDC route,
    so enabling this makes CCTP the default USDC rail.
    """
    return os.environ.get("CCTP_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class CCTPAdapter(BridgeAdapter):
    """Bridge adapter for Circle CCTP v2 Fast Transfer (USDC only)."""

    PROTOCOL = "cctp"

    def supported_routes(self) -> list[tuple[int, int]]:
        return [(1, 8453), (8453, 1)]

    def mock_config(self, quote: BridgeQuote) -> dict[str, Any]:
        """Replace depositForBurn with an ERC-20 transfer in simulation."""
        return {
            "selectors": [DEPOSIT_FOR_BURN_SELECTOR.hex()],
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
        """Quote a Fast Transfer using the live Iris fee schedule.

        Fails loud on any API problem (registry treats it as no-quote and
        the compiler fails closed) — a fabricated fee risks a silent
        downgrade to Standard finality latency the user wasn't quoted.
        """
        src_domain = CCTP_DOMAINS.get(src_chain_id)
        dst_domain = CCTP_DOMAINS.get(dst_chain_id)
        src_usdc = USDC.get(src_chain_id)
        dst_usdc = USDC.get(dst_chain_id)
        if src_domain is None or dst_domain is None or not src_usdc or not dst_usdc:
            raise ValueError(
                f"No CCTP route from chain {src_chain_id} to {dst_chain_id}"
            )
        if token_in.lower() not in (src_usdc.lower(), "usdc"):
            raise ValueError(f"CCTP bridges USDC only, got {token_in}")

        fast_fee_bps = await self._fetch_fast_fee_bps(src_domain, dst_domain)
        fee = amount * fast_fee_bps // 10_000
        # maxFee with headroom — burned amount minus actual fee mints; the
        # headroom only bounds the worst case, it isn't always charged.
        max_fee = amount * fast_fee_bps * MAX_FEE_HEADROOM_PCT // (10_000 * 100)
        if max_fee >= amount:
            raise ValueError(f"CCTP: fee bound {max_fee} consumes amount {amount}")

        return BridgeQuote(
            protocol=self.PROTOCOL,
            src_chain_id=src_chain_id,
            dst_chain_id=dst_chain_id,
            token_in=src_usdc,
            token_out=dst_usdc,
            amount_in=amount,
            estimated_output=amount - fee,
            fee=fee,
            estimated_duration_s=ESTIMATED_DURATION_S,
            metadata={
                "src_domain": src_domain,
                "dst_domain": dst_domain,
                "max_fee": max_fee,
                "fast_fee_bps": fast_fee_bps,
                "min_finality_threshold": FAST_FINALITY_THRESHOLD,
            },
        )

    async def _fetch_fast_fee_bps(self, src_domain: int, dst_domain: int) -> int:
        """GET the live Fast Transfer fee (bps) from the Iris fees API."""
        url = f"{IRIS_API}/v2/burn/USDC/fees/{src_domain}/{dst_domain}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise ValueError(f"Iris fees HTTP {resp.status}: {body}")
                data = await resp.json()

        # Response: list of {finalityThreshold, minimumFee(bps)} tiers.
        for tier in data if isinstance(data, list) else []:
            if int(tier.get("finalityThreshold", 0)) == FAST_FINALITY_THRESHOLD:
                return int(tier.get("minimumFee", 0))
        raise ValueError(
            f"Iris fees: no Fast Transfer tier "
            f"(threshold {FAST_FINALITY_THRESHOLD}) in {data!r:.200}"
        )

    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        recipient: str,
        refund_recipient: str | None = None,
    ) -> list[Interaction]:
        """Build approve + depositForBurn for the TokenMessenger.

        ``recipient`` (from the compiler, never solver-controlled) becomes
        the bytes32 ``mintRecipient`` baked into the burn message: the mint
        cannot be redirected by anyone, including the party that self-relays
        it. ``destinationCaller`` stays open (zero) so any party can submit
        the mint if the platform relayer stalls.

        ``refund_recipient`` is accepted for interface symmetry and unused:
        CCTP has no origin-refund path — a burn always mints eventually, so
        there is nothing to refund on the source chain.
        """
        mint_recipient = b"\x00" * 12 + bytes.fromhex(recipient.replace("0x", ""))

        approve_data = APPROVE_SELECTOR + abi_encode(
            ["address", "uint256"],
            [TOKEN_MESSENGER_V2, quote.amount_in],
        )
        burn_data = DEPOSIT_FOR_BURN_SELECTOR + abi_encode(
            ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
            [
                quote.amount_in,
                quote.metadata["dst_domain"],
                mint_recipient,
                quote.token_in,
                ZERO_BYTES32,                       # destinationCaller: open
                quote.metadata["max_fee"],
                quote.metadata["min_finality_threshold"],
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
                target=TOKEN_MESSENGER_V2,
                value="0",
                call_data="0x" + burn_data.hex(),
                chain_id=quote.src_chain_id,
            ),
        ]

    def build_mint_interactions(
        self, message_hex: str, attestation_hex: str, dst_chain_id: int,
    ) -> list[Interaction]:
        """Build the destination receiveMessage(message, attestation) call.

        The platform relayer submits this once check_status surfaces the
        attested message (BridgeStatus.metadata) — CCTP has no permissionless
        relayer network to do it for us.
        """
        def _bytes(h: str) -> bytes:
            return bytes.fromhex(h[2:] if h.startswith("0x") else h)

        data = RECEIVE_MESSAGE_SELECTOR + abi_encode(
            ["bytes", "bytes"],
            [_bytes(message_hex), _bytes(attestation_hex)],
        )
        return [
            Interaction(
                target=MESSAGE_TRANSMITTER_V2,
                value="0",
                call_data="0x" + data.hex(),
                chain_id=dst_chain_id,
            ),
        ]

    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        """Poll Iris for the burn message's attestation status.

        CCTP has no permissionless relayer: Iris only produces the
        attestation; the PLATFORM must submit ``receiveMessage`` on the
        destination to actually mint (BridgeTracker._submit_cctp_mint). So
        this NEVER returns COMPLETED — Iris ``complete`` means the
        attestation is ready, not that the mint happened. The tracker marks
        completion after its mint tx confirms.

        Verdicts:
          attestation ready (Iris ``complete`` / attestation present)
                             → IN_TRANSIT + metadata {ready_to_mint: True,
                               message, attestation} — tracker mints
          pending            → IN_TRANSIT / PENDING
        API trouble degrades to PENDING (keep polling). A burn cannot be
        stranded: the attestation eventually arrives and anyone can mint.
        """
        if not src_tx_hash:
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING, src_tx_hash=src_tx_hash,
            )
        tx_hash = src_tx_hash if src_tx_hash.startswith("0x") else f"0x{src_tx_hash}"
        src_domain = CCTP_DOMAINS.get(src_chain_id)
        if src_domain is None:
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=tx_hash,
                error=f"No CCTP domain for chain {src_chain_id}",
            )

        try:
            data = await self._fetch_messages(src_domain, tx_hash)
        except Exception as exc:
            logger.warning("Iris message lookup failed: %s", exc)
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING,
                src_tx_hash=tx_hash,
                error=str(exc),
            )

        messages = data.get("messages") or []
        if not messages:
            return BridgeStatus(
                status=BridgeStatusEnum.PENDING, src_tx_hash=tx_hash,
            )
        msg = messages[0]
        status = str(msg.get("status", "")).lower()
        attestation = msg.get("attestation") or ""
        attestation_ready = (
            status == "complete"
            or (bool(attestation) and attestation.lower() not in ("", "pending"))
        )

        if attestation_ready and attestation and attestation.lower() != "pending":
            # Attestation available → the platform can now mint. NOT COMPLETED:
            # the mint only happens once BridgeTracker submits receiveMessage.
            return BridgeStatus(
                status=BridgeStatusEnum.IN_TRANSIT,
                src_tx_hash=tx_hash,
                metadata={
                    "ready_to_mint": True,
                    "message": msg.get("message", ""),
                    "attestation": attestation,
                },
            )
        return BridgeStatus(
            status=BridgeStatusEnum.IN_TRANSIT, src_tx_hash=tx_hash,
        )

    async def _fetch_messages(self, src_domain: int, tx_hash: str) -> dict[str, Any]:
        url = f"{IRIS_API}/v2/messages/{src_domain}"
        params = {"transactionHash": tx_hash}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 404:
                    return {"messages": []}
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise ValueError(f"Iris messages HTTP {resp.status}: {body}")
                return await resp.json()
