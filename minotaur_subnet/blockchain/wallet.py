"""
Wallet management for the App Intents system.

Provides an abstract ``WalletManager`` interface and two implementations:

* **LocalWalletManager** -- generates and stores keys locally.  Suitable for
  dev / test environments.  Keys are stored AES-encrypted (Fernet) on disk;
  the encryption key is derived from a passphrase (defaults to a well-known
  dev-mode value with a loud warning).

* **LitProtocolWalletManager** -- production-grade MPC wallet using
  Lit Protocol's 2-of-2 scheme.  Currently a stub that documents the
  SDK integration points documented inline as ``Reference implementation`` blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

from minotaur_subnet.blockchain.chains import get_web3
from minotaur_subnet.blockchain.tokens import get_erc20_balance, get_native_balance
from minotaur_subnet.shared.types import WalletInfo

logger = logging.getLogger(__name__)

# Enable mnemonic / HD-wallet features in eth_account
Account.enable_unaudited_hdwallet_features()


# ═══════════════════════════════════════════════════════════════════════════════
#                         ABSTRACT INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════


class WalletManager(ABC):
    """
    Abstract wallet management interface.

    All methods are async to support both local (thread-dispatched) and
    remote (HTTP-based, e.g., Lit Protocol) implementations.
    """

    @abstractmethod
    async def create_wallet(self, chain_ids: list[int] | None = None) -> WalletInfo:
        """
        Create a new wallet and return its metadata.

        Parameters
        ----------
        chain_ids:
            Chain IDs the wallet should be usable on.  The same address
            works on all EVM chains, but this lets the manager know which
            chains to track.
        """
        ...

    @abstractmethod
    async def get_wallet(self, address: str) -> WalletInfo:
        """
        Retrieve metadata for an existing wallet.

        Raises ``KeyError`` if the wallet is unknown to this manager.
        """
        ...

    @abstractmethod
    async def sign_transaction(self, address: str, tx: dict[str, Any]) -> str:
        """
        Sign *tx* with the private key for *address*.

        Returns the raw signed transaction as a ``0x``-prefixed hex string.
        """
        ...

    @abstractmethod
    async def get_balance(
        self,
        address: str,
        token: str,
        chain_id: int,
    ) -> str:
        """
        Return the balance of *address* for *token* on *chain_id*.

        *token* can be:
        - ``"ETH"`` or ``"native"`` for the native gas token
        - A token symbol known to the token registry (e.g., ``"USDC"``)
        - A checksummed contract address (``0x...``)

        Returns the balance as a decimal string in the smallest unit (wei).
        """
        ...

    @abstractmethod
    async def list_wallets(self) -> list[WalletInfo]:
        """Return all wallets managed by this instance."""
        ...


# ═══════════════════════════════════════════════════════════════════════════════
#                         LOCAL (DEV) IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════════════


# Default storage directory (relative to this file).
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


class LocalWalletManager(WalletManager):
    """
    Development / test wallet manager that keeps private keys on disk.

    Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256).  The
    encryption key is derived from a passphrase via PBKDF2.

    **WARNING**: This implementation is intended for local development and
    automated tests *only*.  Do NOT use it in production -- use
    ``LitProtocolWalletManager`` or a proper HSM/KMS solution instead.

    Storage layout (``data_dir``):
        wallets.json     -- {address: {encrypted_key, chain_ids, created_at}}
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        passphrase: str | None = None,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._passphrase = (
            passphrase
            or os.environ.get("APP_INTENTS_WALLET_PASSPHRASE")
        )
        if not self._passphrase:
            raise RuntimeError(
                "LocalWalletManager requires an explicit passphrase. "
                "Set the APP_INTENTS_WALLET_PASSPHRASE environment variable "
                "or pass `passphrase=` to the constructor. There is no default "
                "fallback — a hardcoded passphrase would let anyone with "
                "filesystem access decrypt every stored key."
            )

        self._fernet = self._build_fernet(self._passphrase)
        self._store_path = self._data_dir / "wallets.json"
        self._wallets: dict[str, dict[str, Any]] = self._load_store()

    # -----------------------------------------------------------------------
    # Encryption helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_fernet(passphrase: str):
        """Derive a Fernet key from *passphrase* using PBKDF2."""
        import base64
        import hashlib

        # Use a fixed salt derived from the project name.  This is fine for
        # dev-mode; production should use a random, per-wallet salt.
        salt = b"minotaur-app-intents-local-wallet"
        key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 480_000)
        fernet_key = base64.urlsafe_b64encode(key[:32])

        from cryptography.fernet import Fernet

        return Fernet(fernet_key)

    def _encrypt_key(self, private_key: str) -> str:
        """Encrypt a hex private key and return a base64 token."""
        return self._fernet.encrypt(private_key.encode()).decode()

    def _decrypt_key(self, token: str) -> str:
        """Decrypt a previously encrypted private key."""
        return self._fernet.decrypt(token.encode()).decode()

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _load_store(self) -> dict[str, dict[str, Any]]:
        """Load the wallet store from disk, or return empty dict."""
        if not self._store_path.exists():
            return {}
        try:
            raw = self._store_path.read_text()
            return json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load wallet store: %s", exc)
            return {}

    def _save_store(self) -> None:
        """Persist the wallet store to disk."""
        self._store_path.write_text(json.dumps(self._wallets, indent=2))

    # -----------------------------------------------------------------------
    # Internal account access
    # -----------------------------------------------------------------------

    def _get_account(self, address: str) -> LocalAccount:
        """Return the ``LocalAccount`` for *address*."""
        addr_lower = address.lower()
        for stored_addr, entry in self._wallets.items():
            if stored_addr.lower() == addr_lower:
                pk = self._decrypt_key(entry["encrypted_key"])
                return Account.from_key(pk)
        raise KeyError(f"Wallet not found: {address}")

    # -----------------------------------------------------------------------
    # WalletManager interface
    # -----------------------------------------------------------------------

    async def create_wallet(self, chain_ids: list[int] | None = None) -> WalletInfo:
        acct: LocalAccount = Account.create()
        address = acct.address
        now = time.time()
        chains = chain_ids or [1]

        self._wallets[address] = {
            "encrypted_key": self._encrypt_key(acct.key.hex()),
            "chain_ids": chains,
            "created_at": now,
        }
        self._save_store()

        logger.info("Created local wallet %s for chains %s", address, chains)
        return WalletInfo(
            address=address,
            chain_ids=chains,
            wallet_type="local",
            created_at=now,
        )

    async def get_wallet(self, address: str) -> WalletInfo:
        addr_lower = address.lower()
        for stored_addr, entry in self._wallets.items():
            if stored_addr.lower() == addr_lower:
                return WalletInfo(
                    address=stored_addr,
                    chain_ids=entry["chain_ids"],
                    wallet_type="local",
                    created_at=entry.get("created_at", 0.0),
                )
        raise KeyError(f"Wallet not found: {address}")

    async def sign_transaction(self, address: str, tx: dict[str, Any]) -> str:
        acct = self._get_account(address)
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction.hex()

    async def get_balance(
        self,
        address: str,
        token: str,
        chain_id: int,
    ) -> str:
        token_upper = token.upper()
        if token_upper in ("ETH", "NATIVE"):
            return await get_native_balance(address, chain_id)

        # If token looks like an address, use it directly
        if token.startswith("0x") and len(token) == 42:
            return await get_erc20_balance(token, address, chain_id)

        # Otherwise resolve the symbol via the token registry
        from minotaur_subnet.blockchain.tokens import get_token_address

        token_address = get_token_address(token, chain_id)
        return await get_erc20_balance(token_address, address, chain_id)

    async def list_wallets(self) -> list[WalletInfo]:
        return [
            WalletInfo(
                address=addr,
                chain_ids=entry["chain_ids"],
                wallet_type="local",
                created_at=entry.get("created_at", 0.0),
            )
            for addr, entry in self._wallets.items()
        ]


