"""Unit tests for the restart-drift champion boot-restore.

Covers `_restore_persisted_champion_solver`: at boot we must rebuild the live
solver from the persisted active champion instead of silently reverting to the
genesis image, with safe fallbacks on every failure path.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.api.startup import _restore_persisted_champion_solver


def _run(coro):
    return asyncio.run(coro)


def _champion(submission_id="sub_1", image_id="sha256:abc", hotkey="5Gminer", epoch=3):
    return SimpleNamespace(
        submission_id=submission_id,
        image_id=image_id,
        hotkey=hotkey,
        activated_epoch=epoch,
    )


def test_restore_noop_when_hot_swap_disabled():
    round_store = MagicMock()
    block_loop = MagicMock()

    async def build(sub, epoch):  # must never run
        raise AssertionError("build_live_solver must not run when hot-swap disabled")

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=MagicMock(), block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=False,
    ))
    assert out is None
    round_store.get_active_champion.assert_not_called()
    block_loop.set_solver.assert_not_called()


def test_restore_noop_when_no_champion():
    round_store = MagicMock()
    round_store.get_active_champion.return_value = _champion(submission_id=None)
    block_loop = MagicMock()
    called = []

    async def build(sub, epoch):
        called.append(True)
        return object()

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=MagicMock(), block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=True,
    ))
    assert out is None
    assert called == []  # builder never invoked when there is no champion
    block_loop.set_solver.assert_not_called()


def test_restore_builds_and_sets_solver_preferring_real_submission():
    champ = _champion()
    round_store = MagicMock()
    round_store.get_active_champion.return_value = champ
    real_sub = SimpleNamespace(submission_id="sub_1", image_id="sha256:abc", hotkey="5Gminer")
    sub_store = MagicMock()
    sub_store.get.return_value = real_sub
    block_loop = MagicMock()
    solver = object()
    seen = {}

    async def build(sub, epoch):
        seen["sub"] = sub
        seen["epoch"] = epoch
        return solver

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=sub_store, block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=True,
    ))
    assert out is solver
    block_loop.set_solver.assert_called_once_with(solver)
    assert seen["sub"] is real_sub        # prefers the real submission row
    assert seen["epoch"] == 3             # uses the champion's activated_epoch


def test_restore_falls_back_to_snapshot_when_submission_missing():
    champ = _champion()
    round_store = MagicMock()
    round_store.get_active_champion.return_value = champ
    sub_store = MagicMock()
    sub_store.get.return_value = None      # pruned from the store
    block_loop = MagicMock()
    solver = object()
    seen = {}

    async def build(sub, epoch):
        seen["sub"] = sub
        return solver

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=sub_store, block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=True,
    ))
    assert out is solver
    assert seen["sub"] is champ           # fell back to the snapshot (has image_id+hotkey)
    block_loop.set_solver.assert_called_once_with(solver)


def test_restore_keeps_genesis_when_build_returns_none():
    champ = _champion()
    round_store = MagicMock()
    round_store.get_active_champion.return_value = champ
    sub_store = MagicMock()
    sub_store.get.return_value = None
    block_loop = MagicMock()

    async def build(sub, epoch):
        return None  # image unavailable / hot-swap disabled inside the builder

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=sub_store, block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=True,
    ))
    assert out is None
    block_loop.set_solver.assert_not_called()


def test_restore_swallows_build_exception():
    champ = _champion()
    round_store = MagicMock()
    round_store.get_active_champion.return_value = champ
    sub_store = MagicMock()
    sub_store.get.return_value = None
    block_loop = MagicMock()

    async def build(sub, epoch):
        raise RuntimeError("docker image gone")

    out = _run(_restore_persisted_champion_solver(
        round_store=round_store, sub_store=sub_store, block_loop=block_loop,
        build_live_solver=build, allow_hot_swap=True,
    ))
    assert out is None                     # exception caught -> genesis kept
    block_loop.set_solver.assert_not_called()
