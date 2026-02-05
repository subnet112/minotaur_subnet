import datetime as dt

import pytest

from neurons.window_planner import WindowPlanner, WindowPlannerError


class FakeValue:
    def __init__(self, value):
        self.value = value


class FakeSubtensor:
    def __init__(self, tempo: int, current_block: int, timestamps):
        self._tempo = tempo
        self._current_block = current_block
        # timestamps: dict block -> milliseconds
        self._timestamps = timestamps

    def query(self, module, storage, params=None, block_hash=None):
        if storage == "Tempo":
            return FakeValue(self._tempo)
        if storage == "Now":
            if block_hash is None:
                return None
            block_number = int(block_hash.replace("0x", ""), 16)
            if block_number in self._timestamps:
                return FakeValue(self._timestamps[block_number])
            return None
        return None

    def get_block_header(self, block_hash=None):
        return {"number": self._current_block}

    def get_block_hash(self, block_number: int):
        # Use hex to mirror substrate hash encoding for tests
        return hex(block_number)


def test_previous_epoch_window_success():
    tempo = 10
    current_block = 25  # epoch index 2
    # epoch 1 blocks: 10-19 inclusive
    timestamps = {
        10: int(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000),
        19: int(dt.datetime(2025, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp() * 1000),
    }
    fake_subtensor = FakeSubtensor(tempo=tempo, current_block=current_block, timestamps=timestamps)
    planner = WindowPlanner(fake_subtensor, netuid=1)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)

    assert result is not None
    prev_epoch, from_ts, to_ts = result
    assert prev_epoch == 1
    assert from_ts == "2025-01-01T00:00:00Z"
    assert to_ts == "2025-01-01T00:00:30Z"


def test_previous_epoch_window_respects_buffer():
    tempo = 10
    # current block only 1 greater than end block => insufficient finalization
    fake_subtensor = FakeSubtensor(
        tempo=tempo,
        current_block=21,
        timestamps={10: 0, 19: 1000},
    )
    planner = WindowPlanner(fake_subtensor, netuid=1)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=5)
    assert result is None


def test_previous_epoch_window_estimates_when_timestamps_missing():
    """When on-chain timestamps are unavailable, block-time estimation is used."""
    tempo = 10
    fake_subtensor = FakeSubtensor(
        tempo=tempo,
        current_block=30,
        timestamps={},  # missing timestamps
    )
    planner = WindowPlanner(fake_subtensor, netuid=1)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)
    assert result is not None
    prev_epoch, from_ts, to_ts = result
    assert prev_epoch == 2
    # Estimated timestamps should be valid ISO strings
    assert from_ts.endswith("Z")
    assert to_ts.endswith("Z")
    # from_ts should be earlier than to_ts (start block 20 is before end block 29)
    assert from_ts < to_ts


def test_finney_substrate_used_for_current_block():
    """When finney_substrate is provided, _get_current_block uses it instead of archive."""
    tempo = 10
    timestamps = {
        10: int(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000),
        19: int(dt.datetime(2025, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp() * 1000),
    }
    # Archive substrate reports a stale block (block 5 → epoch 0, no previous epoch)
    archive = FakeSubtensor(tempo=tempo, current_block=5, timestamps=timestamps)
    # Finney substrate reports the real chain head (block 25 → epoch 2)
    finney = FakeSubtensor(tempo=tempo, current_block=25, timestamps=timestamps)

    planner = WindowPlanner(archive, netuid=1, finney_substrate=finney)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)
    assert result is not None
    prev_epoch, from_ts, to_ts = result
    assert prev_epoch == 1


def test_archive_still_used_for_timestamps():
    """Historical queries (timestamps) should use the archive substrate, not finney."""
    tempo = 10
    # Only archive has the timestamps
    archive_timestamps = {
        10: int(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000),
        19: int(dt.datetime(2025, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp() * 1000),
    }
    archive = FakeSubtensor(tempo=tempo, current_block=5, timestamps=archive_timestamps)
    # Finney has no timestamps (it's not an archive node)
    finney = FakeSubtensor(tempo=tempo, current_block=25, timestamps={})

    planner = WindowPlanner(archive, netuid=1, finney_substrate=finney)

    # Finney provides the current block (25), but timestamps come from archive
    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)
    assert result is not None
    prev_epoch, from_ts, to_ts = result
    assert prev_epoch == 1
    assert from_ts == "2025-01-01T00:00:00Z"
    assert to_ts == "2025-01-01T00:00:30Z"


def test_no_finney_substrate_falls_back_to_archive():
    """Without finney_substrate, behavior is unchanged (uses archive for everything)."""
    tempo = 10
    timestamps = {
        10: int(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000),
        19: int(dt.datetime(2025, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp() * 1000),
    }
    archive = FakeSubtensor(tempo=tempo, current_block=25, timestamps=timestamps)

    planner = WindowPlanner(archive, netuid=1)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)
    assert result is not None
    prev_epoch, _, _ = result
    assert prev_epoch == 1


def test_finney_fallback_for_timestamps_when_archive_behind():
    """When archive doesn't have recent blocks, finney is used as fallback for timestamps."""
    tempo = 10
    finney_timestamps = {
        10: int(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000),
        19: int(dt.datetime(2025, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp() * 1000),
    }
    # Archive is behind — no timestamps for these blocks
    archive = FakeSubtensor(tempo=tempo, current_block=5, timestamps={})
    # Finney has the recent blocks
    finney = FakeSubtensor(tempo=tempo, current_block=25, timestamps=finney_timestamps)

    planner = WindowPlanner(archive, netuid=1, finney_substrate=finney)

    result = planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)
    assert result is not None
    prev_epoch, from_ts, to_ts = result
    assert prev_epoch == 1
    assert from_ts == "2025-01-01T00:00:00Z"
    assert to_ts == "2025-01-01T00:00:30Z"

