"""Real consensus mode with no signing key must crash at boot, not limp.

2026-07-02: a compose recreate without ``--env-file .env.keys`` interpolated
an empty VALIDATOR_PRIVATE_KEY. The api came up "healthy", both consensus
managers silently stayed None, and a certified-ready dethrone stalled ~50
minutes behind a "Cannot certify: champion consensus manager is None" warning
every 5s. The startup guard turns that misconfiguration into an immediate
boot failure (visible crash-loop under any restart policy) unless the
CONSENSUS_KEY_FAIL_FAST=0 break-glass is set.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.api.startup import _require_real_consensus_signing_key


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "VALIDATOR_PRIVATE_KEY",
        "VALIDATOR_PRIVATE_KEYS",
        "CONSENSUS_KEY_FAIL_FAST",
    ):
        monkeypatch.delenv(var, raising=False)


def test_real_mode_without_key_raises():
    with pytest.raises(RuntimeError, match="env-file"):
        _require_real_consensus_signing_key("real")


def test_real_mode_with_empty_key_raises(monkeypatch: pytest.MonkeyPatch):
    # The incident shape: the variable EXISTS but interpolated to "".
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "")
    with pytest.raises(RuntimeError):
        _require_real_consensus_signing_key("real")


def test_real_mode_with_key_passes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0x" + "11" * 32)
    _require_real_consensus_signing_key("real")


def test_deprecated_plural_keys_still_satisfy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEYS", "0x" + "11" * 32)
    _require_real_consensus_signing_key("real")


def test_local_mode_never_raises():
    _require_real_consensus_signing_key("local")


def test_break_glass_downgrades_to_warning(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONSENSUS_KEY_FAIL_FAST", "0")
    _require_real_consensus_signing_key("real")  # must not raise
