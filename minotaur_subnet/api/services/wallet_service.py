"""Wallet management service functions.

Functions: create_wallet, get_wallet, list_wallets, get_wallet_balances,
fund_wallet, get_wallet_manager.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict
from typing import Any

from minotaur_subnet.shared.types import WalletInfo
from minotaur_subnet.shared.interop_address import parse_address
from minotaur_subnet.store import AppIntentStore

from ._helpers import _generate_wallet_address
from ._state import get_wallet_manager  # noqa: F401 — re-exported

import logging

logger = logging.getLogger(__name__)


def create_wallet(
    store: AppIntentStore,
    chain_ids: list[int],
) -> dict[str, Any]:
    """Create a new managed wallet for the user.

    Uses LitMpcWallet when configured (production) or falls back to
    generating a local dev wallet (random address).

    Args:
        chain_ids: Chain IDs this wallet should support (e.g. [1, 8453]).

    Returns:
        WalletInfo as a dict with address, wallet_type, chain_ids, created_at.
    """
    from ._state import _wallet_manager

    if not chain_ids:
        return {"error": "chain_ids must be a non-empty list of chain IDs"}

    for cid in chain_ids:
        if not isinstance(cid, int) or cid <= 0:
            return {"error": f"Invalid chain_id: {cid}. Must be a positive integer."}

    # Use Lit MPC wallet if available
    if _wallet_manager is not None:
        import asyncio
        try:
            # asyncio.run() works from any thread (creates a new event loop)
            wallet_info = asyncio.run(_wallet_manager.create_wallet(chain_ids))
            store.save_wallet(wallet_info)
            return asdict(wallet_info)
        except Exception as exc:
            logger.warning("Lit wallet creation failed, using local fallback: %s", exc)

    # Local fallback
    address = _generate_wallet_address()
    wallet = WalletInfo(
        address=address,
        chain_ids=chain_ids,
        wallet_type="local",
        created_at=time.time(),
    )
    store.save_wallet(wallet)
    return asdict(wallet)


def get_wallet(
    store: AppIntentStore,
    address: str,
) -> dict[str, Any]:
    """Look up wallet information by address.

    Args:
        address: The 0x-prefixed wallet address.

    Returns:
        WalletInfo dict, or an error if not found.
    """
    if not address:
        return {"error": "address must be a 0x-prefixed hex string"}

    try:
        ia = parse_address(address)
    except ValueError as exc:
        return {"error": str(exc)}
    address = ia.address  # EIP-55 checksummed plain 0x

    wallet = store.get_wallet(address)
    if wallet is None:
        return {"error": f"Wallet not found: {address}"}
    return asdict(wallet)


def _get_bittensor_balances(ss58_address: str) -> dict[str, Any]:
    """Query Bittensor substrate for TAO balance + alpha stakes.

    Uses the local subtensor to fetch:
    - Free TAO balance
    - Alpha stake on known subnets (SN2, SN112)
    """
    try:
        import bittensor as bt

        subtensor_url = os.environ.get("SUBTENSOR_URL", "ws://subtensor:9944")
        sub = bt.Subtensor(network=subtensor_url)

        # TAO balance (free)
        balance = sub.get_balance(ss58_address)
        tao_rao = balance.rao if hasattr(balance, 'rao') else int(str(balance).replace(',', '').split('.')[0])
        tao_human = str(tao_rao / 1e9)

        # Alpha stakes on known subnets
        tokens: list[dict[str, Any]] = []
        tokens.append({
            "symbol": "TAO",
            "address": "native",
            "balance_raw": str(tao_rao),
            "balance": tao_human,
            "decimals": 9,
        })

        # Query alpha stakes per subnet
        try:
            stake_info = sub.get_stake_for_coldkey_and_hotkey(
                coldkey_ss58=ss58_address,
                hotkey_ss58=ss58_address,
            )
            if isinstance(stake_info, dict):
                for netuid, info in stake_info.items():
                    if netuid == 0:
                        continue  # Skip root network
                    stake = getattr(info, 'stake', None)
                    if stake is None:
                        continue
                    stake_rao = stake.rao if hasattr(stake, 'rao') else 0
                    if stake_rao <= 0:
                        continue
                    stake_human = str(stake_rao / 1e9)
                    tokens.append({
                        "symbol": f"Alpha (SN{netuid})",
                        "address": f"alpha:{netuid}",
                        "balance_raw": str(stake_rao),
                        "balance": stake_human,
                        "decimals": 9,
                        "netuid": int(netuid),
                    })
        except Exception as exc:
            logger.warning("Alpha stake query failed: %s", exc)

        return {
            "address": ss58_address,
            "chain_id": 0,
            "native": {"symbol": "TAO", "balance_wei": str(tao_rao), "balance": tao_human},
            "tokens": tokens,
        }
    except Exception as exc:
        logger.warning("Bittensor balance query failed: %s", exc)
        return {
            "address": ss58_address,
            "chain_id": 0,
            "native": {"symbol": "TAO", "balance_wei": "0", "balance": "0"},
            "tokens": [],
            "error": str(exc),
        }


def get_wallet_balances(address: str, chain_id: int) -> dict[str, Any]:
    """Query ETH + all known ERC-20 balances for an address on a chain.

    For chain_id=0 (Bittensor), queries the local subtensor for TAO balance
    and alpha stake on known subnets.
    """
    import asyncio

    # ── Bittensor substrate balances ─────────────────────────────────
    if chain_id == 0:
        return _get_bittensor_balances(address)

    # ── EVM balances ─────────────────────────────────────────────────

    from minotaur_subnet.blockchain.tokens import (
        TOKENS,
        get_erc20_balance,
        get_erc20_decimals,
        get_native_balance,
    )

    try:
        ia = parse_address(address)
    except ValueError as exc:
        return {"error": str(exc)}
    address = ia.address

    # Native ETH balance
    try:
        eth_wei = asyncio.run(get_native_balance(address, chain_id))
        eth_human = str(int(eth_wei) / 10**18) if eth_wei != "0" else "0"
    except Exception as exc:
        return {"error": f"RPC error querying chain {chain_id}: {exc}"}

    native = {"symbol": "ETH", "balance_wei": eth_wei, "balance": eth_human}

    # ERC-20 balances for all known tokens on this chain
    chain_tokens = TOKENS.get(chain_id, {})
    tokens: list[dict[str, Any]] = []
    for symbol, token_addr in chain_tokens.items():
        try:
            raw = asyncio.run(get_erc20_balance(token_addr, address, chain_id))
            if raw == "0":
                continue
            decimals = asyncio.run(get_erc20_decimals(token_addr, chain_id))
            human = str(int(raw) / 10**decimals)
            tokens.append({
                "symbol": symbol,
                "address": token_addr,
                "balance_raw": raw,
                "balance": human,
                "decimals": decimals,
            })
        except Exception:
            continue  # Skip tokens that fail (e.g. not deployed)

    return {
        "address": address,
        "chain_id": chain_id,
        "native": native,
        "tokens": tokens,
    }


def fund_wallet(
    store: AppIntentStore,
    app_id: str,
    token: str,
    amount: str,
    chain_id: int,
    depositor: str = "",
) -> dict[str, Any]:
    """Deposit tokens into an App Intent's contract.

    For the MVP this returns a stub transaction hash. In production the
    blockchain layer would construct and broadcast the real transaction.

    Args:
        app_id:   The app whose contract should receive funds.
        token:    Token contract address to deposit.
        amount:   Amount in wei (decimal string).
        chain_id: Target chain for the deposit.

    Returns:
        Dict with tx_hash and status.
    """
    if not app_id:
        return {"error": "app_id is required"}
    if not token:
        return {"error": "token must be a 0x-prefixed address"}
    try:
        ia = parse_address(token, default_chain_id=chain_id)
    except ValueError as exc:
        return {"error": str(exc)}
    if ia.chain_id is not None and ia.chain_id != chain_id:
        return {"error": f"Token address chain_id {ia.chain_id} != request chain_id {chain_id}"}
    token = ia.address  # EIP-55 checksummed plain 0x
    if not amount:
        return {"error": "amount is required"}
    try:
        int(amount)
    except ValueError:
        return {"error": "amount must be a decimal integer string (wei)"}
    if not isinstance(chain_id, int) or chain_id <= 0:
        return {"error": "chain_id must be a positive integer"}

    app = store.get_app(app_id)
    if app is None:
        return {"error": f"App not found: {app_id}"}

    deployment = store.get_deployment(app_id, chain_id=chain_id)
    if deployment is None or not deployment.status.is_operational():
        return {"error": f"App {app_id} is not deployed/active on chain {chain_id}"}

    contract_address = deployment.contract_address

    # Real deposit: relayer calls DCAApp.depositFor(user, token, amount)
    # The relayer approves the contract, then calls depositFor which transfers
    # tokens from the relayer to the contract's deposit balance for the user.
    try:
        from minotaur_subnet.blockchain.chains import get_web3
        from eth_abi import encode as abi_encode
        from web3 import Web3

        w3 = get_web3(chain_id)
        amount_int = int(amount)

        relayer_key = os.environ.get("RELAYER_PRIVATE_KEY", "")
        if not relayer_key:
            raise ValueError("No RELAYER_PRIVATE_KEY for deposit")

        relayer_acct = w3.eth.account.from_key(relayer_key)
        relayer_addr = relayer_acct.address
        token_cs = Web3.to_checksum_address(token)
        contract_cs = Web3.to_checksum_address(contract_address)

        # Depositor: the user whose deposit balance gets credited
        depositor_addr = depositor if depositor else relayer_addr

        # Step 1: Ensure relayer has enough tokens (faucet should have done this)
        # Step 2: Approve contract to pull from relayer
        approve_data = (
            "0x095ea7b3"
            + contract_cs.replace("0x", "").lower().zfill(64)
            + hex(amount_int)[2:].zfill(64)
        )
        nonce = w3.eth.get_transaction_count(relayer_addr)
        approve_tx = {
            "from": relayer_addr,
            "to": token_cs,
            "data": approve_data,
            "gas": 100_000,
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
            "chainId": chain_id,
        }
        signed_approve = w3.eth.account.sign_transaction(approve_tx, relayer_key)
        w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        w3.eth.wait_for_transaction_receipt(signed_approve.hash, timeout=30)

        # Step 3: Call depositFor(user, token, amount) from relayer
        # depositFor(address,address,uint256) selector
        from eth_hash.auto import keccak as _keccak
        deposit_sel = _keccak(b"depositFor(address,address,uint256)")[:4].hex()
        deposit_data = (
            "0x" + deposit_sel
            + depositor_addr.replace("0x", "").lower().zfill(64)
            + token_cs.replace("0x", "").lower().zfill(64)
            + hex(amount_int)[2:].zfill(64)
        )
        nonce2 = w3.eth.get_transaction_count(relayer_addr)
        deposit_tx = {
            "from": relayer_addr,
            "to": contract_cs,
            "data": deposit_data,
            "gas": 200_000,
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce2,
            "chainId": chain_id,
        }
        signed_deposit = w3.eth.account.sign_transaction(deposit_tx, relayer_key)
        tx_hash_raw = w3.eth.send_raw_transaction(signed_deposit.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_raw, timeout=30)

        return {
            "tx_hash": receipt.transactionHash.hex(),
            "status": "deposited",
            "app_id": app_id,
            "token": token,
            "amount": amount,
            "depositor": depositor_addr,
            "contract_address": contract_address,
            "chain_id": chain_id,
        }
    except Exception as exc:
        logger.warning("Real deposit failed, falling back to stub: %s", exc)

    # Fallback: stub transaction
    tx_hash = "0x" + hashlib.sha256(
        f"{app_id}:{token}:{amount}:{chain_id}:{time.time()}".encode()
    ).hexdigest()

    return {
        "tx_hash": tx_hash,
        "status": "pending",
        "app_id": app_id,
        "token": token,
        "amount": amount,
        "chain_id": chain_id,
        "note": "MVP stub -- no on-chain transaction was sent",
    }


def list_wallets(
    store: AppIntentStore,
) -> dict[str, Any]:
    """List all managed wallets.

    Returns:
        Dict with "wallets" key containing a list of WalletInfo dicts.
    """
    wallets = store.list_wallets()
    return {
        "wallets": [asdict(w) for w in wallets],
        "total": len(wallets),
    }
