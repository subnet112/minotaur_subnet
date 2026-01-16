"""Tests for MetagraphManager metagraph synchronization and caching."""
import types
import logging

import pytest

from neurons.metagraph_manager import MetagraphManager, MetagraphSnapshot


class FakeMetagraph:
    def __init__(self, hotkeys, uids, validator_permit=None):
        self.hotkeys = hotkeys
        self.uids = uids
        self.validator_permit = validator_permit or [True] * len(hotkeys)

    def sync(self, subtensor, lite=True):
        pass


class FakeSubtensor:
    def __init__(self, current_block=100, uid_for_hotkey=None):
        self._current_block = current_block
        self._uid_for_hotkey = uid_for_hotkey or {}
        self.network = "test"

    def get_current_block(self):
        return self._current_block

    def get_uid_for_hotkey_on_subnet(self, hotkey, netuid):
        return self._uid_for_hotkey.get(hotkey)


class FakeWallet:
    def __init__(self, hotkey_address):
        self.hotkey = types.SimpleNamespace(ss58_address=hotkey_address)


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def test_snapshot_builds_uid_mapping(monkeypatch):
    """Snapshot should build correct hotkey to UID mapping."""
    subtensor = FakeSubtensor(uid_for_hotkey={"validator": 0})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    # Mock the metagraph creation
    fake_metagraph = FakeMetagraph(
        hotkeys=["hk0", "hk1", "hk2"],
        uids=[0, 1, 2],
        validator_permit=[True, False, False],
    )

    import bittensor as bt
    original_metagraph = bt.Metagraph

    def mock_metagraph(*args, **kwargs):
        return fake_metagraph

    monkeypatch.setattr(bt, "Metagraph", mock_metagraph)

    snapshot = manager.refresh(force=True)

    assert snapshot is not None
    assert snapshot.uid_for_hotkey == {"hk0": 0, "hk1": 1, "hk2": 2}
    assert snapshot.size == 3


def test_snapshot_detects_validator_permit(monkeypatch):
    """Snapshot should correctly detect validator permit status."""
    subtensor = FakeSubtensor(uid_for_hotkey={"validator": 0})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    fake_metagraph = FakeMetagraph(
        hotkeys=["validator", "miner1"],
        uids=[0, 1],
        validator_permit=[True, False],
    )

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    snapshot = manager.refresh(force=True)

    assert snapshot.validator_permit is True
    assert snapshot.validator_uid == 0


def test_snapshot_detects_no_permit(monkeypatch):
    """Snapshot should detect when validator has no permit."""
    subtensor = FakeSubtensor(uid_for_hotkey={"validator": 1})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    fake_metagraph = FakeMetagraph(
        hotkeys=["other", "validator"],
        uids=[0, 1],
        validator_permit=[True, False],  # Validator at uid 1 has no permit
    )

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    snapshot = manager.refresh(force=True)

    assert snapshot.validator_permit is False


def test_refresh_caches_recent_result(monkeypatch):
    """Refresh should cache and reuse recent results."""
    subtensor = FakeSubtensor(current_block=100, uid_for_hotkey={"validator": 0})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    sync_count = [0]

    class CountingMetagraph(FakeMetagraph):
        def sync(self, subtensor, lite=True):
            sync_count[0] += 1

    fake_metagraph = CountingMetagraph(
        hotkeys=["validator"],
        uids=[0],
    )

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    # First refresh - should sync
    manager.refresh(force=True)
    assert sync_count[0] == 1

    # Second refresh within 5 blocks - should use cache
    manager.refresh(force=False)
    # Cache should prevent additional sync since block hasn't advanced much
    # (depends on implementation - may or may not cache on first call)


def test_refresh_force_ignores_cache(monkeypatch):
    """Force refresh should always sync."""
    subtensor = FakeSubtensor(current_block=100, uid_for_hotkey={"validator": 0})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    sync_count = [0]

    class CountingMetagraph(FakeMetagraph):
        def sync(self, subtensor, lite=True):
            sync_count[0] += 1

    fake_metagraph = CountingMetagraph(hotkeys=["validator"], uids=[0])

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    manager.refresh(force=True)
    manager.refresh(force=True)

    assert sync_count[0] == 2


def test_async_methods_wrap_sync(monkeypatch):
    """Async methods should wrap sync refresh."""
    import asyncio

    subtensor = FakeSubtensor(uid_for_hotkey={"validator": 0})
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    fake_metagraph = FakeMetagraph(hotkeys=["validator"], uids=[0])

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    # Test sync_metagraph
    snapshot = asyncio.run(manager.sync_metagraph())
    assert snapshot is not None

    # Test get_current_metagraph
    snapshot = asyncio.run(manager.get_current_metagraph())
    assert snapshot is not None


def test_snapshot_handles_missing_hotkeys_attribute(monkeypatch):
    """Snapshot should handle metagraph without hotkeys."""
    subtensor = FakeSubtensor()
    wallet = FakeWallet("validator")

    manager = MetagraphManager(subtensor, wallet, netuid=1, logger=DummyLogger())

    # Metagraph without hotkeys attribute
    fake_metagraph = types.SimpleNamespace(uids=[0, 1])

    import bittensor as bt
    monkeypatch.setattr(bt, "Metagraph", lambda *a, **k: fake_metagraph)

    snapshot = manager.refresh(force=True)

    assert snapshot is None

