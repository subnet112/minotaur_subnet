"""Validator identity attestation — binds an EVM signing address to a
Bittensor hotkey + axon URL via an EIP-712 signature.

Used by the peer-discovery flow: each validator's ``GET /identity`` endpoint
returns a freshly signed payload, and remote validators verify the signature
against the on-chain ``ValidatorRegistry.getValidators()`` list + the
Bittensor metagraph axon record.

Why off-chain (chain_id=0): identity attestation is not bound to any
specific operational chain — it's a self-published statement consumed by
peer-discovery. Using chain_id=0 makes the signature unusable as an
on-chain authorization (it can't be replayed against any deployed contract).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_defunct  # noqa: F401 (kept for parity with signatures.py)
from eth_hash.auto import keccak

from .eip712 import EIP712_DOMAIN_TYPEHASH


# Pinned to "MinotaurValidatorIdentity" v1 with chain_id=0 + zero contract,
# so the signature is unforgeable as an on-chain authz on any deployed App.
IDENTITY_DOMAIN_NAME = "MinotaurValidatorIdentity"
IDENTITY_DOMAIN_VERSION = "1"
IDENTITY_DOMAIN_CHAIN_ID = 0
IDENTITY_DOMAIN_CONTRACT = "0x" + "00" * 20

VALIDATOR_IDENTITY_TYPEHASH = keccak(
    b"ValidatorIdentity(address evmAddress,bytes32 hotkeyHash,"
    b"string axonUrl,uint256 expiry,bytes32 nonce)"
)


def _identity_domain_separator() -> bytes:
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [
            EIP712_DOMAIN_TYPEHASH,
            keccak(IDENTITY_DOMAIN_NAME.encode()),
            keccak(IDENTITY_DOMAIN_VERSION.encode()),
            IDENTITY_DOMAIN_CHAIN_ID,
            IDENTITY_DOMAIN_CONTRACT,
        ],
    ))


def _hotkey_hash(hotkey_ss58: str) -> bytes:
    """Bind variable-length SS58 hotkey to a 32-byte field for typed encoding.

    Both signer and verifier hash the same input. The hotkey is not
    sensitive (it's already public on the metagraph), so a plain keccak is
    sufficient.
    """
    return keccak(hotkey_ss58.encode())


@dataclass
class ValidatorIdentity:
    """Self-attested identity payload returned by ``GET /identity``."""

    evm_address: str
    hotkey: str       # SS58
    axon_url: str
    expiry: int       # unix timestamp; verifier rejects if now > expiry
    nonce: str        # 0x-prefixed 32-byte hex; per-signature freshness
    signature: str    # 0x-prefixed 65-byte hex

    def to_dict(self) -> dict:
        return {
            "evm_address": self.evm_address,
            "hotkey": self.hotkey,
            "axon_url": self.axon_url,
            "expiry": self.expiry,
            "nonce": self.nonce,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ValidatorIdentity":
        return cls(
            evm_address=str(data["evm_address"]),
            hotkey=str(data["hotkey"]),
            axon_url=str(data["axon_url"]),
            expiry=int(data["expiry"]),
            nonce=str(data["nonce"]),
            signature=str(data["signature"]),
        )


def _struct_hash(
    evm_address: str,
    hotkey: str,
    axon_url: str,
    expiry: int,
    nonce: bytes,
) -> bytes:
    if len(nonce) != 32:
        raise ValueError(f"nonce must be 32 bytes, got {len(nonce)}")
    return keccak(abi_encode(
        ["bytes32", "address", "bytes32", "bytes32", "uint256", "bytes32"],
        [
            VALIDATOR_IDENTITY_TYPEHASH,
            evm_address,
            _hotkey_hash(hotkey),
            keccak(axon_url.encode()),
            expiry,
            nonce,
        ],
    ))


def _digest(struct_hash: bytes) -> bytes:
    return keccak(b"\x19\x01" + _identity_domain_separator() + struct_hash)


def sign_identity(
    private_key: str,
    hotkey: str,
    axon_url: str,
    *,
    ttl_seconds: int = 300,
    now: int | None = None,
    nonce: bytes | None = None,
) -> ValidatorIdentity:
    """Build and sign a fresh identity attestation.

    Args:
        private_key: Hex (0x-prefixed) EVM private key.
        hotkey: Bittensor SS58 hotkey string.
        axon_url: HTTP URL this validator serves at (e.g. http://host:9100).
        ttl_seconds: How long the signature is valid for. Short by default
            (5 min) so a stolen/replayed payload quickly becomes stale.
        now: Override for the current unix time (testing).
        nonce: Override the random nonce (testing).

    Returns:
        A populated ``ValidatorIdentity``.
    """
    if now is None:
        now = int(time.time())
    if nonce is None:
        nonce = secrets.token_bytes(32)
    expiry = now + ttl_seconds

    acct = Account.from_key(private_key)
    evm_address = acct.address

    sh = _struct_hash(evm_address, hotkey, axon_url, expiry, nonce)
    digest = _digest(sh)
    signed = Account._sign_hash(digest, private_key=private_key)

    return ValidatorIdentity(
        evm_address=evm_address,
        hotkey=hotkey,
        axon_url=axon_url,
        expiry=expiry,
        nonce="0x" + nonce.hex(),
        signature=signed.signature.hex() if signed.signature.hex().startswith("0x")
                  else "0x" + signed.signature.hex(),
    )


def verify_identity(
    identity: ValidatorIdentity,
    *,
    now: int | None = None,
) -> str | None:
    """Verify an identity payload's signature + freshness.

    Returns:
        The recovered EVM address if the signature is valid and unexpired;
        None on any failure (caller logs / discards).

    The caller is responsible for the additional cross-checks:
      - recovered address ∈ ValidatorRegistry.getValidators()
      - identity.hotkey matches what the metagraph says is at axon_url
      - identity.axon_url matches what the metagraph publishes for the hotkey

    Those checks live outside this module to keep it dependency-free.
    """
    if now is None:
        now = int(time.time())
    if now > identity.expiry:
        return None

    try:
        nonce_hex = identity.nonce
        if nonce_hex.startswith("0x"):
            nonce_hex = nonce_hex[2:]
        nonce = bytes.fromhex(nonce_hex)
        if len(nonce) != 32:
            return None

        sh = _struct_hash(
            identity.evm_address,
            identity.hotkey,
            identity.axon_url,
            identity.expiry,
            nonce,
        )
        digest = _digest(sh)

        sig_hex = identity.signature
        if sig_hex.startswith("0x"):
            sig_hex = sig_hex[2:]
        sig_bytes = bytes.fromhex(sig_hex)
        if len(sig_bytes) != 65:
            return None

        recovered = Account._recover_hash(digest, signature=sig_bytes)
        if recovered.lower() != identity.evm_address.lower():
            return None
        return recovered
    except Exception:
        return None
