"""Token approval utilities for managed wallets.

Provides ERC-2612 permit signing and ERC-20 approve fallback so that
managed wallets can auto-approve token spending when submitting orders.

Functions:
    check_allowance     — Read current ERC-20 allowance via Web3.
    try_erc2612_permit  — Sign a gasless permit via the lit-bridge.
    send_approve_tx     — Send an on-chain approve() via the lit-bridge.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_hash.auto import keccak
from web3 import Web3

logger = logging.getLogger(__name__)

# ERC-20 function selectors
_ALLOWANCE_SELECTOR = keccak(b"allowance(address,address)")[:4]
_APPROVE_SELECTOR = keccak(b"approve(address,uint256)")[:4]
_BALANCEOF_SELECTOR = keccak(b"balanceOf(address)")[:4]

# ERC-2612 selectors
_DOMAIN_SEPARATOR_SELECTOR = keccak(b"DOMAIN_SEPARATOR()")[:4]
_NONCES_SELECTOR = keccak(b"nonces(address)")[:4]

# EIP-2612 Permit typehash
_PERMIT_TYPEHASH = keccak(
    b"Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
)

# Default permit deadline: 30 minutes from now
_PERMIT_VALIDITY_SECONDS = 30 * 60


def check_allowance(w3: Web3, token: str, owner: str, spender: str) -> int:
    """Read the current ERC-20 allowance of *owner* for *spender*.

    Returns 0 on any failure (contract reverts, network errors, etc.).
    """
    try:
        calldata = (
            _ALLOWANCE_SELECTOR
            + abi_encode(
                ["address", "address"],
                [Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)],
            )
        )
        result = w3.eth.call({
            "to": Web3.to_checksum_address(token),
            "data": "0x" + calldata.hex(),
        })
        return int.from_bytes(result, "big")
    except Exception as exc:
        logger.debug("allowance() call failed for %s: %s", token, exc)
        return 0


def read_balance_and_allowance(
    w3: Web3, token: str, owner: str, spender: str,
) -> tuple[int, int] | None:
    """Read *owner*'s ERC-20 balance and their allowance to *spender*.

    Returns ``(balance, allowance)`` on success, or ``None`` if EITHER read
    fails. Unlike :func:`check_allowance` (which returns 0 on error), this must
    distinguish a genuine zero from an RPC failure: callers use it to *terminate*
    an order on a funding shortfall, and a transient read error must never be
    mistaken for "user is broke" — the caller fails OPEN on ``None``.
    """
    try:
        owner_cs = Web3.to_checksum_address(owner)
        token_cs = Web3.to_checksum_address(token)
        bal_raw = w3.eth.call({
            "to": token_cs,
            "data": "0x" + (_BALANCEOF_SELECTOR + abi_encode(["address"], [owner_cs])).hex(),
        })
        allow_raw = w3.eth.call({
            "to": token_cs,
            "data": "0x" + (
                _ALLOWANCE_SELECTOR
                + abi_encode(["address", "address"], [owner_cs, Web3.to_checksum_address(spender)])
            ).hex(),
        })
        return int.from_bytes(bytes(bal_raw), "big"), int.from_bytes(bytes(allow_raw), "big")
    except Exception as exc:
        logger.debug("balance/allowance read failed for %s: %s", token, exc)
        return None


def try_erc2612_permit(
    w3: Web3,
    bridge_url: str,
    token: str,
    owner: str,
    spender: str,
    value: int,
    chain_id: int,
) -> dict[str, Any] | None:
    """Try to sign an ERC-2612 permit for *owner* approving *spender*.

    Args:
        w3: Web3 instance connected to the chain.
        bridge_url: Lit-bridge HTTP URL (e.g. ``http://localhost:3100``).
        token: ERC-20 token address.
        owner: Wallet address (must be a managed wallet in the lit-bridge).
        spender: Contract address that will spend the tokens.
        value: Token amount to approve (raw units).
        chain_id: EVM chain ID.

    Returns:
        Dict with ``permit_deadline``, ``permit_v``, ``permit_r``, ``permit_s``
        on success, or ``None`` if the token doesn't support ERC-2612.
    """
    token_cs = Web3.to_checksum_address(token)
    owner_cs = Web3.to_checksum_address(owner)
    spender_cs = Web3.to_checksum_address(spender)

    # 1. Check if token supports ERC-2612 via DOMAIN_SEPARATOR()
    try:
        domain_sep_raw = w3.eth.call({
            "to": token_cs,
            "data": "0x" + _DOMAIN_SEPARATOR_SELECTOR.hex(),
        })
        domain_separator = bytes(domain_sep_raw)
        if len(domain_separator) != 32:
            logger.debug("DOMAIN_SEPARATOR() returned %d bytes, expected 32", len(domain_separator))
            return None
    except Exception as exc:
        logger.debug("Token %s does not support ERC-2612: %s", token, exc)
        return None

    # 2. Get current nonce for owner
    try:
        nonce_calldata = _NONCES_SELECTOR + abi_encode(["address"], [owner_cs])
        nonce_raw = w3.eth.call({
            "to": token_cs,
            "data": "0x" + nonce_calldata.hex(),
        })
        nonce = int.from_bytes(nonce_raw, "big")
    except Exception as exc:
        logger.debug("nonces() call failed for %s: %s", token, exc)
        return None

    # 3. Build EIP-2612 digest
    deadline = int(time.time()) + _PERMIT_VALIDITY_SECONDS

    struct_hash = keccak(
        abi_encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256"],
            [_PERMIT_TYPEHASH, owner_cs, spender_cs, value, nonce, deadline],
        )
    )

    digest = keccak(b"\x19\x01" + domain_separator + struct_hash)

    # 4. Sign via lit-bridge /sign/hash
    try:
        resp = httpx.post(
            f"{bridge_url}/sign/hash",
            json={"address": owner_cs, "hash_hex": digest.hex()},
            timeout=10.0,
        )
        resp.raise_for_status()
        sig_hex = resp.json()["signature"]
    except Exception as exc:
        logger.warning("Permit signing failed for %s: %s", owner, exc)
        return None

    # 5. Decode 65-byte signature → v, r, s
    sig_bytes = bytes.fromhex(sig_hex.replace("0x", ""))
    if len(sig_bytes) != 65:
        logger.warning("Unexpected signature length: %d", len(sig_bytes))
        return None

    r = sig_bytes[:32]
    s = sig_bytes[32:64]
    v = sig_bytes[64]

    # Normalize v (some signers return 0/1 instead of 27/28)
    if v < 27:
        v += 27

    logger.info(
        "ERC-2612 permit signed: token=%s owner=%s spender=%s value=%s",
        token[:10], owner[:10], spender[:10], value,
    )

    return {
        "permit_deadline": deadline,
        "permit_v": v,
        "permit_r": "0x" + r.hex(),
        "permit_s": "0x" + s.hex(),
    }


def send_approve_tx(
    w3: Web3,
    bridge_url: str,
    token: str,
    owner: str,
    spender: str,
    value: int,
    chain_id: int,
) -> str:
    """Send an ERC-20 ``approve(spender, value)`` transaction from *owner*.

    Signs via the lit-bridge, then broadcasts the raw transaction.

    Args:
        w3: Web3 instance connected to the chain.
        bridge_url: Lit-bridge HTTP URL.
        token: ERC-20 token address.
        owner: Wallet address (must be a managed wallet in the lit-bridge).
        spender: Contract address to approve.
        value: Token amount to approve (raw units).
        chain_id: EVM chain ID.

    Returns:
        Transaction hash (hex string).

    Raises:
        RuntimeError: If signing or broadcast fails.
    """
    token_cs = Web3.to_checksum_address(token)
    owner_cs = Web3.to_checksum_address(owner)
    spender_cs = Web3.to_checksum_address(spender)

    # Build approve calldata
    calldata = _APPROVE_SELECTOR + abi_encode(
        ["address", "uint256"],
        [spender_cs, value],
    )

    tx = {
        "from": owner_cs,
        "to": token_cs,
        "data": "0x" + calldata.hex(),
        "value": "0x0",
        "nonce": w3.eth.get_transaction_count(owner_cs),
        "gasPrice": w3.eth.gas_price,
        "chainId": chain_id,
    }
    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 100_000

    # Sign via lit-bridge
    try:
        resp = httpx.post(
            f"{bridge_url}/sign/transaction",
            json={"address": owner_cs, "transaction": tx, "chain_id": chain_id},
            timeout=10.0,
        )
        resp.raise_for_status()
        signed_tx = resp.json()["signed_tx"]
    except Exception as exc:
        raise RuntimeError(f"Failed to sign approve tx: {exc}") from exc

    # Broadcast
    try:
        tx_hash = w3.eth.send_raw_transaction(bytes.fromhex(signed_tx.replace("0x", "")))
        # Mine on Anvil (needed for 2s block time)
        try:
            w3.provider.make_request("evm_mine", [])
        except Exception:
            pass
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError(f"approve() tx reverted: {tx_hash.hex()}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to broadcast approve tx: {exc}") from exc

    tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
    logger.info(
        "ERC-20 approve tx sent: token=%s owner=%s spender=%s value=%s tx=%s",
        token[:10], owner[:10], spender[:10], value, tx_hash_hex[:16],
    )
    return tx_hash_hex
