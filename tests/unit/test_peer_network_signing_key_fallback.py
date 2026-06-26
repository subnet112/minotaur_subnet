"""ValidatorPeerNetwork.private_key resolution + env fallback.

Regression guard for the recreate-vs-restart keyless-instance bug: a freshly
recreated api container reached sign-time with an empty champion-network key,
so lifecycle broadcasts went out unsigned and followers 401'd. The network's
``private_key`` now falls back to the VALIDATOR_PRIVATE_KEY env (set from the
compose substitution on every boot) so signing is never silently dropped.
"""
from __future__ import annotations

import logging

from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork


def _net(private_key: str) -> ValidatorPeerNetwork:
    return ValidatorPeerNetwork(
        validator_id="0xabcdef0123456789",
        private_key=private_key,
        consensus=None,
    )


def test_construction_key_wins(monkeypatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0xENVKEY")
    assert _net("0xCTORKEY").private_key == "0xCTORKEY"


def test_empty_construction_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0xENVKEY")
    assert _net("").private_key == "0xENVKEY"


def test_empty_everywhere_returns_empty(monkeypatch):
    monkeypatch.delenv("VALIDATOR_PRIVATE_KEY", raising=False)
    # No key anywhere -> empty string, so callers skip signing rather than crash.
    assert _net("").private_key == ""


def test_fallback_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0xENVKEY")
    net = _net("")
    with caplog.at_level(logging.WARNING):
        assert net.private_key == "0xENVKEY"
        assert net.private_key == "0xENVKEY"
    warnings = [r for r in caplog.records if "keyless-instance" in r.getMessage()]
    assert len(warnings) == 1
