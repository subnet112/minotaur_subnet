"""Tests for Miner registration, update, and deregistration flows."""
import logging
import types
from unittest.mock import MagicMock, patch

import pytest

from neurons.miner import Miner, generate_hotkey, ss58_encode


class DummyLogger:
    def __init__(self):
        self.messages = []

    def info(self, *args, **kwargs):
        self.messages.append(("info", args, kwargs))

    def debug(self, *args, **kwargs):
        self.messages.append(("debug", args, kwargs))

    def warning(self, *args, **kwargs):
        self.messages.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.messages.append(("error", args, kwargs))

    def success(self, *args, **kwargs):
        self.messages.append(("success", args, kwargs))


def test_generate_hotkey_deterministic():
    """Same miner_id should produce same hotkey."""
    hk1, sk1 = generate_hotkey("test-miner")
    hk2, sk2 = generate_hotkey("test-miner")

    assert hk1 == hk2
    assert sk1.encode() == sk2.encode()


def test_generate_hotkey_different_ids():
    """Different miner_ids should produce different hotkeys."""
    hk1, _ = generate_hotkey("miner-1")
    hk2, _ = generate_hotkey("miner-2")

    assert hk1 != hk2


def test_ss58_encode_valid():
    """SS58 encoding should produce valid addresses."""
    _, signing_key = generate_hotkey("test")
    public_key = signing_key.verify_key.encode()

    address = ss58_encode(public_key)

    assert len(address) > 0
    assert address[0].isdigit() or address[0].isupper()


def test_ss58_encode_invalid_key_length():
    """SS58 encoding should reject invalid key lengths."""
    with pytest.raises(ValueError, match="32 bytes"):
        ss58_encode(b"short")


def test_miner_simulation_mode_signature_type():
    """Simulation mode should use ed25519 signing."""
    miner = Miner(
        miner_id="test-sim",
        aggregator_url="http://localhost",
        miner_api_key="test-key",
        mode="simulation",
        logger=DummyLogger(),
    )

    assert miner.signature_type == "ed25519"
    assert miner.signing_key is not None
    assert miner.signing_keypair is None


def test_miner_registration_message_format():
    """Registration message should follow expected format."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
    )

    solver_config = {
        "solver_id": "solver-01",
        "endpoint": "http://localhost:8000",
    }

    message = miner._registration_message(solver_config)

    assert "oif-register-solver" in message
    assert miner.miner_id in message
    assert "solver-01" in message
    assert "http://localhost:8000" in message


def test_miner_update_message_format():
    """Update message should follow expected format."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
    )

    message = miner._update_message("solver-01", "http://new-endpoint:8000")

    assert "oif-update-solver" in message
    assert miner.miner_id in message
    assert "solver-01" in message
    assert "http://new-endpoint:8000" in message


def test_miner_delete_message_format():
    """Delete message should follow expected format."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
    )

    message = miner._delete_message("solver-01")

    assert "oif-delete-solver" in message
    assert miner.miner_id in message
    assert "solver-01" in message


def test_miner_sign_message_simulation():
    """Signing should work in simulation mode."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
    )

    signature = miner._sign_message(b"test message")

    assert signature.startswith("0x")
    # ed25519 signatures are 64 bytes = 128 hex chars + 2 for 0x
    assert len(signature) == 130


def test_miner_requires_api_key():
    """Miner should require API key."""
    with pytest.raises(ValueError, match="MINER_API_KEY"):
        Miner(
            miner_id="test",
            aggregator_url="http://localhost",
            miner_api_key=None,
            mode="simulation",
        )


def test_miner_requires_wallet_in_bittensor_mode():
    """Bittensor mode should require wallet."""
    with pytest.raises(ValueError, match="Wallet is required"):
        Miner(
            miner_id="test",
            aggregator_url="http://localhost",
            miner_api_key="key",
            mode="bittensor",
            wallet=None,
        )


def test_miner_create_solver_config():
    """Solver config should have required fields."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
        solver_host="192.168.1.100",
        base_port=9000,
    )

    config = miner.create_solver(0, latency_ms=100, quality=0.9)

    assert "solver_id" in config
    assert config["port"] == 9000
    assert config["latency_ms"] == 100
    assert config["quality"] == 0.9
    assert "http://192.168.1.100:9000" in config["endpoint"]
    assert "http://127.0.0.1:9000" in config["local_endpoint"]


def test_miner_num_solvers_minimum():
    """Num solvers should be at least 1."""
    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
        num_solvers=0,
    )

    assert miner.num_solvers >= 1


def test_miner_signature_verification_ed25519():
    """Ed25519 signatures should be verifiable."""
    from nacl.signing import VerifyKey

    miner = Miner(
        miner_id="test",
        aggregator_url="http://localhost",
        miner_api_key="key",
        mode="simulation",
        logger=DummyLogger(),
    )

    message = b"test message for verification"
    signature = miner._sign_message(message)

    # Verify the signature
    signature_bytes = bytes.fromhex(signature[2:])
    verify_key = miner.signing_key.verify_key
    verify_key.verify(message, signature_bytes)  # Should not raise

