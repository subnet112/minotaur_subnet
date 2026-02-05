import asyncio
import types

import neurons.bittensor_validator as bvmod


class DummyEngine:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start_continuous_validation(self):
        self.started = True

    async def stop_continuous_validation(self):
        self.stopped = True

    def get_results_for_window(self, from_ts, to_ts):
        return []

    async def compute_weights_for_epoch(self, epoch_key, validation_results):
        return types.SimpleNamespace(epoch_key=epoch_key, weights={}, stats={})

    async def process_epoch_results(self, epoch_result):
        return None


class DummyLogger:
    def __init__(self):
        self.infos = []

    def info(self, msg, *args, **kwargs):
        self.infos.append(msg)

    def warning(self, *args, **kwargs):
        return None


def test_chain_aligned_epochs_ignore_epoch_minutes(monkeypatch):
    class FakeWindowPlanner:
        def __init__(self, substrate, netuid, *, finney_substrate=None):
            return None

        def previous_epoch_window(self, last_processed_epoch, finalization_buffer_blocks):
            raise KeyboardInterrupt()

    monkeypatch.setattr(bvmod, "WindowPlanner", FakeWindowPlanner)

    validator = bvmod.BittensorValidator.__new__(bvmod.BittensorValidator)
    validator.logger = DummyLogger()
    validator._validation_engine = DummyEngine()
    validator.subtensor = types.SimpleNamespace(substrate=object())
    validator.config = types.SimpleNamespace(netuid=1, poll_seconds=1, finalization_buffer_blocks=0)
    validator._last_epoch_index = None
    validator._state_store = types.SimpleNamespace(commit_epoch=lambda *a, **k: None)

    asyncio.run(validator.run_continuous_epochs(epoch_minutes=3))

    assert any("Ignoring epoch_minutes=3" in msg for msg in validator.logger.infos)

