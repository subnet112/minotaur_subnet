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

    @pytest.mark.asyncio
    async def test_emit_reconnects_on_stale_ws(self):
        # A dead/stale subtensor websocket (operator rotated the RPC) makes the
        # blocking emit RAISE; the emitter must rebuild its client against the URL
        # so the NEXT epoch reconnects instead of failing on the dead socket forever.
        wallet = MagicMock()
        dead = MagicMock()
        dead.metagraph.side_effect = ConnectionError("websocket connection closed")
        fresh = MagicMock()

        we = WeightsEmitter(
            wallet=wallet, subtensor=dead, netuid=1, subtensor_url="ws://node:9944",
        )
        with patch("bittensor.Subtensor", return_value=fresh) as mk_subtensor:
            result = await we.emit_async({"hk_a": 1.0})

        assert result is False                     # this emit failed (dead ws)
        mk_subtensor.assert_called_once_with(network="ws://node:9944")
        assert we.subtensor is fresh               # …but it self-healed for next time

    @pytest.mark.asyncio
    async def test_emit_no_reconnect_without_url(self):
        # Legacy behaviour preserved: no URL → no reconnect attempt, client unchanged.
        wallet = MagicMock()
        dead = MagicMock()
        dead.metagraph.side_effect = ConnectionError("websocket connection closed")

        we = WeightsEmitter(wallet=wallet, subtensor=dead, netuid=1)  # no subtensor_url
        with patch("bittensor.Subtensor") as mk_subtensor:
            result = await we.emit_async({"hk_a": 1.0})

        assert result is False
        mk_subtensor.assert_not_called()
        assert we.subtensor is dead

    @pytest.mark.asyncio
    async def test_reconnect_failure_is_swallowed(self):
        # If the RPC is still down, rebuilding the client also fails — that must be
        # caught (logged), not raised, so the emit loop keeps ticking.
        wallet = MagicMock()
        dead = MagicMock()
        dead.metagraph.side_effect = ConnectionError("ws closed")

        we = WeightsEmitter(
            wallet=wallet, subtensor=dead, netuid=1, subtensor_url="ws://node:9944",
        )
        with patch("bittensor.Subtensor", side_effect=OSError("still down")):
            result = await we.emit_async({"hk_a": 1.0})

        assert result is False
        assert we.subtensor is dead   # left as-is; next epoch retries the reconnect

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

    def test_emit_blocking_drops_unknown_hotkeys_when_no_owner_configured(self, monkeypatch):
        """Without SUBNET_OWNER_HOTKEY there is no fallback — unknown hotkeys drop."""
        monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
        monkeypatch.delenv("OWNER_HOTKEY", raising=False)

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
        uids = call_kwargs.kwargs["uids"]
        assert len(uids) == 1  # only hk_a survives

    def test_emit_blocking_no_valid_uids(self, monkeypatch):
        """Unknown hotkey + no owner fallback = nothing to emit."""
        monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
        monkeypatch.delenv("OWNER_HOTKEY", raising=False)

        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_a"], [100.0])

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_unknown": 0.5})
        assert result is False
        subtensor.set_weights.assert_not_called()

    def test_emit_blocking_reroutes_deregistered_champion_to_owner(self, monkeypatch):
        """A champion hotkey that's no longer in the metagraph gets its weight
        routed to the configured owner UID instead of being silently dropped.

        This guards against the failure mode where a miner wins championship,
        deregisters, and the validator would otherwise emit empty weights
        (losing dividends and bypassing the burn-to-owner fallback)."""
        monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "hk_owner")

        wallet = MagicMock()
        subtensor = MagicMock()
        # owner at UID 0, no longer-champion hotkey in metagraph
        subtensor.metagraph.return_value = _make_mock_metagraph(
            ["hk_owner", "hk_other"], [100.0, 10.0],
        )
        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_dead_champion": 1.0})

        assert result is True
        call_kwargs = subtensor.set_weights.call_args
        uids = call_kwargs.kwargs["uids"]
        weights = call_kwargs.kwargs["weights"]
        assert list(uids) == [0]                  # owner UID
        assert abs(float(weights[0]) - 1.0) < 1e-6

    def test_emit_blocking_merges_known_and_deregistered_into_owner(self, monkeypatch):
        """Mixed mapping: registered miner stays at its UID, deregistered
        portion accrues to the owner UID. Output normalizes across both."""
        monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "hk_owner")

        wallet = MagicMock()
        subtensor = MagicMock()
        # hk_owner at UID 0, hk_alive at UID 1
        subtensor.metagraph.return_value = _make_mock_metagraph(
            ["hk_owner", "hk_alive"], [100.0, 10.0],
        )
        mock_result = MagicMock()
        mock_result.success = True
        subtensor.set_weights.return_value = mock_result

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_alive": 0.6, "hk_dead": 0.4})

        assert result is True
        call_kwargs = subtensor.set_weights.call_args
        uids = list(call_kwargs.kwargs["uids"])
        weights = list(call_kwargs.kwargs["weights"])
        uid_to_weight = dict(zip(uids, (float(w) for w in weights)))
        assert set(uid_to_weight.keys()) == {0, 1}
        assert abs(uid_to_weight[1] - 0.6) < 1e-6   # hk_alive UID 1
        assert abs(uid_to_weight[0] - 0.4) < 1e-6   # rerouted from hk_dead

    def test_emit_blocking_owner_hotkey_itself_missing_drops(self, monkeypatch):
        """If SUBNET_OWNER_HOTKEY is configured but not currently in the
        metagraph (e.g. owner key rotated mid-flight), unknown weight has no
        rescue path — drop and let the alarm fire. Don't silently misroute."""
        monkeypatch.setenv("SUBNET_OWNER_HOTKEY", "hk_owner_gone")

        wallet = MagicMock()
        subtensor = MagicMock()
        subtensor.metagraph.return_value = _make_mock_metagraph(["hk_other"], [10.0])

        we = WeightsEmitter(wallet=wallet, subtensor=subtensor, netuid=1)
        result = we._emit_blocking({"hk_dead_champion": 1.0})
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