# ═══════════════════════════════════════════════════════════════════════════════
#                     LIT PROTOCOL (PRODUCTION) STUB
# ═══════════════════════════════════════════════════════════════════════════════


class LitProtocolWalletManager(WalletManager):
    """
    Production wallet manager using Lit Protocol's MPC infrastructure.

    **Status: STUB** -- the interface is defined with detailed documentation
    of what Lit Protocol SDK calls would be used.  Implementation requires
    the ``@lit-protocol/lit-node-client`` JS SDK (called via a thin HTTP
    bridge or a Node subprocess).

    Lit Protocol MPC overview
    -------------------------
    Lit uses a *distributed key generation* (DKG) scheme where the private
    key is split into shares across a decentralised node network.  No single
    party (including Minotaur) ever holds the complete key.

    For App Intents, the intended scheme is **2-of-2 MPC**:
      - **Share 1**: held by the app/intent owner (the developer)
      - **Share 2**: held by Minotaur's validator network

    This means neither party can sign alone, providing strong security for
    user funds managed by App Intent contracts.

    Key SDK methods (JS, via ``@lit-protocol/lit-node-client``):
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    1. ``litNodeClient.connect()`` -- connect to the Lit network
    2. ``LitActions.signEcdsa({
           toSign,           // message hash bytes
           publicKey,         // the PKP public key
           sigName,           // signature name
       })`` -- distributed signing
    3. ``litNodeClient.executeJs({
           code,              // Lit Action JS code
           authSig,           // authentication signature
           jsParams,          // parameters passed to the Lit Action
       })`` -- execute a Lit Action (for custom logic)

    PKP (Programmable Key Pair) lifecycle:
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    - ``LitContracts.pkpNftContractUtils.write.mint()``
        Mint a new PKP NFT, generating a new distributed key pair.
    - ``LitContracts.pkpPermissionsContractUtils.write.addPermittedAction()``
        Authorize a Lit Action to use the PKP for signing.

    References:
    -----------
    - Lit Protocol docs: https://developer.litprotocol.com/
    - PKP overview: https://developer.litprotocol.com/v3/sdk/wallets/intro
    - Lit Actions: https://developer.litprotocol.com/v3/sdk/serverless-signing/overview
    """

    def __init__(
        self,
        lit_network: str = "datil-dev",
        auth_sig: dict[str, Any] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        lit_network:
            Which Lit network to connect to.  Options:
            ``"datil-dev"`` (testnet), ``"datil-test"`` (staging),
            ``"datil"`` (mainnet).
        auth_sig:
            An Ethereum auth signature proving identity to the Lit network.
            See https://developer.litprotocol.com/v3/sdk/authentication/auth-sig
        """
        self._lit_network = lit_network
        self._auth_sig = auth_sig
        # Reference implementation:
        # Initialize Lit Node Client connection.
        #
        # In JS this would be:
        #   const client = new LitNodeClient({ litNetwork: "datil-dev" });
        #   await client.connect();
        #
        # In Python, we'd call this via:
        #   - A sidecar Node.js process that exposes an HTTP API, OR
        #   - The Lit Protocol REST API (if/when available), OR
        #   - PyExecJS / pynodejs bridge
        logger.info(
            "LitProtocolWalletManager initialised (stub) for network=%s",
            lit_network,
        )

    async def create_wallet(self, chain_ids: list[int] | None = None) -> WalletInfo:
        """
        Create a new PKP (Programmable Key Pair) on the Lit network.

        Production implementation would:
        1. Call ``LitContracts.pkpNftContractUtils.write.mint()`` to mint
           a new PKP NFT, which generates a distributed key pair.
        2. Store the PKP token ID and public key locally.
        3. Configure permitted Lit Actions for signing.
        4. Return a ``WalletInfo`` with the PKP's Ethereum address.

        The PKP address is derived from the distributed public key and works
        on all EVM chains.
        """
        # Reference implementation:
        # PKP minting via Lit SDK.
        #
        # JS equivalent:
        #   const pkp = await litContracts.pkpNftContractUtils.write.mint();
        #   const address = ethers.utils.computeAddress(pkp.publicKey);
        raise NotImplementedError(
            "LitProtocolWalletManager.create_wallet() is not yet implemented. "
            "See docstring for the Lit SDK calls that need to be wired up."
        )

    async def get_wallet(self, address: str) -> WalletInfo:
        """
        Look up a PKP by its Ethereum address.

        Production implementation would query local storage or the Lit
        PKP NFT contract for metadata about this key pair.
        """
        # Reference implementation:
        # Look up PKP metadata (token ID, public key, permissions)
        # from local cache or on-chain PKP NFT contract.
        raise NotImplementedError(
            "LitProtocolWalletManager.get_wallet() is not yet implemented."
        )

    async def sign_transaction(self, address: str, tx: dict[str, Any]) -> str:
        """
        Sign a transaction using the PKP's distributed key.

        Production implementation would:
        1. Serialize the transaction to RLP.
        2. Compute the signing hash (keccak256 of RLP-encoded unsigned tx).
        3. Call ``LitActions.signEcdsa()`` with the PKP public key to get
           a distributed ECDSA signature.
        4. Assemble the signed transaction and return the hex.

        JS equivalent:
        ```js
        const sigShare = await litNodeClient.executeJs({
            code: `
                const sigShare = await LitActions.signEcdsa({
                    toSign: txHash,
                    publicKey: pkpPublicKey,
                    sigName: "tx-sig",
                });
            `,
            authSig: authSig,
            jsParams: { txHash, pkpPublicKey },
        });
        ```
        """
        # Reference implementation: distributed signing via Lit Actions.
        raise NotImplementedError(
            "LitProtocolWalletManager.sign_transaction() is not yet implemented. "
            "See docstring for the Lit SDK signing flow."
        )

    async def get_balance(
        self,
        address: str,
        token: str,
        chain_id: int,
    ) -> str:
        """
        Query balance -- this does not require the private key, so it
        can use the same RPC-based approach as ``LocalWalletManager``.
        """
        token_upper = token.upper()
        if token_upper in ("ETH", "NATIVE"):
            return await get_native_balance(address, chain_id)

        if token.startswith("0x") and len(token) == 42:
            return await get_erc20_balance(token, address, chain_id)

        from minotaur_subnet.blockchain.tokens import get_token_address

        token_address = get_token_address(token, chain_id)
        return await get_erc20_balance(token_address, address, chain_id)

    async def list_wallets(self) -> list[WalletInfo]:
        """
        List all PKPs managed by this instance.

        Production implementation would enumerate PKP NFTs owned by
        the configured auth identity.
        """
        # Reference implementation: query owned PKP NFTs from the Lit contracts.
        raise NotImplementedError(
            "LitProtocolWalletManager.list_wallets() is not yet implemented."
        )
