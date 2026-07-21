"""Guards for the agent's submission signing (minotaur_subnet.miner.agent.loop).

Regression cover for the bug where the agent signed every submission with a
freshly generated throwaway keypair — a valid-looking signature for an ss58 the
SN112 metagraph has never seen, so the leader could not attribute the
submission and silently rejected it on mainnet.
"""

from __future__ import annotations

import base64
import sys
import types

from minotaur_subnet.miner.agent.loop import _sign_submission


def test_sign_submission_never_fabricates_hotkey_when_no_wallet(tmp_path, monkeypatch):
    # No wallet available → explicitly UNSIGNED, attributed to the miner_id.
    # It must NOT invent a random ss58 (the old bug). An empty signature makes
    # a local testnet accept the submission and mainnet correctly refuse it.
    monkeypatch.setenv("BT_WALLET_PATH", str(tmp_path))
    monkeypatch.delenv("MINER_WALLET_NAME", raising=False)
    monkeypatch.delenv("MINER_HOTKEY", raising=False)
    monkeypatch.delenv("MINER_HOTKEY_NAME", raising=False)

    hotkey, signature = _sign_submission("1:deadbeef:round-1", "miner-xyz")

    assert hotkey == "miner-xyz"
    assert signature == ""


def test_sign_submission_uses_real_wallet_hotkey(monkeypatch):
    # With a wallet present, sign with ITS hotkey and return that ss58 —
    # exactly the CLI path (minotaur_subnet.miner.main).
    signed: dict = {}

    class _FakeKeypair:
        ss58_address = "5FakeRegisteredHotkeyAddress"

        def sign(self, message: bytes) -> bytes:
            signed["message"] = message
            return b"rawsig-bytes"

    class _FakeWallet:
        def __init__(self, **kwargs):
            signed["wallet_kwargs"] = kwargs

        def get_hotkey(self):
            return _FakeKeypair()

    fake_mod = types.ModuleType("bittensor_wallet")
    fake_mod.Wallet = _FakeWallet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor_wallet", fake_mod)
    monkeypatch.setenv("MINER_WALLET_NAME", "my-registered-wallet")
    monkeypatch.delenv("MINER_HOTKEY_NAME", raising=False)

    hotkey, signature = _sign_submission("42:cafe:round-9", "miner-xyz")

    assert hotkey == "5FakeRegisteredHotkeyAddress"
    assert signature == base64.b64encode(b"rawsig-bytes").decode("ascii")
    assert signed["message"] == b"42:cafe:round-9"
    assert signed["wallet_kwargs"]["name"] == "my-registered-wallet"
