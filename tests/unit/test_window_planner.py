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


def test_previous_epoch_window_raises_when_timestamps_missing():
    tempo = 10
    fake_subtensor = FakeSubtensor(
        tempo=tempo,
        current_block=30,
        timestamps={},  # missing timestamps
    )
    planner = WindowPlanner(fake_subtensor, netuid=1)

    with pytest.raises(WindowPlannerError):
        planner.previous_epoch_window(last_processed_epoch=None, finalization_buffer_blocks=0)

