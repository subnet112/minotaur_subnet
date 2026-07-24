"""Regression guard: with SIM_OFFLOAD_TO_THREAD=1, AnvilSimulator.simulate()
must run the blocking web3 work OFF the asyncio event loop.

Incident (2026-07-24): the dedicated /quote anvils run with
``--no-storage-caching``, so each quote's ``scoreIntent`` does hundreds of
sequential upstream storage reads on a single-threaded anvil. ``_simulate_inner``
performed all of that **synchronously on the API event loop** (it was an
``async def`` with no awaits, so ``await self._simulate_inner(...)`` blocked the
loop for the whole snapshot->execute->revert window). One stalled quote froze
the ENTIRE api — health checks, the miner dashboard, and order processing all
went unresponsive. py-spy caught the MainThread parked in
``wait_for_transaction_receipt`` -> socket ``readinto``.

The socket timeout guard (#1052, see test_httpprovider_gets_socket_timeout) was
necessary but insufficient: even a bounded 30s stall freezes the loop for 30s if
it runs ON the loop. The fix offloads ``_simulate_inner`` to a worker thread via
``asyncio.to_thread`` under the existing ``_sim_lock`` (which still serializes
per-fork snapshot state) — gated by ``SIM_OFFLOAD_TO_THREAD`` (default OFF; see
``_sim_offload_enabled`` for the enabling gates). These tests pin the
flag-ENABLED behavior; the flag-off inline path is pinned by
test_benchmark_sim_determinism.py.

Ported from the superseded standalone offload PR (#1078), which shipped the
same to_thread offload unconditionally and without the fork-mutation locks.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator


def _make_sim() -> AnvilSimulator:
    """Construct an AnvilSimulator with Web3 fully mocked (no real anvil)."""
    with patch("minotaur_subnet.simulator.anvil_simulator.Web3") as MockWeb3:
        MockWeb3.HTTPProvider.return_value = MagicMock()
        MockWeb3.to_checksum_address = lambda x: x
        instance = MagicMock()
        instance.is_connected.return_value = True
        instance.eth.block_number = 100
        MockWeb3.return_value = instance
        return AnvilSimulator(
            rpc_url="http://anvil:8545",
            default_executor="0x" + "00" * 20,
        )


def test_simulate_inner_is_synchronous():
    """_simulate_inner MUST be a plain sync function so it can run in a worker
    thread. If it becomes a coroutine again, ``asyncio.to_thread`` would hand
    the executor an un-awaited coroutine object — the sim would silently no-op
    AND the loop-freeze would quietly return.
    """
    assert not asyncio.iscoroutinefunction(AnvilSimulator._simulate_inner), (
        "_simulate_inner must be sync (def, not async def) — the simulate() "
        "offload runs it via asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_simulate_does_not_block_the_event_loop(monkeypatch):
    """With the offload enabled, a blocking _simulate_inner must NOT freeze the
    event loop.

    We stub _simulate_inner with a synchronous ``time.sleep`` standing in for
    the real anvil round-trips, then run a 10ms heartbeat coroutine alongside
    ``simulate()``. If simulate() offloads correctly the heartbeat keeps
    ticking; if the blocking work runs on the loop the heartbeat is frozen and
    ``ticks`` stays ~0.
    """
    monkeypatch.setenv("SIM_OFFLOAD_TO_THREAD", "1")
    sim = _make_sim()

    BLOCK_SECONDS = 0.3
    ran = {"inner": False}

    def blocking_inner(*args, **kwargs):
        ran["inner"] = True
        time.sleep(BLOCK_SECONDS)  # the synchronous anvil/web3 round-trips
        return "sim-result"

    sim._simulate_inner = blocking_inner  # type: ignore[method-assign]

    ticks = {"n": 0}

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    hb = asyncio.create_task(heartbeat())
    try:
        result = await sim.simulate(MagicMock())  # plan arg is ignored by stub
    finally:
        hb.cancel()

    assert ran["inner"] is True
    assert result == "sim-result"
    # A responsive loop ticks ~30x during a 0.3s blocking sim. Require a
    # healthy margin (>=5) to stay robust on slow CI while still failing hard
    # if the loop froze (ticks would be 0-1).
    assert ticks["n"] >= 5, (
        f"event loop only advanced {ticks['n']}x during the {BLOCK_SECONDS}s "
        "blocking sim — it froze; simulate() is not offloading _simulate_inner"
    )


@pytest.mark.asyncio
async def test_simulate_serializes_under_the_sim_lock(monkeypatch):
    """The offload must preserve per-fork serialization: two concurrent
    simulate() calls on the same simulator must NOT overlap inside
    _simulate_inner (snapshot->execute->revert would corrupt otherwise).
    """
    monkeypatch.setenv("SIM_OFFLOAD_TO_THREAD", "1")
    sim = _make_sim()

    active = {"n": 0}
    max_concurrent = {"n": 0}

    def tracking_inner(*args, **kwargs):
        active["n"] += 1
        max_concurrent["n"] = max(max_concurrent["n"], active["n"])
        time.sleep(0.05)
        active["n"] -= 1
        return "ok"

    sim._simulate_inner = tracking_inner  # type: ignore[method-assign]

    await asyncio.gather(*(sim.simulate(MagicMock()) for _ in range(4)))

    assert max_concurrent["n"] == 1, (
        f"_simulate_inner ran {max_concurrent['n']} at once — the _sim_lock no "
        "longer serializes fork access across the to_thread offload"
    )
