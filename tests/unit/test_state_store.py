"""Tests for StateStore state persistence."""
import json
import os
import tempfile
import pytest

from neurons.state_store import StateStore


def test_state_store_creates_new_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store.get_watermark() is None
        assert store.get_last_epoch() is None
        assert store.get_last_scores() == {}


def test_state_store_commit_epoch():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        store.commit_epoch(
            epoch_index=42,
            to_ts="2025-01-01T00:00:00Z",
            last_scores={"miner-a": 0.5, "miner-b": 0.3}
        )

        assert store.get_last_epoch() == 42
        assert store.get_watermark() == "2025-01-01T00:00:00Z"
        assert store.get_last_scores() == {"miner-a": 0.5, "miner-b": 0.3}


def test_state_store_persistence_across_restarts():
    with tempfile.TemporaryDirectory() as tmpdir:
        # First instance
        store1 = StateStore(base_dir=tmpdir, filename="test_state.json")
        store1.commit_epoch(
            epoch_index=10,
            to_ts="2025-01-10T12:00:00Z",
            last_scores={"m1": 0.7}
        )

        # Second instance loads from file
        store2 = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store2.get_last_epoch() == 10
        assert store2.get_watermark() == "2025-01-10T12:00:00Z"
        assert store2.get_last_scores() == {"m1": 0.7}


def test_state_store_commit_window():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        store.commit_window(
            to_ts="2025-01-05T06:00:00Z",
            last_scores={"miner-x": 0.9}
        )

        assert store.get_watermark() == "2025-01-05T06:00:00Z"
        assert store.get_last_scores() == {"miner-x": 0.9}


def test_state_store_weight_block():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store.get_last_weight_block() is None

        store.set_last_weight_block(12345)

        assert store.get_last_weight_block() == 12345


def test_state_store_handles_corrupted_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "test_state.json")

        # Write corrupted JSON
        with open(filepath, "w") as f:
            f.write("{ this is not valid json }")

        # Should not crash, falls back to defaults
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store.get_watermark() is None
        assert store.get_last_epoch() is None


def test_state_store_handles_empty_scores():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        store.commit_epoch(epoch_index=1, to_ts="ts", last_scores={})

        assert store.get_last_scores() == {}


def test_state_store_handles_non_dict_scores():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "test_state.json")

        # Write state with invalid scores format
        with open(filepath, "w") as f:
            json.dump({"last_scores": "not a dict"}, f)

        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store.get_last_scores() == {}


def test_state_store_coerces_score_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "test_state.json")

        # Write state with string score values
        with open(filepath, "w") as f:
            json.dump({"last_scores": {"m1": "0.5", "m2": 0.3}}, f)

        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        scores = store.get_last_scores()
        assert scores["m1"] == 0.5
        assert scores["m2"] == 0.3


def test_state_store_creates_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        # First save
        store.commit_epoch(epoch_index=1, to_ts="t1", last_scores={})

        # Second save should create backup
        store.commit_epoch(epoch_index=2, to_ts="t2", last_scores={})

        backup_path = os.path.join(tmpdir, "test_state.json.backup")
        assert os.path.exists(backup_path)


def test_state_store_recovers_from_none_epoch():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "test_state.json")

        with open(filepath, "w") as f:
            json.dump({"last_epoch_index": None}, f)

        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        assert store.get_last_epoch() is None


def test_state_store_multiple_epochs():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(base_dir=tmpdir, filename="test_state.json")

        for i in range(5):
            store.commit_epoch(
                epoch_index=i,
                to_ts=f"2025-01-0{i+1}T00:00:00Z",
                last_scores={f"miner-{i}": float(i) / 10}
            )

        assert store.get_last_epoch() == 4
        assert store.get_watermark() == "2025-01-05T00:00:00Z"
        assert store.get_last_scores() == {"miner-4": 0.4}

