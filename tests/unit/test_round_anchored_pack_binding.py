"""P1: bind round-anchored fork pins into benchmark_pack_hash (gated, default-off).

The binding rides on the existing signed + recomputed-by-followers
``benchmark_pack_hash`` (no ChampionRegistry redeploy). These tests pin the two
properties that make the rollout safe: with ``ROUND_ANCHORED_PIN`` off the pack
hash is byte-for-byte unchanged, and when on the pins enter the preimage
deterministically so a divergent pin surfaces as PACK_HASH_MISMATCH.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.startup import _round_anchored_pin_segment
from minotaur_subnet.harness.round_store import RoundState, RoundStatus


def _round_with_pins(pins):
    return RoundState(round_id="round-x", status=RoundStatus.CLOSED, fork_pins=pins)


def _patch_round(round_state):
    store = SimpleNamespace(get_round=lambda _rid: round_state)
    return patch(
        "minotaur_subnet.api.routes.submissions.get_round_store",
        return_value=store,
    )


# ── gate / segment behavior ───────────────────────────────────────────────────


def test_segment_empty_when_gate_off_even_with_pins(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # emergency override -> off
    with _patch_round(_round_with_pins({8453: 46_904_887, 964: 5_012_345})):
        assert _round_anchored_pin_segment("round-x") == ""


def test_segment_empty_when_gate_on_but_no_pins(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with _patch_round(_round_with_pins(None)):
        assert _round_anchored_pin_segment("round-x") == ""


def test_segment_serialized_when_gate_on_with_pins(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with _patch_round(_round_with_pins({8453: 46_904_887, 964: 5_012_345})):
        # Sorted by chain_id, deterministic across nodes.
        assert _round_anchored_pin_segment("round-x") == "964:5012345|8453:46904887"


def test_segment_empty_when_round_missing(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with _patch_round(None):
        assert _round_anchored_pin_segment("round-x") == ""


def test_segment_empty_when_lookup_raises(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")

    def _boom():
        raise RuntimeError("store down")

    store = SimpleNamespace(get_round=lambda _rid: (_ for _ in ()).throw(RuntimeError("boom")))
    with patch(
        "minotaur_subnet.api.routes.submissions.get_round_store",
        return_value=store,
    ):
        assert _round_anchored_pin_segment("round-x") == ""


# ── RoundState serde round-trip ───────────────────────────────────────────────


def test_roundstate_fork_pins_serde_roundtrip():
    rs = _round_with_pins({8453: 46_904_887, 964: 5_012_345})
    restored = RoundState.from_dict(rs.to_dict())
    # int keys preserved (string keys would break serialize_fork_pins ordering).
    assert restored.fork_pins == {8453: 46_904_887, 964: 5_012_345}
    assert all(isinstance(k, int) for k in restored.fork_pins)


def test_roundstate_fork_pins_none_serde_roundtrip():
    rs = _round_with_pins(None)
    restored = RoundState.from_dict(rs.to_dict())
    assert restored.fork_pins is None
    assert rs.to_dict()["fork_pins"] is None
