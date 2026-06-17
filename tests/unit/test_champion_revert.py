"""Champion revert-to-previous (emergency rollback) + dethrone-margin tests.

Covers the kill-switch added for safe champion adoption: a one-step undo that
rolls the live champion back to the one active immediately before the current
one (NOT genesis), and the 5% dethrone margin (single-sourced).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.epoch.manager import EpochManager, DETHRONE_MARGIN
from minotaur_subnet.harness.round_store import ChampionSnapshot, RoundStore

# Reuse the established EpochManager test fixtures.
from tests.unit.test_epoch_manager import (  # noqa: E402
    _make_submission,
    _make_store_with_subs,
    _make_mock_block_loop,
    _make_mock_benchmark_worker,
)


# ── dethrone margin ──────────────────────────────────────────────────────────


def test_dethrone_margin_is_five_percent_single_source():
    # The single source of truth — every consumer imports this constant.
    assert DETHRONE_MARGIN == 0.05


# ── round_store previous-champion persistence ────────────────────────────────


def test_round_store_persists_previous_champion(tmp_path):
    path = tmp_path / "rounds.json"
    store = RoundStore(persist_path=path)
    snap = ChampionSnapshot(submission_id="sub_prev", solver_name="prev", activated_epoch=3)
    store.set_previous_champion(snap)

    # A fresh store loading the same file recovers the rollback target.
    reloaded = RoundStore(persist_path=path)
    assert reloaded.get_previous_champion().submission_id == "sub_prev"
    # Backward-compat: a store without the field loads as an empty snapshot.
    assert not RoundStore().get_previous_champion().submission_id


# ── manager: capture previous on adoption + revert ───────────────────────────


def _mgr_with_subs(*subs):
    async def runtime_builder(submission, epoch):
        return MagicMock(name=f"runtime:{submission.submission_id}")

    return EpochManager(
        block_loop=_make_mock_block_loop(),
        benchmark_worker=_make_mock_benchmark_worker(),
        submission_store=_make_store_with_subs(*subs),
        round_store=RoundStore(),
        runtime_builder=runtime_builder,
    )


@pytest.mark.asyncio
async def test_hot_swap_records_previous_champion_on_change():
    a = _make_submission(submission_id="sub_a", epoch=1, solver_name="a")
    b = _make_submission(submission_id="sub_b", epoch=2, solver_name="b")
    mgr = _mgr_with_subs(a, b)

    await mgr._hot_swap(a, 1)
    assert mgr.champion.submission_id == "sub_a"
    # First champion — nothing displaced yet.
    assert not mgr._round_store.get_previous_champion().submission_id

    await mgr._hot_swap(b, 2)
    assert mgr.champion.submission_id == "sub_b"
    # The displaced champion is now the rollback target.
    assert mgr._round_store.get_previous_champion().submission_id == "sub_a"


@pytest.mark.asyncio
async def test_revert_rolls_back_to_previous_champion():
    a = _make_submission(submission_id="sub_a", epoch=1, solver_name="a", image_tag="solver:a")
    b = _make_submission(submission_id="sub_b", epoch=2, solver_name="b", image_tag="solver:b")
    mgr = _mgr_with_subs(a, b)
    await mgr._hot_swap(a, 1)
    await mgr._hot_swap(b, 2)

    result = await mgr.revert_to_previous_champion(reason="b misbehaved")

    assert result["reverted"] is True
    assert result["from_submission_id"] == "sub_b"
    assert result["to_submission_id"] == "sub_a"
    assert result["to_image_tag"] == "solver:a"
    # Live champion + active-champion snapshot both rolled back to A.
    assert mgr.champion.submission_id == "sub_a"
    assert mgr._round_store.get_active_champion().submission_id == "sub_a"
    # The live block-loop solver was swapped (the revert is not metadata-only).
    assert mgr._block_loop.set_solver.called


@pytest.mark.asyncio
async def test_revert_bypasses_adoption_gate(monkeypatch):
    # The kill switch must work even with the safety gate ON — reverting to an
    # already-vetted prior champion is always safe.
    a = _make_submission(submission_id="sub_a", epoch=1, solver_name="a")
    b = _make_submission(submission_id="sub_b", epoch=2, solver_name="b")
    mgr = _mgr_with_subs(a, b)
    await mgr._hot_swap(a, 1)
    await mgr._hot_swap(b, 2)

    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")
    await mgr.revert_to_previous_champion()
    assert mgr.champion.submission_id == "sub_a"


@pytest.mark.asyncio
async def test_revert_errors_when_no_previous_champion():
    a = _make_submission(submission_id="sub_a", epoch=1, solver_name="a")
    mgr = _mgr_with_subs(a)
    await mgr._hot_swap(a, 1)  # only one champion ever — nothing to revert to
    with pytest.raises(ValueError, match="no previous champion"):
        await mgr.revert_to_previous_champion()


@pytest.mark.asyncio
async def test_revert_is_not_a_redo_loop():
    # After a revert, reverting again must error (the target is already active),
    # so an operator can't ping-pong back to the bad champion.
    a = _make_submission(submission_id="sub_a", epoch=1, solver_name="a")
    b = _make_submission(submission_id="sub_b", epoch=2, solver_name="b")
    mgr = _mgr_with_subs(a, b)
    await mgr._hot_swap(a, 1)
    await mgr._hot_swap(b, 2)
    await mgr.revert_to_previous_champion()  # -> A
    with pytest.raises(ValueError, match="already active"):
        await mgr.revert_to_previous_champion()
