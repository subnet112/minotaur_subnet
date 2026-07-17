"""Tests for throne-time accrual (time-weighted emission Phase 0, observe-only)."""

from __future__ import annotations

import pytest

from minotaur_subnet.epoch.reign_attribution import (
    MAX_SAMPLE_GAP_EPOCHS,
    ThroneTimeAccumulator,
    ThroneTimeAttribution,
    build_time_weighted_mapping,
)

OWNER = "5OwnerBurnHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
A = "5MinerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
B = "5MinerBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
C = "5MinerCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


def _feed(acc: ThroneTimeAccumulator, samples, *, tempo_index=1, max_gap_epochs=None):
    """Drive the accumulator through a sequence of (epoch, hotkey) samples."""
    for epoch, hotkey in samples:
        acc.sample(
            now_epoch=epoch,
            tempo_index=tempo_index,
            champion_hotkey=hotkey,
            max_gap_epochs=max_gap_epochs,
        )


# ── accrual by sampling ──────────────────────────────────────────────────────


def test_first_sample_anchors_without_crediting():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A)])
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {}
    assert attr.unattributed_epochs == 0


def test_single_champion_accrues_over_ticks():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (172, A)])  # anchor at 100, +72 to A
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {A: 72}
    assert attr.unattributed_epochs == 0


def test_two_champions_split_by_time():
    # A holds [100,148)=48, B holds [148,172)=24 → 2:1.
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (148, A), (172, B)])
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {A: 48, B: 24}
    assert attr.unattributed_epochs == 0


def test_short_reign_is_not_zeroed():
    # B holds only 6 epochs mid-tempo before C dethrones it. Under winner-take-all
    # B earns zero; here it accrues its 6 epochs.
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (150, A), (156, B), (172, C)])
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {A: 50, B: 6, C: 16}
    assert attr.per_hotkey_epochs[B] > 0


def test_same_epoch_samples_are_idempotent():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (100, A), (100, A), (105, A)])  # only +5 credited
    assert acc.settle().per_hotkey_epochs == {A: 5}


def test_no_champion_span_is_unattributed():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, None), (110, None), (120, A)])  # [100,120) no champion, [110? ] ...
    attr = acc.settle()
    # [100,110) None → unattributed 10; [110,120) credited to A (sampled with A).
    assert attr.per_hotkey_epochs == {A: 10}
    assert attr.unattributed_epochs == 10


def test_same_hotkey_reclaims_throne_epochs_sum():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (120, A), (140, B), (172, A)])
    attr = acc.settle()
    assert attr.per_hotkey_epochs[A] == 20 + 32  # [100,120) + [140,172)
    assert attr.per_hotkey_epochs[B] == 20


def test_tempo_rollover_resets():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (150, A)], tempo_index=1)  # +50 A in tempo 1
    # New tempo: reset, anchor at 10, then +30 to B.
    acc.sample(now_epoch=10, tempo_index=2, champion_hotkey=B)
    acc.sample(now_epoch=40, tempo_index=2, champion_hotkey=B)
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {B: 30}  # A gone with the tempo


def test_normal_small_span_is_not_capped():
    # Ticks are finer than epochs, so normal spans (0–1 epochs) are never capped.
    acc = ThroneTimeAccumulator()
    acc.sample(now_epoch=100, tempo_index=1, champion_hotkey=A, max_gap_epochs=MAX_SAMPLE_GAP_EPOCHS)
    acc.sample(now_epoch=101, tempo_index=1, champion_hotkey=A, max_gap_epochs=MAX_SAMPLE_GAP_EPOCHS)
    assert acc.settle().per_hotkey_epochs == {A: 1}


def test_downtime_gap_is_capped_to_owner():
    # A coordinator STALL leaves a wide gap between samples within one tempo
    # (epochs 100 and 130 are both tempo 1 = epoch//72): only the cap is credited
    # to the current champion; the un-observed remainder folds into the owner
    # residual, never blindly credited to whoever is champion on recovery.
    acc = ThroneTimeAccumulator()
    acc.sample(now_epoch=100, tempo_index=1, champion_hotkey=A, max_gap_epochs=MAX_SAMPLE_GAP_EPOCHS)
    acc.sample(now_epoch=130, tempo_index=1, champion_hotkey=A, max_gap_epochs=MAX_SAMPLE_GAP_EPOCHS)
    attr = acc.settle()
    assert attr.per_hotkey_epochs == {A: MAX_SAMPLE_GAP_EPOCHS}
    assert attr.unattributed_epochs == 30 - MAX_SAMPLE_GAP_EPOCHS


