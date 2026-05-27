"""Tests for the periodic axon-resync loop.

Background: validators behind dynamic-IP setups (AWS ELB/ALB, Cloudflare
proxy, residential IPs) have their public IP rotate outside the
operator's control. The startup-only ``serve_axon`` call publishes the
IP that resolved at boot, then goes stale. ``_axon_resync_loop`` runs on
a timer and re-publishes whenever the resolved IP drifted from the
chain entry.

These tests verify:
  - The loop respects ``AXON_RESYNC_INTERVAL_SECONDS`` env
  - It clamps the interval to a sane minimum (no chain-spam)
  - Setting the env to <=0 disables the loop
  - Exceptions inside one iteration don't kill the loop
  - The loop reuses the stored bittensor handles and calls
    ``_auto_serve_axon_on_metagraph`` with them
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import AppIntentsValidator


async def _run_one_iteration(self_stub) -> None:
    """Drive ``_axon_resync_loop`` long enough to execute one tick body,
    then exit. Patches ``asyncio.sleep`` to fire the first sleep
    instantly and raise on the second so the body runs once.

    The loop's first statement is ``await asyncio.sleep(interval)`` —
    so the FIRST sleep is the one we let through. The body that calls
    ``_auto_serve_axon_on_metagraph`` runs after that. We raise on the
    second sleep to exit the while-True.
    """
    call_count = {"sleep": 0}

    async def fake_sleep(delay):
        call_count["sleep"] += 1
        if call_count["sleep"] >= 2:
            raise asyncio.CancelledError
        # First sleep returns immediately

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._axon_resync_loop(self_stub)


def _make_self_stub(*,
                    axon_url: str = "http://my-elb.example.com:9100",
                    my_hotkey: str = "5G66U8yjZJygrr8E2JGaR3PkY7UQzMtJdq9ZU2U7UQUsn112"):
    """Build a minimal AppIntentsValidator stub with bittensor handles wired."""
    self_stub = MagicMock()
    self_stub._bt_subtensor = MagicMock(name="subtensor")
    self_stub._bt_module = MagicMock(name="bt")
    self_stub._bt_wallet = MagicMock(name="wallet")
    self_stub._bt_netuid = 112
    self_stub._validator_axon_url = axon_url
    self_stub._metagraph_sync = MagicMock()
    self_stub._metagraph_sync.my_hotkey = my_hotkey
    return self_stub


@pytest.mark.asyncio
async def test_calls_serve_helper_with_stored_handles(monkeypatch):
    """The loop's normal-path tick calls _auto_serve_axon_on_metagraph
    with the stored subtensor/bt_module/wallet/netuid/hotkey/axon_url."""
    self_stub = _make_self_stub()
    with patch(
        "minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"
    ) as mock_helper:
        await _run_one_iteration(self_stub)

    mock_helper.assert_called_once()
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["subtensor"] is self_stub._bt_subtensor
    assert kwargs["bt_module"] is self_stub._bt_module
    assert kwargs["wallet"] is self_stub._bt_wallet
    assert kwargs["netuid"] == 112
    assert kwargs["my_hotkey"] == self_stub._metagraph_sync.my_hotkey
    assert kwargs["axon_url"] == self_stub._validator_axon_url


@pytest.mark.asyncio
async def test_disabled_when_interval_zero(monkeypatch):
    """AXON_RESYNC_INTERVAL_SECONDS=0 must short-circuit immediately,
    not even call the helper once. For operators with truly static IPs
    who want zero background chain reads."""
    monkeypatch.setenv("AXON_RESYNC_INTERVAL_SECONDS", "0")
    self_stub = _make_self_stub()
    with patch(
        "minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"
    ) as mock_helper:
        await AppIntentsValidator._axon_resync_loop(self_stub)  # returns immediately

    mock_helper.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_when_interval_negative(monkeypatch):
    """Negative values also disable — same intent as 0."""
    monkeypatch.setenv("AXON_RESYNC_INTERVAL_SECONDS", "-1")
    self_stub = _make_self_stub()
    with patch(
        "minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"
    ) as mock_helper:
        await AppIntentsValidator._axon_resync_loop(self_stub)

    mock_helper.assert_not_called()


@pytest.mark.asyncio
async def test_interval_clamped_to_minimum_60s(monkeypatch):
    """Operators setting <60s shouldn't beat the chain rate limit
    (~10 min). Clamp to 60s as the lower bound."""
    monkeypatch.setenv("AXON_RESYNC_INTERVAL_SECONDS", "5")
    self_stub = _make_self_stub()
    observed_intervals: list[float] = []

    async def recording_sleep(delay):
        observed_intervals.append(delay)
        if len(observed_intervals) >= 2:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=recording_sleep), \
         patch("minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._axon_resync_loop(self_stub)

    assert observed_intervals[0] >= 60, (
        f"Interval should clamp to >=60s, got {observed_intervals[0]}s"
    )


@pytest.mark.asyncio
async def test_invalid_env_value_falls_back_to_default(monkeypatch):
    """Garbled AXON_RESYNC_INTERVAL_SECONDS (non-integer) falls back to
    the default rather than crashing the daemon."""
    monkeypatch.setenv("AXON_RESYNC_INTERVAL_SECONDS", "not-a-number")
    self_stub = _make_self_stub()
    observed_intervals: list[float] = []

    async def recording_sleep(delay):
        observed_intervals.append(delay)
        if len(observed_intervals) >= 2:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=recording_sleep), \
         patch("minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._axon_resync_loop(self_stub)

    assert observed_intervals[0] == 300, (
        "Default should be 300s when env value is not parseable"
    )


@pytest.mark.asyncio
async def test_iteration_exception_does_not_kill_loop():
    """If _auto_serve_axon_on_metagraph raises (eg. DNS hiccup, chain
    timeout), the loop must log and continue, not crash."""
    self_stub = _make_self_stub()
    call_count = {"helper": 0}

    def flaky_helper(**kwargs):
        call_count["helper"] += 1
        if call_count["helper"] == 1:
            raise RuntimeError("simulated chain timeout")

    sleep_count = {"n": 0}

    async def fake_sleep(delay):
        sleep_count["n"] += 1
        # 1st sleep: pass-through. 2nd: helper has been called twice.
        # 3rd: exit.
        if sleep_count["n"] >= 3:
            raise asyncio.CancelledError

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=fake_sleep), \
         patch(
            "minotaur_subnet.validator.main._auto_serve_axon_on_metagraph",
            side_effect=flaky_helper,
         ):
        with pytest.raises(asyncio.CancelledError):
            await AppIntentsValidator._axon_resync_loop(self_stub)

    # Two iterations attempted — proves the loop survived the first failure
    assert call_count["helper"] == 2


@pytest.mark.asyncio
async def test_skips_iteration_when_no_my_hotkey():
    """If metagraph_sync somehow lost track of my_hotkey (eg. mid-restart),
    skip the iteration rather than serve garbage. Next tick will retry."""
    self_stub = _make_self_stub()
    self_stub._metagraph_sync.my_hotkey = ""  # missing
    with patch(
        "minotaur_subnet.validator.main._auto_serve_axon_on_metagraph"
    ) as mock_helper:
        await _run_one_iteration(self_stub)

    mock_helper.assert_not_called()
