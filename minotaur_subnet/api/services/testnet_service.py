"""Testnet faucet service functions."""

from __future__ import annotations

import os
from typing import Any

from minotaur_subnet.shared.interop_address import parse_address

from . import _state as _state_mod

import logging

logger = logging.getLogger(__name__)


def faucet_eth(
    address: str,
    amount_eth: float = 10.0,
    chain_id: int = 0,
) -> dict[str, Any]:
    """Fund an address with ETH on a local Anvil testnet fork.

    Uses the ``anvil_setBalance`` cheat code to instantly set the balance.
    Supports multiple Anvil forks (ETH mainnet, Base, etc.).

    Args:
        address:    The 0x-prefixed Ethereum address to fund.
        amount_eth: Amount of ETH to set (default 10.0).
        chain_id:   Target chain (0 = first available, 31337/1 = ETH fork, 8453 = Base fork).

    Returns:
        Dict with address, amount, balance_wei, chain_id, and status.
    """
    # Resolve RPC URL for the requested chain
    anvil_url: str | None = None
    resolved_chain_id = chain_id

    if chain_id and _state_mod._faucet_rpc_urls:
        anvil_url = _state_mod._faucet_rpc_urls.get(chain_id)
    elif _state_mod._faucet_rpc_urls:
        # chain_id=0: use first available
        resolved_chain_id, anvil_url = next(iter(_state_mod._faucet_rpc_urls.items()))
    else:
        # Fallback to env var (backward compat)
        anvil_url = os.environ.get("ANVIL_RPC_URL")
        if anvil_url:
            resolved_chain_id = chain_id or 31337

    if not anvil_url:
        available = list(_state_mod._faucet_rpc_urls.keys()) if _state_mod._faucet_rpc_urls else []
        return {
            "error": (
                f"No Anvil RPC URL for chain_id={chain_id}. "
                f"Available chains: {available}. "
                "Start the testnet with: make testnet-up"
            ),
        }

    if not address:
        return {"error": "address must be a 0x-prefixed hex string"}
    try:
        ia = parse_address(address)
    except ValueError as exc:
        return {"error": str(exc)}
    address = ia.address  # EIP-55 checksummed plain 0x

    if amount_eth <= 0:
        return {"error": "amount_eth must be positive"}

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(anvil_url))

        if not w3.is_connected():
            return {"error": f"Cannot connect to Anvil at {anvil_url}"}

        amount_wei = int(amount_eth * 1e18)
        w3.provider.make_request(
            "anvil_setBalance",
            [address, hex(amount_wei)],
        )

        # Read back to confirm
        balance = w3.eth.get_balance(address)

        return {
            "address": address,
            "amount_eth": amount_eth,
            "balance_wei": str(balance),
            "balance_eth": str(w3.from_wei(balance, "ether")),
            "chain_id": resolved_chain_id,
            "status": "funded",
            "rpc_url": anvil_url,
        }
    except Exception as exc:
        return {"error": f"Faucet failed: {exc}"}


