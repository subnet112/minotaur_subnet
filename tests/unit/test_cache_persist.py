"""Tests for RPC-cache disk persistence (fork-cache + block-pin cache).

Persistence caches ONLY immutable data, so the invariants under test are:
a snapshot round-trips exactly, a restore honours the current size bounds, and
any bad/absent/foreign snapshot degrades to a cold start (never a crash, never
a wrong answer).
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from minotaur_subnet.harness.rpc_budget_proxy._persist import (
    SNAPSHOT_VERSION,
    SnapshotScheduler,
    load_snapshot,
    write_snapshot,
)
from minotaur_subnet.harness.rpc_budget_proxy.fork_cache import ForkCache
from minotaur_subnet.harness.rpc_budget_proxy.proxy import PinCache


# ── low-level snapshot file I/O ──────────────────────────────────────────────

def test_write_load_roundtrip(tmp_path):
    path = str(tmp_path / "snap.json")
    payload = {"a": 1, "b": ["x", "y"]}
    write_snapshot(path, payload)
    assert load_snapshot(path) == payload


def test_load_missing_is_none(tmp_path):
    assert load_snapshot(str(tmp_path / "nope.json")) is None


def test_load_empty_path_is_none():
    assert load_snapshot("") is None


def test_load_corrupt_is_none(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json")
    assert load_snapshot(str(path)) is None  # tolerated → cold start


def test_load_version_mismatch_is_none(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"v": SNAPSHOT_VERSION + 1, "payload": {"a": 1}}))
    assert load_snapshot(str(path)) is None


def test_write_is_atomic_no_temp_left(tmp_path):
    path = str(tmp_path / "snap.json")
    write_snapshot(path, {"a": 1})
    # only the final file remains — no stray .cache-*.tmp
    leftovers = [p for p in os.listdir(tmp_path) if p != "snap.json"]
    assert leftovers == []


def test_write_bad_payload_does_not_raise(tmp_path):
    path = str(tmp_path / "snap.json")
    write_snapshot(path, {"k": {1, 2, 3}})  # a set is not JSON-serializable
    assert load_snapshot(path) is None  # nothing written, no exception


# ── ForkCache snapshot / restore ─────────────────────────────────────────────

def _fork_cache(**kw) -> ForkCache:
    return ForkCache({"eth": "http://upstream.invalid"}, **kw)


def test_fork_cache_roundtrip():
    fc = _fork_cache()
    fc._put("eth:eth_getStorageAt:[\"0xA\",\"0x1\",\"0x2\"]", "0xdead")
    fc._put("eth:eth_getCode:[\"0xB\",\"0x2\"]", "0x6001")
    payload = fc._snapshot_payload()

    fc2 = _fork_cache()
    fc2._restore(payload)
    assert fc2._get("eth:eth_getStorageAt:[\"0xA\",\"0x1\",\"0x2\"]") == (True, "0xdead")
    assert fc2._get("eth:eth_getCode:[\"0xB\",\"0x2\"]") == (True, "0x6001")


def test_fork_cache_restore_honours_max_entries():
    fc = _fork_cache()
    for i in range(10):
        fc._put(f"eth:m:{i}", i)
    payload = fc._snapshot_payload()

    small = _fork_cache(max_entries=3)
    small._restore(payload)
    assert len(small._cache) == 3
    # keeps the most-recently-inserted (LRU tail): 7, 8, 9
    assert small._get("eth:m:9") == (True, 9)
    assert small._get("eth:m:0")[0] is False


def test_fork_cache_restore_ignores_garbage():
    fc = _fork_cache()
    fc._restore(["not", "a", "dict"])  # wrong shape → no-op, no raise
    assert fc._cache == {}


# ── PinCache snapshot / restore ──────────────────────────────────────────────

def _resp(result):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    return SimpleNamespace(status=200, body=body)


def test_pin_cache_roundtrip():
    pc = PinCache()
    k = PinCache.key("eth_getStorageAt", ["0xA", "0x1"])
    pc.put_from_response("base", "0x10", k, _resp("0xbeef"))
    payload = pc.snapshot()

    pc2 = PinCache()
    loaded = pc2.restore(payload)
    assert loaded == 1
    assert pc2.get("base", "0x10", k) == (True, "0xbeef")


def test_pin_cache_restore_honours_max_blocks():
    pc = PinCache(max_blocks=8)
    k = PinCache.key("eth_getBalance", ["0xA"])
    for b in range(5):
        pc.put_from_response("base", hex(b), k, _resp(f"0x{b}"))
    payload = pc.snapshot()

    small = PinCache(max_blocks=2)
    small.restore(payload)
    assert len(small._blocks) == 2
    # keeps the most-recent block groups (0x3, 0x4)
    assert small.get("base", "0x4", k) == (True, "0x4")
    assert small.get("base", "0x0", k)[0] is False


def test_pin_cache_restore_ignores_garbage():
    pc = PinCache()
    assert pc.restore({"not": "a list"}) == 0
    assert pc.restore([["too", "short"], 42, ["c", "b", "not-a-dict"]]) == 0
    assert pc._blocks == {}


# ── SnapshotScheduler ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduler_disabled_when_no_path():
    sched = SnapshotScheduler("", 300, lambda: {"a": 1})
    assert sched.enabled is False
    await sched.start()  # no-op
    await sched.flush()  # no-op
    await sched.stop()   # no final write (nowhere to write)


@pytest.mark.asyncio
async def test_scheduler_flush_and_final_snapshot(tmp_path):
    path = str(tmp_path / "snap.json")
    state = {"n": 1}
    sched = SnapshotScheduler(path, 300, lambda: dict(state))
    await sched.start()
    await sched.flush()
    assert load_snapshot(path) == {"n": 1}

    state["n"] = 2
    await sched.stop()  # stop() flushes a final snapshot
    assert load_snapshot(path) == {"n": 2}
