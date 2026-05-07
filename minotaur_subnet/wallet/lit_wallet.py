"""Lit Protocol MPC wallet — production wallet for App Intents.

Communicates with a Lit Protocol Node.js bridge service via HTTP.
The bridge runs Lit SDK calls (PKP minting, distributed ECDSA signing)
and exposes them as a simple REST API.

Architecture:
    Python (this module) ── HTTP ──→ Node.js bridge ── Lit SDK ──→ Lit Network

In development mode (no bridge running), falls back to LocalWalletManager
from minotaur_subnet.blockchain.wallet.

Usage:
    wallet = LitMpcWallet(bridge_url="http://localhost:3100")
    info = await wallet.create_wallet(chain_ids=[1, 8453])
    signed = await wallet.sign_transaction(info.address, tx_data)
    sig = await wallet.sign_message(info.address, message)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.shared.types import WalletInfo

logger = logging.getLogger(__name__)

# Default bridge URL (the Node.js sidecar that wraps the Lit SDK)
DEFAULT_BRIDGE_URL = os.environ.get("LIT_BRIDGE_URL", "http://localhost:3100")

# Default Lit network
DEFAULT_LIT_NETWORK = os.environ.get("LIT_NETWORK", "datil-dev")


@dataclass
class SignResult:
    """Result of a signing operation."""

    signature: str  # hex-encoded signature
    recovery_id: int = 0
    public_key: str = ""  # signer's public key


class LitMpcWallet:
    """High-level Lit Protocol MPC wallet interface.

    Wraps the Lit SDK bridge (Node.js sidecar) for PKP creation and
    distributed ECDSA signing. Falls back to local dev wallet if bridge
    is unavailable and ``allow_fallback=True``.
    """

    def __init__(
        self,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        lit_network: str = DEFAULT_LIT_NETWORK,
        auth_sig: dict[str, Any] | None = None,
        allow_fallback: bool = True,
        session: Any = None,  # aiohttp.ClientSession
    ) -> None:
        self._bridge_url = bridge_url.rstrip("/")
        self._lit_network = lit_network
        self._auth_sig = auth_sig
        self._allow_fallback = allow_fallback
        self._session = session
        self._local_fallback: Any = None  # lazy-initialized LocalWalletManager
        self._pkp_cache: dict[str, dict[str, Any]] = {}  # address → pkp metadata

    # ── Public API ────────────────────────────────────────────────────────

    async def create_wallet(self, chain_ids: list[int] | None = None) -> WalletInfo:
        """Create a new PKP wallet via Lit Protocol.

        Mints a new PKP NFT, generating a distributed key pair across
        the Lit network. No single party holds the complete private key.

        Args:
            chain_ids: EVM chains this wallet should support.

        Returns:
            WalletInfo with the PKP's derived Ethereum address.
        """
        chains = chain_ids or [1]

        try:
            data = await self._bridge_request("POST", "/wallets", {
                "chain_ids": chains,
                "lit_network": self._lit_network,
                "auth_sig": self._auth_sig,
            })

            address = data["address"]
            self._pkp_cache[address.lower()] = {
                "pkp_token_id": data.get("pkp_token_id"),
                "public_key": data.get("public_key"),
                "chain_ids": chains,
                "created_at": time.time(),
            }

            logger.info("Created Lit MPC wallet %s on chains %s", address, chains)
            return WalletInfo(
                address=address,
                chain_ids=chains,
                wallet_type="lit_mpc",
                created_at=time.time(),
            )

        except BridgeUnavailableError:
            return await self._fallback_create_wallet(chains)

    async def sign_transaction(
        self,
        address: str,
        tx: dict[str, Any],
        chain_id: int = 1,
    ) -> str:
        """Sign a transaction using the PKP's distributed key.

        The signing happens across the Lit network — no single node
        holds the full private key. The bridge orchestrates the
        distributed ECDSA signing protocol.

        Args:
            address: The PKP wallet address.
            tx: Transaction dict (to, value, data, nonce, gas, etc.).
            chain_id: Target chain ID.

        Returns:
            Hex-encoded signed transaction (ready for broadcast).
        """
        try:
            data = await self._bridge_request("POST", "/sign/transaction", {
                "address": address,
                "transaction": tx,
                "chain_id": chain_id,
                "lit_network": self._lit_network,
                "auth_sig": self._auth_sig,
            })
            return data["signed_tx"]

        except BridgeUnavailableError:
            return await self._fallback_sign_transaction(address, tx)

    async def sign_message(
        self,
        address: str,
        message: str | bytes,
    ) -> str:
        """Sign an arbitrary message using the PKP's distributed key.

        Useful for EIP-712 typed data, personal_sign, and other
        message-signing use cases.

        Args:
            address: The PKP wallet address.
            message: The message to sign (string or bytes).

        Returns:
            Hex-encoded signature (65 bytes: r + s + v).
        """
        msg_hex = (
            message.hex() if isinstance(message, bytes)
            else message.encode().hex()
        )

        try:
            data = await self._bridge_request("POST", "/sign/message", {
                "address": address,
                "message_hex": msg_hex,
                "lit_network": self._lit_network,
                "auth_sig": self._auth_sig,
            })
            return data["signature"]

        except BridgeUnavailableError:
            return await self._fallback_sign_message(address, message)

    async def sign_eip712_order(
        self,
        address: str,
        order_id: bytes,
        app: str,
        intent_selector: bytes,
        intent_params: bytes,
        submitted_by: str,
        chain_id: int,
        deadline: int,
        nonce: int,
        perpetual: bool,
        max_executions: int,
        cooldown: int,
        contract_address: str = "0x" + "00" * 20,
    ) -> str:
        """Sign an EIP-712 IntentOrder using the PKP's distributed key (WAL-6).

        Computes the EIP-712 digest from the order fields and signs it via
        the Lit bridge (distributed ECDSA). Falls back to local signing if
        the bridge is unavailable.

        The resulting signature can be submitted as ``user_signature`` when
        creating an order via POST /v1/apps/{app_id}/orders.

        Returns:
            Hex-encoded 65-byte ECDSA signature (r + s + v).
        """
        from minotaur_subnet.consensus.eip712 import (
            hash_order_struct,
            build_domain_separator,
            _to_typed_data_hash,
        )

        struct_hash = hash_order_struct(
            order_id=order_id,
            app=app,
            intent_selector=intent_selector,
            intent_params=intent_params,
            submitted_by=submitted_by,
            chain_id=chain_id,
            deadline=deadline,
            nonce=nonce,
            perpetual=perpetual,
            max_executions=max_executions,
            cooldown=cooldown,
        )
        domain_sep = build_domain_separator(
            chain_id=chain_id,
            contract_address=contract_address,
        )
        digest = _to_typed_data_hash(domain_sep, struct_hash)

        # Sign the raw 32-byte EIP-712 digest (no personal_sign prefix)
        try:
            data = await self._bridge_request("POST", "/sign/hash", {
                "address": address,
                "hash_hex": digest.hex(),
                "lit_network": self._lit_network,
                "auth_sig": self._auth_sig,
            })
            return data["signature"]

        except BridgeUnavailableError:
            return await self._fallback_sign_hash(address, digest)

    async def get_wallet(self, address: str) -> WalletInfo | None:
        """Look up a PKP wallet by address."""
        cached = self._pkp_cache.get(address.lower())
        if cached:
            return WalletInfo(
                address=address,
                chain_ids=cached["chain_ids"],
                wallet_type="lit_mpc",
                created_at=cached.get("created_at", 0.0),
            )

        try:
            data = await self._bridge_request("GET", f"/wallets/{address}")
            return WalletInfo(
                address=data["address"],
                chain_ids=data.get("chain_ids", [1]),
                wallet_type="lit_mpc",
                created_at=data.get("created_at", 0.0),
            )
        except (BridgeUnavailableError, BridgeError):
            return None

    async def list_wallets(self) -> list[WalletInfo]:
        """List all PKP wallets managed by this instance."""
        try:
            data = await self._bridge_request("GET", "/wallets")
            return [
                WalletInfo(
                    address=w["address"],
                    chain_ids=w.get("chain_ids", [1]),
                    wallet_type="lit_mpc",
                    created_at=w.get("created_at", 0.0),
                )
                for w in data.get("wallets", [])
            ]
        except BridgeUnavailableError:
            if self._local_fallback:
                return await self._local_fallback.list_wallets()
            return []

    async def health(self) -> dict[str, Any]:
        """Check the bridge service health."""
        try:
            data = await self._bridge_request("GET", "/health")
            return {"status": "ok", "bridge": "connected", **data}
        except BridgeUnavailableError:
            return {"status": "degraded", "bridge": "unavailable", "fallback": self._allow_fallback}

    # ── Bridge Communication ──────────────────────────────────────────────

    async def _bridge_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a request to the Lit bridge service."""
        url = f"{self._bridge_url}{path}"

        try:
            import aiohttp

            session = self._session
            close_after = False
            if session is None:
                session = aiohttp.ClientSession()
                close_after = True

            try:
                kwargs: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=30)}
                if body and method != "GET":
                    kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise BridgeError(f"Bridge error {resp.status}: {text}")
                    return await resp.json()
            finally:
                if close_after:
                    await session.close()

        except ImportError:
            raise BridgeUnavailableError("aiohttp not installed")
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise BridgeUnavailableError(f"Bridge not reachable at {url}: {exc}")
        except Exception as exc:
            # Catch aiohttp-specific errors (InvalidUrlClientError, etc.)
            exc_type = type(exc).__name__
            if "ClientError" in exc_type or "aiohttp" in type(exc).__module__:
                raise BridgeUnavailableError(f"Bridge not reachable at {url}: {exc}")
            raise

    # ── Fallback to Local Dev Wallet ──────────────────────────────────────

    def _get_fallback(self):
        """Lazy-initialize the local fallback wallet manager."""
        if not self._allow_fallback:
            raise BridgeUnavailableError(
                "Lit bridge unavailable and fallback disabled"
            )

        if self._local_fallback is None:
            logger.warning(
                "Lit bridge unavailable — falling back to LocalWalletManager. "
                "Do NOT use in production."
            )
            from minotaur_subnet.blockchain.wallet import LocalWalletManager
            self._local_fallback = LocalWalletManager()

        return self._local_fallback

    async def _fallback_create_wallet(self, chain_ids: list[int]) -> WalletInfo:
        mgr = self._get_fallback()
        info = await mgr.create_wallet(chain_ids)
        info.wallet_type = "local"
        return info

    async def _fallback_sign_transaction(self, address: str, tx: dict) -> str:
        mgr = self._get_fallback()
        return await mgr.sign_transaction(address, tx)

    async def _fallback_sign_hash(self, address: str, digest: bytes) -> str:
        """Sign a raw 32-byte hash using the local fallback wallet.

        Uses ``unsafe_sign_hash`` (no prefix) — appropriate for EIP-712 digests
        which already include the \\x19\\x01 prefix.
        """
        mgr = self._get_fallback()
        acct = mgr._get_account(address)
        from eth_account import Account
        signed = Account.unsafe_sign_hash(digest, private_key=acct.key)
        return signed.signature.hex()

    async def _fallback_sign_message(self, address: str, message: str | bytes) -> str:
        mgr = self._get_fallback()
        acct = mgr._get_account(address)
        if isinstance(message, bytes):
            from eth_account.messages import encode_defunct
            msg = encode_defunct(primitive=message)
        else:
            from eth_account.messages import encode_defunct
            msg = encode_defunct(text=message)
        signed = acct.sign_message(msg)
        return signed.signature.hex()


class BridgeUnavailableError(Exception):
    """The Lit bridge service is not reachable."""


class BridgeError(Exception):
    """The Lit bridge returned an error."""
