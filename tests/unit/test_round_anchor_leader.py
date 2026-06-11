"""P2: leader-side derivation + storage of round-anchored fork pins (gated).

Covers the chain-set config parsing, the live-adapter derivation (web3 faked),
the gated populate-on-close behavior, and the RoundStore setter. The pure
determinism is tested in test_round_anchor.py; here we test the wiring stays
inert by default and reads the LIVE chain when on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.api import startup
from minotaur_subnet.api.startup import (
    _derive_round_fork_pins,
    _maybe_populate_round_fork_pins,
    _round_anchor_chains,
)
from minotaur_subnet.harness.round_store import RoundState, RoundStore


def _fake_web3(*, head: int, t0: int, spacing: int):
    """A minimal Web3 stand-in: block b has timestamp t0 + b*spacing."""

    class _Eth:
        block_number = head

        def get_block(self, b):
            return {"timestamp": t0 + b * spacing}

    class _W3:
        def __init__(self, provider=None):
            self.eth = _Eth()

        @staticmethod
        def HTTPProvider(url, **kwargs):
            return ("http", url)

    return _W3


# ── chain-set config ──────────────────────────────────────────────────────────


def test_round_anchor_chains_default_base(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)
    assert _round_anchor_chains() == [8453]


def test_round_anchor_chains_parses_and_skips_garbage(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHOR_CHAINS", "8453, 964 , x,")
    assert _round_anchor_chains() == [8453, 964]


# ── _derive_round_fork_pins (live adapter, web3 faked) ────────────────────────


def _patch_derive(fake_w3, *, epoch_seconds=60, rpc="http://stub"):
    return (
        patch(
            "minotaur_subnet.epoch.clock.SolverRoundEpochClock.from_env",
            return_value=SimpleNamespace(epoch_seconds=epoch_seconds),
        ),
        patch(
            "minotaur_subnet.consensus.app_registry_cache._chain_rpc_env",
            return_value=rpc,
        ),
        patch("web3.Web3", fake_w3),
    )


def test_derive_returns_canonical_pin(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)
    monkeypatch.delenv("ROUND_ANCHOR_CONFIRMATIONS", raising=False)
    # epoch 100 * 60 = anchor_ts 6000; Base ~2s blocks -> pin = 3000.
    fake = _fake_web3(head=10_000, t0=0, spacing=2)
    p1, p2, p3 = _patch_derive(fake)
    with p1, p2, p3:
        assert _derive_round_fork_pins(100) == {8453: 3000}


def test_derive_defers_when_anchor_in_future(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)
    # Anchor far past the chain's confirmed tip -> not bracketed -> None.
    fake = _fake_web3(head=100, t0=0, spacing=2)
    p1, p2, p3 = _patch_derive(fake)
    with p1, p2, p3:
        assert _derive_round_fork_pins(1000) is None


def test_derive_defers_when_no_live_rpc(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_CHAINS", raising=False)
    fake = _fake_web3(head=10_000, t0=0, spacing=2)
    p1, p2, p3 = _patch_derive(fake, rpc="")  # no RPC -> ForkPinUnavailable -> None
    with p1, p2, p3:
        assert _derive_round_fork_pins(100) is None


# ── _maybe_populate_round_fork_pins (gated leader hook) ───────────────────────


def test_populate_noop_when_gate_off(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    store = MagicMock()
    with patch("minotaur_subnet.api.routes.submissions.get_round_store", return_value=store):
        _maybe_populate_round_fork_pins("r1", 100)
    store.set_round_fork_pins.assert_not_called()


def test_populate_stores_pins_when_gate_on(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    store = MagicMock()
    with patch.object(startup, "_derive_round_fork_pins", return_value={8453: 3000}), \
         patch("minotaur_subnet.api.routes.submissions.get_round_store", return_value=store):
        _maybe_populate_round_fork_pins("r1", 100)
    store.set_round_fork_pins.assert_called_once_with("r1", {8453: 3000})


def test_populate_noop_when_derivation_defers(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    store = MagicMock()
    with patch.object(startup, "_derive_round_fork_pins", return_value=None), \
         patch("minotaur_subnet.api.routes.submissions.get_round_store", return_value=store):
        _maybe_populate_round_fork_pins("r1", 100)
    store.set_round_fork_pins.assert_not_called()


# ── RoundStore.set_round_fork_pins ────────────────────────────────────────────


def test_set_round_fork_pins_sets_and_clears():
    store = RoundStore()
    store._rounds["r1"] = RoundState(round_id="r1")

    out = store.set_round_fork_pins("r1", {8453: 3000, 964: 42})
    assert out.fork_pins == {8453: 3000, 964: 42}
    assert all(isinstance(k, int) for k in out.fork_pins)

    cleared = store.set_round_fork_pins("r1", None)
    assert cleared.fork_pins is None


def test_set_round_fork_pins_missing_round_raises():
    store = RoundStore()
    with pytest.raises(KeyError):
        store.set_round_fork_pins("nope", {8453: 1})
