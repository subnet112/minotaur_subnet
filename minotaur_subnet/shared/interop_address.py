"""ERC-7930 / CAIP-10 interop address support.

Accepts chain-aware addresses at API boundaries while keeping plain
``0x`` addresses internally. Three input formats are supported:

- **CAIP-10 text**: ``eip155:1:0xA0b86991...``
- **ERC-7930 binary hex**: ``0x01 0000 ...`` (version 1, EVM chain type)
- **Plain 0x** (42 chars): uses ``default_chain_id``

All outputs are EIP-55 checksummed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from eth_hash.auto import keccak

# ---------------------------------------------------------------------------
# EIP-55 checksum
# ---------------------------------------------------------------------------

def _eip55_checksum(address: str) -> str:
    """Apply EIP-55 mixed-case checksum to a 40-hex-char address."""
    addr_lower = address.lower().removeprefix("0x")
    hash_hex = keccak(addr_lower.encode()).hex()
    out = []
    for i, ch in enumerate(addr_lower):
        if ch in "0123456789":
            out.append(ch)
        else:
            out.append(ch.upper() if int(hash_hex[i], 16) >= 8 else ch)
    return "0x" + "".join(out)


def _validate_hex_address(raw: str) -> str:
    """Validate a 0x-prefixed hex address and return EIP-55 checksummed form.

    Raises ``ValueError`` on bad format.
    """
    if not raw.startswith("0x"):
        raise ValueError(f"Address must start with 0x, got: {raw!r}")
    body = raw[2:]
    if len(body) != 40:
        raise ValueError(
            f"Address must be 20 bytes (40 hex chars), got {len(body)} chars"
        )
    if not all(c in "0123456789abcdefABCDEF" for c in body):
        raise ValueError(f"Address contains non-hex characters: {raw!r}")
    return _eip55_checksum(raw)


# ---------------------------------------------------------------------------
# CAIP-10 regex:  eip155:<chain_id>:<0x address>
# ---------------------------------------------------------------------------

_CAIP10_RE = re.compile(
    r"^eip155:(\d+):(0x[0-9a-fA-F]{40})$"
)


# ---------------------------------------------------------------------------
# InteropAddress dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InteropAddress:
    """A chain-aware Ethereum address.

    ``address`` is always EIP-55 checksummed (42 chars).
    ``chain_id`` is ``None`` when the input was a plain ``0x`` address
    with no chain context supplied.
    """

    address: str
    chain_id: int | None = None

    # -- Parsing --------------------------------------------------------

    @classmethod
    def parse(
        cls,
        value: str,
        default_chain_id: int | None = None,
    ) -> InteropAddress:
        """Parse an address from CAIP-10 text, ERC-7930 hex, or plain 0x.

        Args:
            value: The address string.
            default_chain_id: Chain ID to use for plain ``0x`` inputs.

        Raises:
            ValueError: If the input format is invalid.
        """
        if not value:
            raise ValueError("Address cannot be empty")

        value = value.strip()

        # 1) CAIP-10 text:  eip155:<chain_id>:<0x address>
        m = _CAIP10_RE.match(value)
        if m:
            chain_id = int(m.group(1))
            address = _validate_hex_address(m.group(2))
            return cls(address=address, chain_id=chain_id)

        # 2) Plain 0x address (exactly 42 chars)
        if value.startswith("0x") and len(value) == 42:
            address = _validate_hex_address(value)
            return cls(address=address, chain_id=default_chain_id)

        # 3) ERC-7930 binary hex (starts with 0x, longer than 42 chars)
        if value.startswith("0x") and len(value) > 42:
            return cls._decode_erc7930(value)

        # 4) Bare CAIP-10 without 0x prefix — reject with helpful message
        if value.startswith("eip155:"):
            raise ValueError(
                f"Invalid CAIP-10 address: {value!r}. "
                "Expected format: eip155:<chain_id>:<0x address>"
            )

        raise ValueError(
            f"Unrecognised address format: {value!r}. "
            "Expected plain 0x address, CAIP-10 (eip155:<chain>:<0x...>), "
            "or ERC-7930 binary hex."
        )

    # -- ERC-7930 encoding/decoding ------------------------------------

    @classmethod
    def _decode_erc7930(cls, hex_str: str) -> InteropAddress:
        """Decode an ERC-7930 binary-encoded interop address.

        ERC-7930 format (version 1, EVM):
            byte 0:     version (0x01)
            byte 1-2:   chain type (0x0000 = EVM)
            bytes 3..N: chain reference (big-endian, minimal)
            last 20:    address bytes

        Raises ValueError on invalid encoding.
        """
        try:
            raw = bytes.fromhex(hex_str[2:])
        except ValueError:
            raise ValueError(f"ERC-7930 hex contains non-hex characters: {hex_str!r}")

        # Minimum: 1 (version) + 2 (chain type) + 1 (chain ref) + 20 (address) = 24
        if len(raw) < 24:
            raise ValueError(
                f"ERC-7930 payload too short: {len(raw)} bytes (need >= 24)"
            )

        version = raw[0]
        if version != 0x01:
            raise ValueError(f"Unsupported ERC-7930 version: {version}")

        chain_type = int.from_bytes(raw[1:3], "big")
        if chain_type != 0x0000:
            raise ValueError(
                f"Unsupported chain type: 0x{chain_type:04x} (only EVM 0x0000 supported)"
            )

        # Address is always last 20 bytes
        address_bytes = raw[-20:]
        # Chain reference is everything between chain type and address
        chain_ref_bytes = raw[3:-20]
        if not chain_ref_bytes:
            raise ValueError("ERC-7930 missing chain reference bytes")

        chain_id = int.from_bytes(chain_ref_bytes, "big")
        address = _eip55_checksum("0x" + address_bytes.hex())
        return cls(address=address, chain_id=chain_id)

    def to_erc7930(self) -> bytes:
        """Encode as ERC-7930 binary bytes.

        Raises ValueError if chain_id is None.
        """
        if self.chain_id is None:
            raise ValueError("Cannot encode to ERC-7930 without chain_id")

        # Minimal big-endian encoding of chain_id
        chain_ref = self.chain_id.to_bytes(
            (self.chain_id.bit_length() + 7) // 8 or 1, "big"
        )
        address_bytes = bytes.fromhex(self.address[2:])

        return (
            b"\x01"                          # version 1
            + b"\x00\x00"                    # chain type: EVM
            + chain_ref
            + address_bytes
        )

    # -- CAIP-10 formatting --------------------------------------------

    def to_caip10(self) -> str:
        """Format as CAIP-10 text: ``eip155:<chain_id>:<address>``.

        Raises ValueError if chain_id is None.
        """
        if self.chain_id is None:
            raise ValueError("Cannot format as CAIP-10 without chain_id")
        return f"eip155:{self.chain_id}:{self.address}"

    # -- dunder methods ------------------------------------------------

    def __str__(self) -> str:
        if self.chain_id is not None:
            return self.to_caip10()
        return self.address

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InteropAddress):
            return NotImplemented
        return (
            self.address.lower() == other.address.lower()
            and self.chain_id == other.chain_id
        )

    def __hash__(self) -> int:
        return hash((self.address.lower(), self.chain_id))


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def parse_address(
    value: str,
    default_chain_id: int | None = None,
) -> InteropAddress:
    """Parse an address string into an ``InteropAddress``.

    Primary entry point for API handlers. Accepts CAIP-10, ERC-7930, or
    plain ``0x`` addresses.
    """
    return InteropAddress.parse(value, default_chain_id=default_chain_id)


def normalize_address(value: str) -> str:
    """Parse and return the EIP-55 checksummed plain ``0x`` address.

    Chain information (if present) is discarded.
    """
    return InteropAddress.parse(value).address


def validate_address(value: str) -> str:
    """Validate an address and return it EIP-55 checksummed.

    Raises ``ValueError`` on invalid input.
    """
    return _validate_hex_address(value)


# ---------------------------------------------------------------------------
# SS58 ↔ H160 address conversion (Bittensor EVM)
# ---------------------------------------------------------------------------

def h160_to_ss58(h160_address: str, network_id: int = 42) -> str:
    """Convert an EVM H160 address to its SS58 representation on Substrate.

    Bittensor EVM maps ``blake2b(b"evm:" + address_bytes)`` to a 32-byte
    account ID, which is then SS58-encoded with *network_id* (42 for
    Bittensor mainnet).

    This is a **display-only** utility. The platform uses H160 internally.

    Args:
        h160_address: The 0x-prefixed 20-byte Ethereum address.
        network_id: SS58 network prefix (default 42 = Bittensor).

    Returns:
        SS58-encoded address string.

    Raises:
        ValueError: If the address format is invalid.
        ImportError: If ``base58`` is not installed.
    """
    import hashlib
    import base58

    addr = h160_address.lower().removeprefix("0x")
    if len(addr) != 40:
        raise ValueError(f"Expected 20-byte address, got {len(addr) // 2} bytes")
    addr_bytes = bytes.fromhex(addr)

    # Substrate account ID: blake2b("evm:" + address, digest_size=32)
    account_id = hashlib.blake2b(b"evm:" + addr_bytes, digest_size=32).digest()

    # SS58 encoding: prefix + account_id + checksum
    # For network_id < 64: single-byte prefix
    # For network_id 64..16383: two-byte prefix (big-endian, interleaved)
    if network_id < 64:
        payload = bytes([network_id]) + account_id
    else:
        # Two-byte prefix encoding per SS58 spec
        first = ((network_id & 0xFC) >> 2) | 0x40
        second = (network_id >> 8) | ((network_id & 0x03) << 6)
        payload = bytes([first, second]) + account_id

    # SS58 checksum: blake2b(b"SS58PRE" + payload, digest_size=64)[:2]
    checksum = hashlib.blake2b(
        b"SS58PRE" + payload, digest_size=64,
    ).digest()[:2]

    return base58.b58encode(payload + checksum).decode()


def ss58_to_h160(ss58_address: str) -> str | None:
    """Attempt to recover an H160 EVM address from an SS58 address.

    This only works for addresses that were derived from an EVM H160 via
    ``h160_to_ss58``. Since the derivation uses blake2b (a one-way hash),
    we cannot reverse it in general. This function is provided for
    documentation purposes and returns ``None``.

    For actual EVM↔SS58 mapping, query the Bittensor EVM chain directly
    via the ``evmAccounts`` pallet.
    """
    return None
