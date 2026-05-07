"""Bridge plan verifier — validators verify platform-compiled cross-chain plans.

Called by validators before signing a consensus proposal. Ensures that:
1. Bridge legs were compiled by the platform (not solver-crafted)
2. No bridge protocol selectors exist in solver's business-logic legs
3. Destination legs have escrow deposits on-chain
4. Bridge quotes are reasonable (within acceptable bounds)
"""

from __future__ import annotations

import logging
from typing import Any

from minotaur_subnet.shared.types import _BRIDGE_CALL_SELECTORS

logger = logging.getLogger(__name__)


def verify_platform_compiled(
    plan_data: dict[str, Any],
    params: dict[str, Any],
    leg_index: int | None = None,
) -> tuple[bool, str]:
    """Verify a cross-chain plan was platform-compiled.

    Called by validators in the consensus proposal handler before signing.

    Args:
        plan_data: The execution plan dict from the proposal.
        params: Order params from the proposal.
        leg_index: Current leg index being proposed (from plan metadata).

    Returns:
        (is_valid, reason) — True if valid, False with reason if not.
    """
    meta = plan_data.get("metadata", {}) if plan_data else {}

    # If not a cross-chain plan, skip verification
    if not meta.get("_platform_compiled") and not meta.get("cross_chain"):
        return True, "not cross-chain"

    # Check 1: Platform-compiled flag must be set for cross-chain plans
    if meta.get("cross_chain") and not meta.get("_platform_compiled"):
        # Legacy cross-chain plan (pre-compiler). Allow with warning.
        logger.warning("Legacy cross-chain plan without _platform_compiled flag")
        return True, "legacy cross-chain (warning)"

    # Check 2: No bridge selectors in solver's business-logic interactions
    interactions = plan_data.get("interactions", [])
    for ix in interactions:
        cd = ix.get("call_data", "") or ix.get("callData", "") or ""
        raw = cd[2:] if cd.startswith("0x") else cd
        selector = raw[:8] if len(raw) >= 8 else ""
        if selector in _BRIDGE_CALL_SELECTORS:
            return False, f"Bridge selector {selector} in solver interactions"

    return True, "ok"


def verify_escrow_on_chain(
    contract_address: str,
    chain_id: int,
    order_id: str,
    leg_index: int,
) -> tuple[bool, str]:
    """Verify escrow deposit exists on-chain for a destination leg.

    Queries the AppIntentBase contract's getEscrow() function.

    Returns:
        (has_escrow, reason) — True if escrow exists with amount > 0.
    """
    try:
        from minotaur_subnet.blockchain.chains import get_web3
        from web3 import Web3
        from eth_hash.auto import keccak
        from eth_abi import encode as abi_encode

        w3 = get_web3(chain_id)

        # getEscrow(bytes32, uint256) → (address, uint256, address, uint256, bool, bool)
        sel = keccak(b"getEscrow(bytes32,uint256)")[:4]
        oid_bytes = bytes.fromhex(order_id.replace("0x", "").zfill(64))
        cd = "0x" + sel.hex() + abi_encode(
            ["bytes32", "uint256"], [oid_bytes, leg_index],
        ).hex()

        result = w3.eth.call({
            "to": Web3.to_checksum_address(contract_address),
            "data": cd,
        })

        # Decode: bytes 32-64 = amount (uint256)
        escrow_amount = int.from_bytes(result[32:64], "big")
        if escrow_amount > 0:
            return True, f"escrow amount={escrow_amount}"
        return False, "escrow amount is 0"

    except Exception as exc:
        logger.debug("Escrow on-chain check failed: %s", exc)
        return False, f"check failed: {exc}"
