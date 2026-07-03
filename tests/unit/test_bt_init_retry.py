"""Regression tests for the Bittensor init-retry / fail-closed-leader fix.

Pre-fix: the entire Bittensor bring-up (subtensor connect → owner-hotkey
lookup → wallet load → MetagraphSync/WeightsEmitter construction) ran
exactly once, inline in ``AppIntentsValidator.__init__``, inside one
try/except that "continued standalone" on any exception. One transient
websocket failure at container start therefore latched a daemon that:

* never constructed its WeightsEmitter (``weights_emitter_configured=false``,
  set_weights never attempted for the life of the process),
* never resolved the subnet-owner hotkey (``owner_hotkey_resolved=false``),
* kept the constructor's fail-open ``_is_leader=True`` default and ran the
  BlockLoop as an unelected "phantom leader",
* answered /identity with HTTP 503 while /health said "ok".

That exact signature recurred ~20 times across operators since 2026-05-27
(validator-health issue #59), every time until the next container restart.

Post-fix behaviour pinned here:

1. A failed bring-up records the error (``_bt_init_error``) instead of
   silently discarding it, and resets ALL partially-built chain state.
2. Leadership fails CLOSED whenever Bittensor is configured: a failed
   bring-up or a failed initial metagraph sync leaves the daemon a
   follower (FORCE_LEADER still overrides).
3. ``_bt_init_retry_loop`` re-attempts the bring-up with backoff and, on
   success, runs the initial election and starts the metagraph tasks.
4. /health exposes the bring-up state (``bt_init``) and reports
   ``status: degraded`` while configured-but-broken, so the health
   workflow reads the actual exception instead of guessing the cause.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import AppIntentsValidator


def _make_stub(*, subtensor_url="ws://subtensor.example:9944") -> MagicMock:
    """A minimal self-stub carrying the real bt-init state machine.

    Mirrors the pattern used by the other _epoch_loop / axon-resync unit
    tests: a MagicMock ``self`` with the real methods under test bound via
    ``__get__`` so we exercise the actual implementation without paying
    for the full ~500-line constructor.
    """
    stub = MagicMock()
    stub._subtensor_url = subtensor_url
    stub._netuid = 112
    stub._bt_wallet_name = None
    stub._bt_hotkey_name = None
    stub._validator_hotkey_ss58 = "5FakeValidatorHotkeyForTests"
    stub._bt_init_attempts = 0
    stub._bt_init_ok = False
    stub._bt_init_error = None
    stub._bt_init_error_at = None
    stub._bt_init_retry_task = None
    stub._metagraph_sync = None
    stub._weights_emitter = None
    stub._tempo_gate = None
    stub._bt_wallet = None
    stub._bt_subtensor = None
    stub._bt_module = None
    stub._bt_netuid = None
    stub._validator_axon_url = ""
    stub._is_leader = False
    stub._metagraph_task = None
    stub._leader_monitor_task = None
    stub._axon_resync_task = None
    stub.weights = MagicMock()
    stub.weights.owner_hotkey = ""

    for name in (
        "_init_bittensor",
        "_init_bittensor_connected",
        "_reset_bt_state",
        "_record_bt_init_failure",
        "_bt_init_retry_loop",
        "_apply_initial_role",
        "_start_metagraph_tasks",
        "_wire_chain_tasks",
    ):
        setattr(
            stub, name,
            getattr(AppIntentsValidator, name).__get__(stub, AppIntentsValidator),
        )
    return stub


def _patch_bittensor(monkeypatch, subtensor_factory):
    """Install a fake ``bittensor`` module whose Subtensor is controllable."""
    fake_bt = MagicMock()
    fake_bt.Subtensor = subtensor_factory
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)
    return fake_bt


# ── 1. Failure recording + clean reset ───────────────────────────────────


def test_init_failure_records_error_and_resets_state(monkeypatch):
    """A subtensor connect failure is recorded verbatim, not swallowed."""
    _patch_bittensor(
        monkeypatch,
        MagicMock(side_effect=ConnectionError("[Errno 111] Connection refused")),
    )
    stub = _make_stub()
    stub._bt_init_attempts = 1  # __init__ increments before the attempt

    with pytest.raises(ConnectionError):
        stub._init_bittensor()
    stub._record_bt_init_failure(ConnectionError("[Errno 111] Connection refused"))

    assert stub._bt_init_error is not None
    assert "Connection refused" in stub._bt_init_error
    assert stub._bt_init_error_at is not None
    assert stub._metagraph_sync is None
    assert stub._weights_emitter is None


def test_record_failure_resets_partial_bringup_but_keeps_owner():
    """A half-built attempt leaves nothing behind for the next attempt —
    except a successfully-resolved owner hotkey, which stays valid."""
    stub = _make_stub()
    # Simulate a bring-up that died after building most of its objects.
    stub._metagraph_sync = MagicMock()
    stub._weights_emitter = MagicMock()
    stub._tempo_gate = MagicMock()
    stub._bt_wallet = MagicMock()
    stub._bt_subtensor = MagicMock()
    stub._bt_module = MagicMock()
    stub._bt_netuid = 112
    stub._validator_axon_url = "http://1.2.3.4:9100"
    stub.weights.owner_hotkey = "5OwnerHotkeyResolvedFromChain"

    stub._record_bt_init_failure(RuntimeError("late failure"))

    assert stub._metagraph_sync is None
    assert stub._weights_emitter is None
    assert stub._tempo_gate is None
    assert stub._bt_wallet is None
    assert stub._bt_subtensor is None
    assert stub._bt_module is None
    assert stub._bt_netuid is None
    assert stub._validator_axon_url == ""
    assert stub.weights.owner_hotkey == "5OwnerHotkeyResolvedFromChain"


def test_init_error_is_truncated():
    stub = _make_stub()
    stub._record_bt_init_failure(RuntimeError("x" * 5000))
    assert len(stub._bt_init_error) == 300


# ── 2. Fail-closed leadership ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_sync_failure_stays_follower(monkeypatch):
    """Pre-fix this branch logged '(assuming leader)' and set True."""
    monkeypatch.delenv("FORCE_LEADER", raising=False)
    stub = _make_stub()
    stub._metagraph_sync = MagicMock()
    stub._metagraph_sync.sync_once = AsyncMock(side_effect=RuntimeError("chain down"))
    stub._is_leader = False

    await stub._apply_initial_role()

    assert stub._is_leader is False


@pytest.mark.asyncio
async def test_initial_sync_success_adopts_elected_role(monkeypatch):
    monkeypatch.delenv("FORCE_LEADER", raising=False)
    stub = _make_stub()
    state = MagicMock()
    state.my_role = "leader"
    state.block = 100
    state.validators = []
    state.my_last_update_block = None
    stub._metagraph_sync = MagicMock()
    stub._metagraph_sync.sync_once = AsyncMock(return_value=state)
    stub._metagraph_sync.is_leader = True
    stub._is_leader = False

    await stub._apply_initial_role()

    assert stub._is_leader is True


@pytest.mark.asyncio
async def test_force_leader_still_overrides_failed_sync(monkeypatch):
    """FORCE_LEADER is an explicit operator knob — it must keep working
    even when the chain is unreachable (local-testnet use case)."""
    monkeypatch.setenv("FORCE_LEADER", "1")
    stub = _make_stub()
    stub._metagraph_sync = MagicMock()
    stub._metagraph_sync.sync_once = AsyncMock(side_effect=RuntimeError("chain down"))
    stub._is_leader = False

    await stub._apply_initial_role()

    assert stub._is_leader is True


# ── 3. Retry loop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_loop_recovers_and_wires_tasks(monkeypatch):
    """Attempts fail twice, then succeed → error cleared, election run,
    metagraph tasks started, no leader promotion for a follower."""
    monkeypatch.delenv("FORCE_LEADER", raising=False)
    stub = _make_stub()
    stub._bt_init_attempts = 1  # the failed __init__ attempt

    attempts = {"n": 0}

    def fake_init():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("still refusing")
        stub._metagraph_sync = MagicMock()
        stub._metagraph_sync.sync_once = AsyncMock(
            side_effect=RuntimeError("first election sync can fail too"),
        )

    stub._init_bittensor = fake_init
    stub._start_metagraph_tasks = MagicMock()
    stub._become_leader = AsyncMock()
    stub.block_loop = MagicMock()
    stub.block_loop.running = False

    async def instant_sleep(_delay):
        return None

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=instant_sleep):
        await stub._bt_init_retry_loop()

    assert attempts["n"] == 3
    assert stub._bt_init_ok is True
    assert stub._bt_init_error is None
    assert stub._bt_init_error_at is None
    assert stub._bt_init_attempts == 4  # 1 from __init__ + 3 retries
    stub._start_metagraph_tasks.assert_called_once()
    stub._become_leader.assert_not_awaited()
    assert stub._is_leader is False


@pytest.mark.asyncio
async def test_retry_loop_promotes_when_elected(monkeypatch):
    monkeypatch.delenv("FORCE_LEADER", raising=False)
    stub = _make_stub()

    def fake_init():
        state = MagicMock()
        state.my_role = "leader"
        state.block = 100
        state.validators = []
        state.my_last_update_block = None
        stub._metagraph_sync = MagicMock()
        stub._metagraph_sync.sync_once = AsyncMock(return_value=state)
        stub._metagraph_sync.is_leader = True

    stub._init_bittensor = fake_init
    stub._start_metagraph_tasks = MagicMock()
    stub._become_leader = AsyncMock()
    stub.block_loop = MagicMock()
    stub.block_loop.running = False

    async def instant_sleep(_delay):
        return None

    with patch("minotaur_subnet.validator.main.asyncio.sleep", new=instant_sleep):
        await stub._bt_init_retry_loop()

    assert stub._is_leader is True
    stub._become_leader.assert_awaited_once()


# ── 4. /health surfacing ─────────────────────────────────────────────────


def _health_stub() -> MagicMock:
    """Stub with enough shape for the real _handle_health to run."""
    stub = _make_stub()
    stub.engine.list_loaded_intents.return_value = []
    stub.orderbook.stats.return_value = {}
    stub._start_time = 0.0
    stub.block_loop = MagicMock()
    stub.block_loop.running = False
    stub._champion_source = "init"
    stub._champion_miner_id = None
    stub._tempo_gate = None
    stub.weights.epoch_seconds = 60
    stub.weights.owner_hotkey = ""
    stub._last_emit_state = None
    stub._last_successful_emit_state = None
    stub._handle_health = AppIntentsValidator._handle_health.__get__(
        stub, AppIntentsValidator,
    )
    return stub


@pytest.mark.asyncio
async def test_health_reports_degraded_with_bt_init_error():
    stub = _health_stub()
    stub._bt_init_attempts = 4
    stub._bt_init_error = "[Errno 111] Connection refused"
    stub._bt_init_error_at = 1234.5

    resp = await stub._handle_health(MagicMock())
    body = json.loads(resp.text)

    assert body["status"] == "degraded"
    assert body["bt_init"] == {
        "configured": True,
        "ok": False,
        "attempts": 4,
        "error": "[Errno 111] Connection refused",
        "error_at": 1234.5,
        "retrying": True,
    }
    assert body["weights_emitter_configured"] is False


@pytest.mark.asyncio
async def test_health_ok_when_bringup_succeeded():
    stub = _health_stub()
    stub._bt_init_attempts = 1
    stub._bt_init_ok = True
    stub._metagraph_sync = MagicMock()
    stub._metagraph_sync.state = None

    resp = await stub._handle_health(MagicMock())
    body = json.loads(resp.text)

    assert body["status"] == "ok"
    assert body["bt_init"]["ok"] is True
    assert body["bt_init"]["retrying"] is False


@pytest.mark.asyncio
async def test_health_ok_when_standalone():
    """No SUBTENSOR_URL at all: not degraded — chain integration is
    simply off (local dev / standalone mode)."""
    stub = _health_stub()
    stub._subtensor_url = None

    resp = await stub._handle_health(MagicMock())
    body = json.loads(resp.text)

    assert body["status"] == "ok"
    assert body["bt_init"]["configured"] is False
    assert body["bt_init"]["retrying"] is False


# ── 5. start() wiring: _wire_chain_tasks ─────────────────────────────────
#
# _wire_chain_tasks is the retry mechanism's ONLY activation point —
# without these tests, deleting the spawn (or regressing its condition)
# would pass the whole suite while production reverts to the latched
# broken-daemon behavior this fix exists for.


@pytest.mark.asyncio
async def test_wire_spawns_retry_task_when_init_failed():
    stub = _make_stub()
    assert stub._metagraph_sync is None  # bring-up failed in __init__

    async def noop_retry():
        return None

    stub._bt_init_retry_loop = noop_retry
    stub._start_metagraph_tasks = MagicMock()

    stub._wire_chain_tasks()

    assert stub._bt_init_retry_task is not None
    stub._start_metagraph_tasks.assert_not_called()
    await stub._bt_init_retry_task  # completes (noop) — also retrieves it


@pytest.mark.asyncio
async def test_wire_starts_metagraph_tasks_on_healthy_bringup():
    stub = _make_stub()
    stub._metagraph_sync = MagicMock()
    stub._start_metagraph_tasks = MagicMock()

    stub._wire_chain_tasks()

    stub._start_metagraph_tasks.assert_called_once()
    assert stub._bt_init_retry_task is None


@pytest.mark.asyncio
async def test_wire_does_nothing_standalone():
    stub = _make_stub(subtensor_url=None)
    stub._start_metagraph_tasks = MagicMock()

    stub._wire_chain_tasks()

    stub._start_metagraph_tasks.assert_not_called()
    assert stub._bt_init_retry_task is None


# ── 6. The REAL __init__ wiring ──────────────────────────────────────────
#
# The stub tests bind helper methods; these construct the actual
# validator so the __init__-only wiring is pinned too: the fail-closed
# ``_is_leader = False`` pre-set (re-adding ``self._is_leader = True`` in
# the except branch — the literal phantom-leader revert — must fail
# here), the except→_record_bt_init_failure hookup, and the attempt
# pre-increment.


def _make_real_validator(monkeypatch, tmp_path, subtensor_factory):
    fake_bt = MagicMock()
    fake_bt.Subtensor = subtensor_factory
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)
    monkeypatch.delenv("FORCE_LEADER", raising=False)
    monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
    monkeypatch.delenv("OWNER_HOTKEY", raising=False)

    from minotaur_subnet.store import AppIntentStore

    return AppIntentsValidator(
        store=AppIntentStore(store_path=tmp_path / "store.db"),
        subtensor_url="ws://subtensor.example:9944",
        validator_hotkey_ss58="5FakeValidatorHotkeyForTests",
    )


def test_real_init_fails_closed_and_records_error(monkeypatch, tmp_path):
    v = _make_real_validator(
        monkeypatch, tmp_path,
        MagicMock(side_effect=ConnectionError("[Errno 111] Connection refused")),
    )

    assert v._is_leader is False  # fail-closed: the phantom-leader revert trips here
    assert v._bt_init_attempts == 1
    assert v._bt_init_ok is False
    assert v._bt_init_error is not None
    assert "Connection refused" in v._bt_init_error
    assert v._metagraph_sync is None
    assert v._weights_emitter is None


def test_real_init_success_is_still_follower_until_elected(monkeypatch, tmp_path):
    """A successful bring-up (metagraph-only: no wallet envs) must ALSO
    stay follower — the election in start() decides, not the default."""
    from types import SimpleNamespace

    fake_subtensor = MagicMock()
    fake_subtensor.query_subtensor.return_value = SimpleNamespace(
        value="5OwnerHotkeyFromChain",
    )
    v = _make_real_validator(
        monkeypatch, tmp_path, MagicMock(return_value=fake_subtensor),
    )

    assert v._bt_init_ok is True
    assert v._bt_init_error is None
    assert v._metagraph_sync is not None
    assert v._weights_emitter is None  # no wallet → metagraph-only
    assert v._is_leader is False  # follower until the election says otherwise
    assert v.weights.owner_hotkey == "5OwnerHotkeyFromChain"