def faucet_erc20(
    token: str,
    address: str,
    amount: str,
    chain_id: int = 0,
) -> dict[str, Any]:
    """Fund an address with ERC-20 tokens on a local Anvil testnet fork.

    Uses ``anvil_setStorageAt`` to directly write the token balance into the
    contract's storage.  Works for any standard ERC-20 (mapping-based
    ``balanceOf``).

    Args:
        token:    Token identifier -- 0x address, symbol ("USDC"), or
                  chain-qualified symbol ("USDC@8453").
        address:  The 0x-prefixed recipient address.
        amount:   Amount in the token's smallest unit (e.g. "10000000000"
                  for 10 000 USDC with 6 decimals).
        chain_id: Target chain (0 = first available).

    Returns:
        Dict with token, symbol, decimals, address, amount, balance,
        chain_id, and status.
    """
    from eth_hash.auto import keccak
    from web3 import Web3

    from minotaur_subnet.blockchain.tokens import (
        get_token_symbol,
        resolve_token,
    )

    # ── resolve chain + RPC ─────────────────────────────────────────────
    anvil_url: str | None = None
    resolved_chain_id = chain_id

    if chain_id and _state_mod._faucet_rpc_urls:
        anvil_url = _state_mod._faucet_rpc_urls.get(chain_id)
    elif _state_mod._faucet_rpc_urls:
        resolved_chain_id, anvil_url = next(iter(_state_mod._faucet_rpc_urls.items()))
    else:
        anvil_url = os.environ.get("ANVIL_RPC_URL")
        if anvil_url:
            resolved_chain_id = chain_id or 31337

    if not anvil_url:
        available = list(_state_mod._faucet_rpc_urls.keys()) if _state_mod._faucet_rpc_urls else []
        return {
            "error": (
                f"No Anvil RPC URL for chain_id={chain_id}. "
                f"Available chains: {available}. "
                "Start the testnet with: make testnet-up"
            ),
        }

    # ── resolve token address ───────────────────────────────────────────
    try:
        token_address, token_chain = resolve_token(token, fallback_chain_id=resolved_chain_id)
    except ValueError as exc:
        return {"error": str(exc)}

    # ── validate recipient ──────────────────────────────────────────────
    if not address:
        return {"error": "address must be a 0x-prefixed hex string"}
    try:
        ia = parse_address(address)
    except ValueError as exc:
        return {"error": str(exc)}
    address = ia.address

    # ── validate amount ─────────────────────────────────────────────────
    try:
        amount_int = int(amount)
    except (ValueError, TypeError):
        return {"error": f"amount must be a decimal integer string, got {amount!r}"}
    if amount_int <= 0:
        return {"error": "amount must be positive"}

    # ── connect + deal ──────────────────────────────────────────────────
    try:
        w3 = Web3(Web3.HTTPProvider(anvil_url))
        if not w3.is_connected():
            return {"error": f"Cannot connect to Anvil at {anvil_url}"}

        token_cs = Web3.to_checksum_address(token_address)
        addr_cs = Web3.to_checksum_address(address)

        # Read current balance via balanceOf(address)
        balance_of_sig = "0x70a08231" + addr_cs[2:].lower().zfill(64)
        current_raw = w3.eth.call({"to": token_cs, "data": balance_of_sig})
        current_balance = int.from_bytes(current_raw, "big")

        # Faucet is additive: new balance = current + requested amount
        target_balance = current_balance + amount_int
        target_hex = hex(target_balance)[2:].zfill(64)
        addr_padded = addr_cs[2:].lower().zfill(64)

        # Standard ERC-20 balances use mapping(address => uint256).  The
        # storage key is keccak256(abi.encodePacked(address, slot)) where
        # slot is the mapping's position in storage layout.  We probe
        # slots 0-10 (covers virtually all standard ERC-20s), verify via
        # balanceOf(), and revert failed writes.
        # See also: AnvilSimulator._deal_erc20() for the same technique.
        dealt = False
        for slot in range(11):
            slot_hex = hex(slot)[2:].zfill(64)
            key_input = bytes.fromhex(addr_padded + slot_hex)
            storage_key = "0x" + keccak(key_input).hex()

            w3.provider.make_request(
                "anvil_setStorageAt",
                [token_cs, storage_key, "0x" + target_hex],
            )

            # Verify
            result = w3.eth.call({"to": token_cs, "data": balance_of_sig})
            new_balance = int.from_bytes(result, "big")
            if new_balance == target_balance:
                dealt = True
                break

            # Revert failed slot write
            w3.provider.make_request(
                "anvil_setStorageAt",
                [token_cs, storage_key, "0x" + hex(current_balance)[2:].zfill(64)],
            )

        if not dealt:
            return {"error": f"Could not find balanceOf storage slot for {token_cs}"}

        # Read back decimals + symbol for the response
        decimals = 18
        try:
            dec_sig = "0x313ce567"  # decimals()
            dec_raw = w3.eth.call({"to": token_cs, "data": dec_sig})
            decimals = int.from_bytes(dec_raw, "big")
        except Exception:
            pass

        symbol = get_token_symbol(token_address, resolved_chain_id) or token
        human_amount = amount_int / (10 ** decimals)

        return {
            "token": token_cs,
            "symbol": symbol,
            "decimals": decimals,
            "address": addr_cs,
            "amount": amount,
            "amount_human": f"{human_amount:,.{min(decimals, 6)}f}",
            "balance": str(new_balance),
            "chain_id": resolved_chain_id,
            "status": "funded",
        }
    except Exception as exc:
        return {"error": f"ERC-20 faucet failed: {exc}"}
