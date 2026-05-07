"""Unit tests for ERC-7930 / CAIP-10 InteropAddress."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from minotaur_subnet.shared.interop_address import (
    InteropAddress,
    normalize_address,
    parse_address,
    validate_address,
)


# ---------------------------------------------------------------------------
# Well-known addresses for tests (EIP-55 checksummed)
# ---------------------------------------------------------------------------

USDC_MAINNET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH_MAINNET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


# ═══════════════════════════════════════════════════════════════════════════
#  Parse plain 0x addresses
# ═══════════════════════════════════════════════════════════════════════════


class TestParsePlain:
    def test_lowercase(self):
        ia = InteropAddress.parse(USDC_MAINNET.lower())
        assert ia.address == USDC_MAINNET
        assert ia.chain_id is None

    def test_uppercase(self):
        ia = InteropAddress.parse("0x" + USDC_MAINNET[2:].upper())
        assert ia.address == USDC_MAINNET

    def test_mixed_case_checksum(self):
        ia = InteropAddress.parse(USDC_MAINNET)
        assert ia.address == USDC_MAINNET

    def test_default_chain_id(self):
        ia = InteropAddress.parse(USDC_MAINNET, default_chain_id=1)
        assert ia.chain_id == 1

    def test_default_chain_id_none(self):
        ia = InteropAddress.parse(USDC_MAINNET)
        assert ia.chain_id is None

    def test_weth_checksum(self):
        ia = InteropAddress.parse(WETH_MAINNET.lower())
        assert ia.address == WETH_MAINNET

    def test_vitalik_checksum(self):
        ia = InteropAddress.parse(VITALIK.lower())
        assert ia.address == VITALIK

    def test_zero_address(self):
        ia = InteropAddress.parse("0x" + "0" * 40)
        assert ia.address == "0x" + "0" * 40

    def test_whitespace_stripped(self):
        ia = InteropAddress.parse(f"  {USDC_MAINNET}  ")
        assert ia.address == USDC_MAINNET


# ═══════════════════════════════════════════════════════════════════════════
#  Parse CAIP-10
# ═══════════════════════════════════════════════════════════════════════════


class TestParseCaip10:
    def test_mainnet(self):
        ia = InteropAddress.parse(f"eip155:1:{USDC_MAINNET}")
        assert ia.address == USDC_MAINNET
        assert ia.chain_id == 1

    def test_base(self):
        addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        ia = InteropAddress.parse(f"eip155:8453:{addr}")
        assert ia.chain_id == 8453

    def test_arbitrum(self):
        addr = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
        ia = InteropAddress.parse(f"eip155:42161:{addr}")
        assert ia.chain_id == 42161

    def test_optimism(self):
        addr = "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"
        ia = InteropAddress.parse(f"eip155:10:{addr}")
        assert ia.chain_id == 10

    def test_local_anvil(self):
        ia = InteropAddress.parse(f"eip155:31337:{VITALIK}")
        assert ia.chain_id == 31337

    def test_caip10_lowercase_address(self):
        ia = InteropAddress.parse(f"eip155:1:{USDC_MAINNET.lower()}")
        assert ia.address == USDC_MAINNET
        assert ia.chain_id == 1

    def test_caip10_overrides_default_chain_id(self):
        ia = InteropAddress.parse(f"eip155:8453:{USDC_MAINNET}", default_chain_id=1)
        assert ia.chain_id == 8453  # CAIP-10 wins


# ═══════════════════════════════════════════════════════════════════════════
#  Parse ERC-7930 binary hex
# ═══════════════════════════════════════════════════════════════════════════


class TestParseErc7930:
    def _encode(self, address: str, chain_id: int) -> str:
        """Helper: build ERC-7930 hex string."""
        chain_ref = chain_id.to_bytes(
            (chain_id.bit_length() + 7) // 8 or 1, "big"
        )
        addr_bytes = bytes.fromhex(address[2:])
        payload = b"\x01\x00\x00" + chain_ref + addr_bytes
        return "0x" + payload.hex()

    def test_roundtrip_mainnet(self):
        encoded = self._encode(USDC_MAINNET, 1)
        ia = InteropAddress.parse(encoded)
        assert ia.address == USDC_MAINNET
        assert ia.chain_id == 1

    def test_roundtrip_base(self):
        addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        encoded = self._encode(addr, 8453)
        ia = InteropAddress.parse(encoded)
        assert ia.chain_id == 8453

    def test_roundtrip_arbitrum(self):
        encoded = self._encode(WETH_MAINNET, 42161)
        ia = InteropAddress.parse(encoded)
        assert ia.chain_id == 42161
        assert ia.address == WETH_MAINNET

    def test_to_erc7930_then_parse(self):
        original = InteropAddress(address=USDC_MAINNET, chain_id=1)
        binary = original.to_erc7930()
        hex_str = "0x" + binary.hex()
        parsed = InteropAddress.parse(hex_str)
        assert parsed == original

    def test_to_erc7930_large_chain_id(self):
        original = InteropAddress(address=VITALIK, chain_id=42161)
        binary = original.to_erc7930()
        parsed = InteropAddress._decode_erc7930("0x" + binary.hex())
        assert parsed.chain_id == 42161


# ═══════════════════════════════════════════════════════════════════════════
#  Formatting
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatting:
    def test_to_caip10(self):
        ia = InteropAddress(address=USDC_MAINNET, chain_id=1)
        assert ia.to_caip10() == f"eip155:1:{USDC_MAINNET}"

    def test_to_caip10_no_chain_raises(self):
        ia = InteropAddress(address=USDC_MAINNET)
        with pytest.raises(ValueError, match="chain_id"):
            ia.to_caip10()

    def test_str_with_chain(self):
        ia = InteropAddress(address=USDC_MAINNET, chain_id=1)
        assert str(ia) == f"eip155:1:{USDC_MAINNET}"

    def test_str_without_chain(self):
        ia = InteropAddress(address=USDC_MAINNET)
        assert str(ia) == USDC_MAINNET

    def test_to_erc7930_no_chain_raises(self):
        ia = InteropAddress(address=USDC_MAINNET)
        with pytest.raises(ValueError, match="chain_id"):
            ia.to_erc7930()

    def test_to_erc7930_bytes(self):
        ia = InteropAddress(address=USDC_MAINNET, chain_id=1)
        data = ia.to_erc7930()
        assert isinstance(data, bytes)
        assert data[0] == 0x01  # version
        assert data[1:3] == b"\x00\x00"  # EVM chain type


# ═══════════════════════════════════════════════════════════════════════════
#  Equality and hashing
# ═══════════════════════════════════════════════════════════════════════════


class TestEquality:
    def test_same_address_same_chain(self):
        a = InteropAddress(address=USDC_MAINNET, chain_id=1)
        b = InteropAddress(address=USDC_MAINNET, chain_id=1)
        assert a == b

    def test_case_insensitive(self):
        a = InteropAddress(address=USDC_MAINNET, chain_id=1)
        b = InteropAddress(address=USDC_MAINNET.lower(), chain_id=1)
        assert a == b

    def test_different_chain_not_equal(self):
        a = InteropAddress(address=USDC_MAINNET, chain_id=1)
        b = InteropAddress(address=USDC_MAINNET, chain_id=8453)
        assert a != b

    def test_none_chain_not_equal_to_chain(self):
        a = InteropAddress(address=USDC_MAINNET, chain_id=None)
        b = InteropAddress(address=USDC_MAINNET, chain_id=1)
        assert a != b

    def test_hash_same(self):
        a = InteropAddress(address=USDC_MAINNET, chain_id=1)
        b = InteropAddress(address=USDC_MAINNET.lower(), chain_id=1)
        assert hash(a) == hash(b)

    def test_usable_in_set(self):
        s = {
            InteropAddress(address=USDC_MAINNET, chain_id=1),
            InteropAddress(address=USDC_MAINNET.lower(), chain_id=1),
        }
        assert len(s) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Error cases
# ═══════════════════════════════════════════════════════════════════════════


class TestErrors:
    def test_empty_string(self):
        with pytest.raises(ValueError, match="empty"):
            InteropAddress.parse("")

    def test_too_short(self):
        with pytest.raises(ValueError):
            InteropAddress.parse("0xabc")

    def test_too_long_plain(self):
        # 41 hex chars = not 40
        with pytest.raises(ValueError):
            InteropAddress.parse("0x" + "a" * 41)

    def test_bad_hex_chars(self):
        with pytest.raises(ValueError, match="non-hex"):
            InteropAddress.parse("0x" + "g" * 40)

    def test_non_eip155_namespace(self):
        with pytest.raises(ValueError):
            InteropAddress.parse(f"cosmos:1:{USDC_MAINNET}")

    def test_caip10_missing_address(self):
        with pytest.raises(ValueError):
            InteropAddress.parse("eip155:1:")

    def test_erc7930_bad_version(self):
        # version 0x02 not supported
        payload = b"\x02\x00\x00\x01" + bytes.fromhex(USDC_MAINNET[2:])
        with pytest.raises(ValueError, match="version"):
            InteropAddress._decode_erc7930("0x" + payload.hex())

    def test_erc7930_bad_chain_type(self):
        # chain type 0x0001 (non-EVM) not supported
        payload = b"\x01\x00\x01\x01" + bytes.fromhex(USDC_MAINNET[2:])
        with pytest.raises(ValueError, match="chain type"):
            InteropAddress._decode_erc7930("0x" + payload.hex())

    def test_erc7930_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            InteropAddress._decode_erc7930("0x" + "00" * 10)

    def test_random_string(self):
        with pytest.raises(ValueError, match="Unrecognised"):
            InteropAddress.parse("not-an-address")


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience functions
# ═══════════════════════════════════════════════════════════════════════════


class TestConvenienceFunctions:
    def test_parse_address_plain(self):
        ia = parse_address(USDC_MAINNET, default_chain_id=1)
        assert ia.address == USDC_MAINNET
        assert ia.chain_id == 1

    def test_parse_address_caip10(self):
        ia = parse_address(f"eip155:8453:{USDC_MAINNET}")
        assert ia.chain_id == 8453

    def test_normalize_address(self):
        result = normalize_address(USDC_MAINNET.lower())
        assert result == USDC_MAINNET

    def test_normalize_from_caip10(self):
        result = normalize_address(f"eip155:1:{USDC_MAINNET}")
        assert result == USDC_MAINNET

    def test_validate_address_valid(self):
        result = validate_address(USDC_MAINNET.lower())
        assert result == USDC_MAINNET

    def test_validate_address_invalid(self):
        with pytest.raises(ValueError):
            validate_address("not-an-address")

    def test_validate_address_too_short(self):
        with pytest.raises(ValueError):
            validate_address("0xabc")


# ═══════════════════════════════════════════════════════════════════════════
#  EIP-55 checksum verification against known addresses
# ═══════════════════════════════════════════════════════════════════════════


class TestEip55Checksum:
    """Verify EIP-55 output matches known checksummed addresses."""

    @pytest.mark.parametrize("expected", [
        USDC_MAINNET,
        WETH_MAINNET,
        VITALIK,
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC on Arbitrum
    ])
    def test_known_checksums(self, expected):
        ia = InteropAddress.parse(expected.lower())
        assert ia.address == expected
