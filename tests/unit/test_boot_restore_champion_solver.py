"""Boot-restore: relaunch the live ORDER solver onto the adopted champion on restart.

Regression for the silent split where, after an api restart, ``/solver/champion``
and the weights report the adopted champion, but the live ORDER solver stays on
the genesis / ``FORCE_SOLVER_IMAGE`` boot image — which served a real multi-hop
order with stale SwapRouter calldata that reverted (score 0 → order rejected).

``EpochManager`` restores the champion METADATA at boot; these tests pin that
``ensure_live_solver_matches_champion()`` actually relaunches the live solver to
the certified champion (and fails loud / no-ops correctly otherwise).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.round_store import RoundStore
from minotaur_subnet.weight_policy import GENESIS_HOTKEY

# Reuse the established EpochManager test fixtures.
from tests.unit.test_epoch_manager import (  # noqa: E402
    _make_submission,
    _make_store_with_subs,
    _make_mock_block_loop,
    _make_mock_benchmark_worker,
)


def _manager(store, rounds, *, block_loop, runtime_builder):
    """A fresh EpochManager over already-persisted stores == an api restart."""
    return EpochManager(
        block_loop=block_loop,
        benchmark_worker=_make_mock_benchmark_worker(),
        submission_store=store,
        round_store=rounds,
        runtime_builder=runtime_builder,
    )


@pytest.mark.asyncio
async def test_boot_restore_relaunches_live_solver_onto_champion():
    champ = _make_submission(
        submission_id="sub_x", epoch=1, solver_name="x",
        image_tag="solver:x", hotkey="5Miner",
    )
    store, rounds = _make_store_with_subs(champ), RoundStore()

    # Adopt the champion on a first manager (sets ADOPTED + active-champion snapshot).
    adopt_mgr = _manager(
        store, rounds, block_loop=_make_mock_block_loop(),
        runtime_builder=lambda s, e: MagicMock(),
    )
    await adopt_mgr._hot_swap(champ, 1)

    # Restart: a fresh manager + fresh block loop over the same persisted stores.
    bl = _make_mock_block_loop()
    built = MagicMock(name="champion-runtime")

    async def builder(sub, epoch):
        assert sub.submission_id == "sub_x"  # builds the restored champion
        return built

    mgr = _manager(store, rounds, block_loop=bl, runtime_builder=builder)

    # Boot restores champion METADATA but must not have swapped the live solver yet.
    assert mgr.champion.submission_id == "sub_x"
    assert not bl.set_solver.called

    assert await mgr.ensure_live_solver_matches_champion() is True
    bl.set_solver.assert_called_once_with(built)


@pytest.mark.asyncio
async def test_boot_restore_noop_when_builder_declines():
    # FORCE_SOLVER_IMAGE / hot-swap-disabled → builder returns None → keep boot solver.
    champ = _make_submission(submission_id="sub_x", epoch=1, hotkey="5Miner")
    store, rounds = _make_store_with_subs(champ), RoundStore()
    adopt_mgr = _manager(
        store, rounds, block_loop=_make_mock_block_loop(),
        runtime_builder=lambda s, e: MagicMock(),
    )
    await adopt_mgr._hot_swap(champ, 1)

    bl = _make_mock_block_loop()

    async def declines(sub, epoch):
        return None

    mgr = _manager(store, rounds, block_loop=bl, runtime_builder=declines)
    assert await mgr.ensure_live_solver_matches_champion() is False
    assert not bl.set_solver.called


@pytest.mark.asyncio
async def test_boot_restore_fails_loud_not_silent_on_builder_error():
    # A failed champion build must NOT silently swap to a wrong solver, and must
    # not raise out of boot (fail loud via log, return False).
    champ = _make_submission(submission_id="sub_x", epoch=1, hotkey="5Miner")
    store, rounds = _make_store_with_subs(champ), RoundStore()
    adopt_mgr = _manager(
        store, rounds, block_loop=_make_mock_block_loop(),
        runtime_builder=lambda s, e: MagicMock(),
    )
    await adopt_mgr._hot_swap(champ, 1)

    bl = _make_mock_block_loop()

    async def boom(sub, epoch):
        raise RuntimeError("ghcr pull failed")

    mgr = _manager(store, rounds, block_loop=bl, runtime_builder=boom)
    assert await mgr.ensure_live_solver_matches_champion() is False
    assert not bl.set_solver.called


@pytest.mark.asyncio
async def test_boot_restore_skips_genesis_incumbent():
    # A genesis incumbent (burn hotkey) must NOT trigger a champion relaunch —
    # the genesis boot solver is already the correct live solver.
    champ = _make_submission(submission_id="sub_x", epoch=1, hotkey="5Miner")
    store, rounds = _make_store_with_subs(champ), RoundStore()
    bl = _make_mock_block_loop()
    built_calls = {"n": 0}

    async def builder(sub, epoch):
        built_calls["n"] += 1
        return MagicMock()

    mgr = _manager(store, rounds, block_loop=bl, runtime_builder=builder)
    # Force the restored champion to a genesis-hotkey submission.
    mgr._restored_champion_submission = _make_submission(
        submission_id="sub_g", epoch=0, hotkey=GENESIS_HOTKEY,
    )
    assert await mgr.ensure_live_solver_matches_champion() is False
    assert built_calls["n"] == 0
    assert not bl.set_solver.called


@pytest.mark.asyncio
async def test_boot_restore_noop_without_persisted_champion():
    # Fresh node, no champion ever adopted → nothing to restore.
    store, rounds = _make_store_with_subs(), RoundStore()
    bl = _make_mock_block_loop()

    async def builder(sub, epoch):  # pragma: no cover - must not be called
        raise AssertionError("builder must not run without a champion")

    mgr = _manager(store, rounds, block_loop=bl, runtime_builder=builder)
    assert await mgr.ensure_live_solver_matches_champion() is False
    assert not bl.set_solver.called
