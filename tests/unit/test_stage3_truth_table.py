"""Truth-table tests for the Stage 3 regression gate.

Covers the documented matrix in EpochManager._passes_regression_gate
so disabling the gate vs missing archive vs having/lacking candidates
all produce the right decision — no more order-of-return surprises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.epoch.manager import EpochManager, _stage3_disabled


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_stage3_disabled_env_helper(monkeypatch):
    monkeypatch.delenv("STAGE3_DISABLED", raising=False)
    assert _stage3_disabled() is False

    monkeypatch.setenv("STAGE3_DISABLED", "1")
    assert _stage3_disabled() is True

    monkeypatch.setenv("STAGE3_DISABLED", "true")
    assert _stage3_disabled() is True

    monkeypatch.setenv("STAGE3_DISABLED", "0")
    assert _stage3_disabled() is False


def _make_manager_with_disabled_flag() -> EpochManager:
    """Smallest EpochManager that exercises the gate entry point."""
    mgr = MagicMock(spec=EpochManager)
    # bind the real method so we test it through a mock self
    mgr._passes_regression_gate = EpochManager._passes_regression_gate.__get__(mgr)
    # Fields accessed before the env check exit are not reached; fill them
    # anyway for the cases that go past the early return.
    mgr._benchmark_worker = None
    mgr._sub_store = None
    mgr._champion = MagicMock(submission_id=None)
    return mgr


@pytest.mark.asyncio
async def test_disabled_env_returns_true_without_any_other_work(monkeypatch):
    """Row: STAGE3_DISABLED=1 → True. No other fields inspected."""
    monkeypatch.setenv("STAGE3_DISABLED", "1")
    mgr = _make_manager_with_disabled_flag()

    challenger = MagicMock(submission_id="sub_abc", image_tag=None)
    assert await mgr._passes_regression_gate(challenger, round_id="round-1") is True


@pytest.mark.asyncio
async def test_no_candidates_returns_true(monkeypatch):
    """Row: enabled + no regression candidates → True (nothing to test)."""
    monkeypatch.delenv("STAGE3_DISABLED", raising=False)
    mgr = _make_manager_with_disabled_flag()

    # Provide the collaborators needed to reach candidate extraction.
    mgr._benchmark_worker = MagicMock()
    mgr._benchmark_worker._load_benchmark_intents = MagicMock(return_value=[])
    mgr._sub_store = MagicMock()
    mgr._sub_store.get = MagicMock(return_value=MagicMock(image_tag="incumbent:v1"))
    mgr._champion = MagicMock(submission_id="sub_champ")

    challenger = MagicMock(
        submission_id="sub_new",
        image_tag="challenger:v1",
        benchmark_details={"results": []},  # no historical-failing scenarios
    )
    mgr._app_store = MagicMock()
    mgr._app_store.list_apps = MagicMock(return_value=[])
    assert await mgr._passes_regression_gate(challenger, round_id="round-1") is True


@pytest.mark.asyncio
async def test_candidates_plus_missing_archive_fails_closed(monkeypatch):
    """Row: enabled + candidates + archive missing → False."""
    monkeypatch.delenv("STAGE3_DISABLED", raising=False)

    # Build a more realistic mock: candidate extraction sees a failed
    # historical order, archive RPC says no for that chain.
    mgr = _make_manager_with_disabled_flag()
    mgr._benchmark_worker = MagicMock()
    mgr._sub_store = MagicMock()
    mgr._sub_store.get = MagicMock(return_value=MagicMock(image_tag="incumbent:v1"))
    mgr._champion = MagicMock(submission_id="sub_champ")

    # One failing historical scenario for chain 8453.
    mgr._app_store = MagicMock()
    mgr._app_store.get_order = MagicMock(return_value={
        "block_number": 123456,
        "chain_id": 8453,
        "params": {},
        "app_id": "app_1",
    })
    mgr._app_store.list_apps = MagicMock(return_value=[])

    challenger = MagicMock(
        submission_id="sub_new",
        image_tag="challenger:v1",
        benchmark_details={
            "results": [
                {"intent_id": "app_1:hist:ord_xyz", "score": 0, "error": "failed"},
            ],
        },
    )

    with patch(
        "minotaur_subnet.harness.historical_fork.archive_rpc_available",
        return_value=False,
    ):
        assert await mgr._passes_regression_gate(challenger, round_id="r1") is False


@pytest.mark.asyncio
async def test_disabled_short_circuits_before_archive_check(monkeypatch):
    """Row: STAGE3_DISABLED=1 + missing archive + candidates → True.

    Even with candidates that would otherwise require an archive RPC,
    the disabled flag must short-circuit and return True so operators
    know what "disabled" means unambiguously.
    """
    monkeypatch.setenv("STAGE3_DISABLED", "1")
    mgr = _make_manager_with_disabled_flag()

    challenger = MagicMock(
        submission_id="sub_new",
        image_tag="challenger:v1",
        benchmark_details={
            "results": [
                {"intent_id": "app_1:hist:ord_xyz", "score": 0, "error": "failed"},
            ],
        },
    )

    # archive_rpc_available must not be called — disabled short-circuits.
    with patch(
        "minotaur_subnet.harness.historical_fork.archive_rpc_available",
    ) as arch:
        assert await mgr._passes_regression_gate(challenger, round_id="r1") is True
        arch.assert_not_called()
