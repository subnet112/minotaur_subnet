"""P6a: round-anchored fork-pin SHADOW mode (observe-only, default-off).

Closes the rollout gap in ``round-anchored-fork-pin-spec.md`` §6 step 2: with the
real gate ``ROUND_ANCHORED_PIN`` off, the live path never derives a pin, so there
is no way to confirm every validator computes the identical pin *before* flipping
the gate. ``ROUND_ANCHOR_SHADOW`` derives + logs the pins (and the pack hash they
*would* produce) with **zero consensus effect**.

These tests pin the safety-critical properties: shadow only runs when explicitly
enabled and the real gate is off, it never stores ``fork_pins`` (so the real pack
hash is byte-for-byte unchanged), and the ``shadow_pin_segment`` builder override
computes the would-be hash without activating the gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.startup import (
    _build_solver_round_benchmark_pack_hash,
    _maybe_shadow_log_round_fork_pins,
)

_PINS = {8453: 46_904_887, 964: 5_012_345}


def _ctx():
    return SimpleNamespace(store=SimpleNamespace(list_apps=lambda: []))


def _patch_pack_builder():
    """Patch the heavy collectors so the real builder runs cheaply + deterministically."""
    return (
        patch(
            "minotaur_subnet.api.routes.submissions.get_store",
            return_value=SimpleNamespace(list_by_round=lambda _rid: []),
        ),
        patch(
            "minotaur_subnet.harness.benchmark_pack.collect_synthetic_scenarios",
            return_value=[],
        ),
        patch(
            "minotaur_subnet.harness.order_sampler.sample_historical_orders",
            return_value=[],
        ),
    )


# ── gate matrix ───────────────────────────────────────────────────────────────


def test_shadow_noop_when_shadow_flag_off(monkeypatch):
    monkeypatch.delenv("ROUND_ANCHOR_SHADOW", raising=False)
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    with patch("minotaur_subnet.api.startup._derive_round_fork_pins") as derive:
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="leader", anchor_epoch=42)
    derive.assert_not_called()


def test_shadow_noop_when_real_gate_on(monkeypatch):
    # Live path already derives/binds/logs — shadow must stay out of the way.
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "1")
    with patch("minotaur_subnet.api.startup._derive_round_fork_pins") as derive:
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="leader", anchor_epoch=42)
    derive.assert_not_called()


def test_shadow_derives_and_logs_when_enabled(monkeypatch, caplog):
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    p_store, p_syn, p_hist = _patch_pack_builder()
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=dict(_PINS)
    ) as derive, p_store, p_syn, p_hist, caplog.at_level("INFO"):
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="leader", anchor_epoch=42)
    derive.assert_called_once_with(42)
    line = "\n".join(caplog.messages)
    assert "[round-anchor-shadow]" in line
    assert "role=leader" in line
    assert "964:5012345|8453:46904887" in line  # sorted, deterministic segment


def test_shadow_never_stores_pins(monkeypatch):
    # The whole point: shadow must NOT touch RoundState.fork_pins (which would
    # feed the real pack hash). It must never call set_round_fork_pins.
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    store = SimpleNamespace(
        get_round=lambda _rid: SimpleNamespace(close_epoch=42),
        set_round_fork_pins=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("shadow must never store pins")
        ),
    )
    p_store, p_syn, p_hist = _patch_pack_builder()
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=dict(_PINS)
    ), patch(
        "minotaur_subnet.api.routes.submissions.get_round_store", return_value=store
    ), p_store, p_syn, p_hist:
        # anchor_epoch=None forces the round-store resolution path (follower).
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="follower")


def test_shadow_resolves_anchor_from_round_store_when_not_passed(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    store = SimpleNamespace(get_round=lambda _rid: SimpleNamespace(close_epoch=99))
    p_store, p_syn, p_hist = _patch_pack_builder()
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=dict(_PINS)
    ) as derive, patch(
        "minotaur_subnet.api.routes.submissions.get_round_store", return_value=store
    ), p_store, p_syn, p_hist:
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="follower")
    derive.assert_called_once_with(99)


def test_shadow_logs_deferred_when_pins_unavailable(monkeypatch, caplog):
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins", return_value=None
    ), patch(
        "minotaur_subnet.api.startup._build_solver_round_benchmark_pack_hash"
    ) as builder, caplog.at_level("INFO"):
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="leader", anchor_epoch=42)
    builder.assert_not_called()  # no pins → nothing to hash
    assert "pins=deferred" in "\n".join(caplog.messages)


def test_shadow_swallows_errors(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHOR_SHADOW", "1")
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    with patch(
        "minotaur_subnet.api.startup._derive_round_fork_pins",
        side_effect=RuntimeError("rpc down"),
    ):
        # Must not raise — a shadow log can never perturb the round.
        _maybe_shadow_log_round_fork_pins(_ctx(), "round-x", role="leader", anchor_epoch=42)


# ── builder override: would-be hash without activating the gate ─────────────────


def test_pack_builder_shadow_override_changes_hash_without_gate(monkeypatch):
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate OFF (default is on)
    p_store, p_syn, p_hist = _patch_pack_builder()
    with p_store, p_syn, p_hist:
        ctx = _ctx()
        actual = _build_solver_round_benchmark_pack_hash(ctx, "round-x")
        would_be = _build_solver_round_benchmark_pack_hash(
            ctx, "round-x", shadow_pin_segment="964:5012345|8453:46904887"
        )
        # Empty override == no override (both omit fork_pins) → unchanged hash.
        empty_override = _build_solver_round_benchmark_pack_hash(
            ctx, "round-x", shadow_pin_segment=""
        )
    assert actual.startswith("0x") and would_be.startswith("0x")
    assert would_be != actual  # the would-be hash reflects the bound pins
    assert empty_override == actual  # gate-off behavior is byte-for-byte unchanged


def test_pack_builder_default_unchanged_when_gate_off(monkeypatch):
    # Sanity: with the gate off and no override, the builder never folds pins in,
    # so shadow logging (which only ever passes an override to a *throwaway* call)
    # cannot move the real pack hash.
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")  # gate off (default is on)
    p_store, p_syn, p_hist = _patch_pack_builder()
    with p_store, p_syn, p_hist:
        ctx = _ctx()
        h1 = _build_solver_round_benchmark_pack_hash(ctx, "round-x")
        h2 = _build_solver_round_benchmark_pack_hash(ctx, "round-x")
    assert h1 == h2
