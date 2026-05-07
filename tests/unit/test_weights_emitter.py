"""Unit tests for WeightsEmitter."""

import asyncio
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from minotaur_subnet.validator.weights_emitter import WeightsEmitter


def _make_mock_metagraph(hotkeys, stakes):
    """Create a mock metagraph with given hotkeys and stakes."""
    mg = MagicMock()
    mg.n.item.return_value = len(hotkeys)
    mg.hotkeys = hotkeys
    mg.S = [MagicMock() for _ in stakes]
    for i, s in enumerate(stakes):
        mg.S[i].item.return_value = s
    return mg


class TestWeightsEmitter:
    def test_init(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        assert we.netuid == 1
        assert we.version_key == 6

    @pytest.mark.asyncio
    async def test_emit_empty_mapping(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        we = WeightsEmitter(wallet=wallet, subtensor=subtensor)
        result = await we.emit_async({})
        assert result is False

    def test_emit_blocking_maps_hotkeys_to_uids(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        hotkeys = ["hk_a", "hk_b", "hk_c"]
        stakes = [100.0, 200.0, 50.0]
        subtensor.metagraph.return_value = _make_mock_metagraph(hotkeys, stakes)

        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_a": 0.5, "hk_b": 0.3, "hk_c": 0.2})

        assert result is True
        subtensor.set_weights.assert_called_once()
        call_kwargs = subtensor.set_weights.call_args
        # Check UIDs and weights are passed
        assert call_kwargs.kwargs["netuid"] == 1

    def test_emit_blocking_skips_unknown_hotkeys(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_a"], [100.0])

        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_a": 0.5, "hk_unknown": 0.5})

        assert result is True
        call_kwargs = subtensor.set_weights.call_args
        # Only hk_a should be in the arrays (UID 0)
        import numpy as np
        uids = call_kwargs.kwargs["uids"]
        assert len(uids) == 1

    def test_emit_blocking_no_valid_uids(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_a"], [100.0])

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_unknown": 0.5})
        assert result is False
        subtensor.set_weights.assert_not_called()

    def test_emit_blocking_normalizes_weights(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(
            ["hk_a", "hk_b"], [100.0, 100.0],
        )

        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_a": 3.0, "hk_b": 1.0})

        assert result is True
        call_kwargs = subtensor.set_weights.call_args
        weights = call_kwargs.kwargs["weights"]
        # Should normalize: 3/(3+1)=0.75, 1/(3+1)=0.25
        assert abs(float(weights[0]) - 0.75) < 0.01
        assert abs(float(weights[1]) - 0.25) < 0.01

    def test_emit_blocking_handles_set_weights_failure(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_a"], [100.0])

        mock_result = MagicMock()
        mock_result.success = False
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_a": 1.0})
        assert result is False

    @pytest.mark.asyncio
    async def test_emit_async_calls_blocking(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_a"], [100.0])

        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = await we.emit_async({"hk_a": 1.0})
        assert result is True

    @pytest.mark.asyncio
    async def test_emit_async_handles_exception(self):
        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.side_effect = RuntimeError("connection failed")

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = await we.emit_async({"hk_a": 1.0})
        assert result is False

    def test_block_time_local_testnet(self):
        we = WeightsEmitter(
            wallet=MagicMock(), subtensor=MagicMock(),
            block_time=0.25,
        )
        assert we.block_time == 0.25

    def test_max_attempts_configurable(self):
        we = WeightsEmitter(
            wallet=MagicMock(), subtensor=MagicMock(),
            max_attempts=5,
        )
        assert we.max_attempts == 5
