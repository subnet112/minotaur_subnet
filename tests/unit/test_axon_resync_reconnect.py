"""The axon-resync loop's long-lived subtensor self-heals on a stale websocket.

Same root cause as the weight-emitter (one Subtensor reused forever, dies on an RPC
rotation). These pin AppIntentsValidator._reconnect_bt_subtensor directly via an
unbound call with a fake self, so we don't have to stand up the whole daemon.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.validator.main import AppIntentsValidator


def _fake(url, bt_module, current):
    return SimpleNamespace(_subtensor_url=url, _bt_module=bt_module, _bt_subtensor=current)


def test_reconnect_rebuilds_client():
    fresh = MagicMock()
    bt = MagicMock()
    bt.Subtensor.return_value = fresh
    fake = _fake("ws://node:9944", bt, object())

    AppIntentsValidator._reconnect_bt_subtensor(fake)

    bt.Subtensor.assert_called_once_with(network="ws://node:9944")
    assert fake._bt_subtensor is fresh


def test_reconnect_noop_without_url():
    bt = MagicMock()
    old = object()
    fake = _fake("", bt, old)

    AppIntentsValidator._reconnect_bt_subtensor(fake)

    bt.Subtensor.assert_not_called()
    assert fake._bt_subtensor is old


def test_reconnect_noop_without_bt_module():
    old = object()
    fake = _fake("ws://node:9944", None, old)

    AppIntentsValidator._reconnect_bt_subtensor(fake)  # must not raise

    assert fake._bt_subtensor is old


def test_reconnect_swallows_failure():
    bt = MagicMock()
    bt.Subtensor.side_effect = OSError("rpc still down")
    old = object()
    fake = _fake("ws://node:9944", bt, old)

    AppIntentsValidator._reconnect_bt_subtensor(fake)  # must not raise

    assert fake._bt_subtensor is old   # left as-is; next tick retries
