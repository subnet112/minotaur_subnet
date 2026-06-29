"""Golden-master + invariant tests for swap intentParams encoding.

Swap orders are now encoded through the single, generic, manifest-driven
encoder (`build_intent_params_hex_from_manifest`) — the app-specific
`build_swap_intent_params_hex` was removed so no DexAggregator field names
(`output_token`, `unwrap_output`, `min_output_amount`, …) live in the platform
daemon. The layout comes entirely from the app's manifest.

This path is load-bearing for order signing, the validator benchmark, and the
score endpoint, so a byte of unintended drift would break EIP-712 signatures
and desync consensus. The golden bytes live in
``tests/unit/fixtures/swap_encoding_golden.json`` (committed, generated from the
generic encoder + the manifest below). Changing them is a behavior change and
must be intentional — regenerate the fixture deliberately.

The manifest mirrors what `minotaur-apps` declares for DexAggregatorApp,
including ``unwrap_output`` with a static ``default: true``. The contract itself
guards ``tokenOut == wrappedNative`` before unwrapping, so a blanket true is
correct for every token and needs no token-aware logic in the daemon.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_abi import decode as abi_decode

from minotaur_subnet.api import services

# DexAggregator swap manifest — the cross-repo contract with minotaur-apps.
SWAP_MANIFEST = {
    "app_name": "DexAggregatorApp",
    "intent_functions": [{
        "name": "swap",
        "params": {
            "input_token":      {"type": "address", "source": "user"},
            "output_token":     {"type": "address", "source": "user"},
            "input_amount":     {"type": "uint256", "source": "user"},
            "min_output_amount":{"type": "uint256", "source": "quote"},
            "receiver":         {"type": "address", "source": "system"},
            "permit_deadline":  {"type": "uint256", "source": "system"},
            "permit_v":         {"type": "uint8",   "source": "system"},
            "permit_r":         {"type": "bytes32", "source": "system"},
            "permit_s":         {"type": "bytes32", "source": "system"},
            "platform_fee_wei": {"type": "uint256", "source": "quote", "in_signature": False},
            "quoted_output":    {"type": "uint256", "source": "quote", "in_signature": False},
            "unwrap_output":    {"type": "bool",    "source": "system", "in_signature": False, "default": True},
        },
    }],
}

# 12-field tuple type for decoding/verifying the encoded params.
_SWAP_ABI_TYPES = [
    "address", "address", "uint256", "uint256", "address",
    "uint256", "uint8", "bytes32", "bytes32",
    "uint256", "uint256", "bool",
]

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "swap_encoding_golden.json").read_text()
)
SUBMITTED_BY = _FIXTURE["submitted_by"]
GOLDEN_CASES = _FIXTURE["cases"]

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


class _FakeJsEngine:
    def get_manifest(self, _app_id):
        return SWAP_MANIFEST


def _encode(params):
    return services.build_intent_params_hex_from_manifest(
        None, _FakeJsEngine(), "dex", "swap", dict(params), SUBMITTED_BY,
    )


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_generic_encoder_golden_master(name):
    """The generic manifest encoder emits exactly the frozen bytes for each
    representative order (ERC-20 out, WETH out, permit, quote-fields omitted)."""
    case = GOLDEN_CASES[name]
    got = _encode(case["params"])
    assert got is not None, f"{name}: encoder returned None unexpectedly"
    assert got == case["expected_hex"], (
        f"{name}: swap encoding changed — this is a behavior change to a "
        f"signing/consensus-critical path. Regenerate the golden fixture only "
        f"if the change is intentional."
    )


def test_signature_fields_round_trip():
    """The signature fields (tokens/amounts/min-out/receiver) the user signs
    decode back to exactly what was supplied — the consensus-critical values."""
    params = {
        "input_token": "0x" + "11" * 20, "output_token": "0x" + "22" * 20,
        "input_amount": "123", "min_output_amount": "100", "receiver": "0x" + "33" * 20,
        "platform_fee_wei": "7", "quoted_output": "150",
    }
    decoded = abi_decode(_SWAP_ABI_TYPES, bytes.fromhex(_encode(params)))
    assert decoded[0].lower() == "0x" + "11" * 20
    assert decoded[1].lower() == "0x" + "22" * 20
    assert decoded[2] == 123
    assert decoded[3] == 100
    assert decoded[4].lower() == "0x" + "33" * 20
    assert decoded[9] == 7      # platform_fee_wei
    assert decoded[10] == 150   # quoted_output


def test_receiver_defaults_to_submitter():
    """Universal intent semantic: an omitted receiver delivers to the order's
    submitter (not the zero address)."""
    params = {
        "input_token": USDC, "output_token": "0x" + "22" * 20,
        "input_amount": "1", "min_output_amount": "1",
    }
    decoded = abi_decode(_SWAP_ABI_TYPES, bytes.fromhex(_encode(params)))
    assert decoded[4].lower() == SUBMITTED_BY.lower()


@pytest.mark.parametrize("output_token", [
    "0x4200000000000000000000000000000000000006",  # WETH (Base)
    "0x" + "22" * 20,                               # arbitrary ERC-20
])
def test_unwrap_output_is_manifest_default_not_token_heuristic(output_token):
    """unwrap_output comes from the manifest's static default (true) for EVERY
    output token — the daemon does NOT inspect output_token. The contract's own
    `tokenOut == wrappedNative` guard decides whether an unwrap actually happens.
    """
    params = {
        "input_token": USDC, "output_token": output_token,
        "input_amount": "1", "min_output_amount": "1",
    }
    decoded = abi_decode(_SWAP_ABI_TYPES, bytes.fromhex(_encode(params)))
    assert decoded[11] is True


def test_encoder_defaults_omitted_fields_no_app_knowledge():
    """The generic encoder carries ZERO app knowledge: it never inspects field
    *names* to decide requiredness. An omitted field falls back to the manifest
    default (e.g. unwrap_output) or the ABI type default (0 / zero-address) and
    the full fixed-width tuple is still emitted. Supplying mandatory values
    (e.g. min_output_amount) is the caller's/quote's responsibility, not the
    encoder's."""
    # Omit min_output_amount and all quote fields — still encodes (no guard).
    out = _encode({
        "input_token": USDC, "output_token": "0x" + "22" * 20, "input_amount": "1",
    })
    assert out is not None
    decoded = abi_decode(_SWAP_ABI_TYPES, bytes.fromhex(out))
    assert decoded[3] == 0    # min_output_amount → type default 0
    assert decoded[9] == 0    # platform_fee_wei → type default 0
    assert decoded[10] == 0   # quoted_output → type default 0
    assert decoded[11] is True  # unwrap_output → manifest default


def test_encoder_returns_none_without_a_manifest():
    """No manifest / unknown intent → None (the ONE legitimate reason the
    generic encoder bails — there's nothing to encode against)."""
    class _NoManifest:
        def get_manifest(self, _app_id):
            return None
    assert services.build_intent_params_hex_from_manifest(
        None, _NoManifest(), "dex", "swap",
        {"input_token": USDC, "output_token": "0x" + "22" * 20, "input_amount": "1"},
        SUBMITTED_BY,
    ) is None