def test_settle_applies_minimum_reign_floor():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (168, A), (172, B)])  # A:68, B:4
    attr = acc.settle(min_reign_epochs=8)
    assert B not in attr.per_hotkey_epochs
    assert attr.per_hotkey_epochs == {A: 68}
    assert attr.unattributed_epochs == 4


def test_settle_does_not_reset():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, A), (150, A)])
    assert acc.settle().per_hotkey_epochs == {A: 50}
    # A second settle without further samples returns the same snapshot.
    assert acc.settle().per_hotkey_epochs == {A: 50}


def test_window_epochs_is_sum_invariant():
    acc = ThroneTimeAccumulator()
    _feed(acc, [(100, None), (110, A), (150, B)])
    attr = acc.settle()
    assert attr.window_epochs == sum(attr.per_hotkey_epochs.values()) + attr.unattributed_epochs


# ── determinism ──────────────────────────────────────────────────────────────


def test_identical_sample_sequences_produce_identical_attribution():
    seq = [(100, A), (120, B), (150, C), (172, A)]
    a1, a2 = ThroneTimeAccumulator(), ThroneTimeAccumulator()
    _feed(a1, seq)
    _feed(a2, seq)
    assert a1.settle().per_hotkey_epochs == a2.settle().per_hotkey_epochs


def test_mapping_is_byte_identical_for_identical_accrual():
    seq = [(100, A), (120, B), (150, C), (172, A)]
    a1, a2 = ThroneTimeAccumulator(), ThroneTimeAccumulator()
    _feed(a1, seq)
    _feed(a2, seq)
    m1 = build_time_weighted_mapping(a1.settle(), owner_hotkey=OWNER, miner_fraction=0.75)
    m2 = build_time_weighted_mapping(a2.settle(), owner_hotkey=OWNER, miner_fraction=0.75)
    assert m1 == m2  # exact float equality — same ops, same (sorted) order


# ── build_time_weighted_mapping ──────────────────────────────────────────────


def test_mapping_full_coverage_sums_to_one_and_burns_correctly():
    attr = ThroneTimeAttribution(per_hotkey_epochs={A: 48, B: 24}, unattributed_epochs=0)
    m = build_time_weighted_mapping(attr, owner_hotkey=OWNER, miner_fraction=0.75)
    assert m[OWNER] == pytest.approx(0.25)  # full coverage → owner base 25%
    assert m[A] == pytest.approx(0.50)  # 2/3 of 0.75
    assert m[B] == pytest.approx(0.25)  # 1/3 of 0.75
    assert sum(m.values()) == pytest.approx(1.0)


def test_gaps_shrink_the_miner_pool_toward_owner():
    # Champion present only half the tempo → miners get half of 0.75, owner the rest.
    attr = ThroneTimeAttribution(per_hotkey_epochs={A: 36}, unattributed_epochs=36)
    m = build_time_weighted_mapping(attr, owner_hotkey=OWNER, miner_fraction=0.75)
    assert m[A] == pytest.approx(0.75 * 36 / 72)  # 0.375
    assert m[OWNER] == pytest.approx(1.0 - 0.375)
    assert sum(m.values()) == pytest.approx(1.0)


def test_mapping_no_miner_routes_all_to_owner():
    attr = ThroneTimeAttribution(per_hotkey_epochs={}, unattributed_epochs=72)
    assert build_time_weighted_mapping(attr, owner_hotkey=OWNER, miner_fraction=0.75) == {OWNER: 1.0}


def test_mapping_empty_window_routes_all_to_owner():
    attr = ThroneTimeAttribution(per_hotkey_epochs={}, unattributed_epochs=0)
    assert build_time_weighted_mapping(attr, owner_hotkey=OWNER, miner_fraction=0.75) == {OWNER: 1.0}


def test_single_champion_matches_winner_take_all():
    # A stable champion over a fully-covered tempo reproduces the existing
    # single-champion split exactly — the time-weighted path is a superset.
    attr = ThroneTimeAttribution(per_hotkey_epochs={A: 72}, unattributed_epochs=0)
    m = build_time_weighted_mapping(attr, owner_hotkey=OWNER, miner_fraction=0.75)
    assert m[A] == pytest.approx(0.75)
    assert m[OWNER] == pytest.approx(0.25)
