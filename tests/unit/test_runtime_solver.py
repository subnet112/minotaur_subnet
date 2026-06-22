"""Unit tests for DockerRuntimeSolver adapter."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
from minotaur_subnet.sdk.intent_solver import SolverMetadata
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    AppIntentConfig,
    ExecutionPlan,
    Interaction,
    IntentState,
)


@pytest.mark.asyncio
async def test_create_initializes_docker_session():
    session = AsyncMock()
    # Explicit ``_closed = False`` is required since DockerRuntimeSolver
    # added auto-respawn — without it AsyncMock auto-attrs ``_closed`` to
    # a truthy MagicMock, the runtime treats the session as dead, and
    # ``_call_with_respawn`` tries to spawn a real Docker container.
    session._closed = False
    session.metadata.return_value = SolverMetadata(
        name="champion",
        version="1.2.3",
        author="miner",
    )

    with patch(
        "minotaur_subnet.harness.runtime_solver.SolverOrchestrator.start_docker",
        new=AsyncMock(return_value=session),
    ):
        solver = await DockerRuntimeSolver.create(
            image_ref="sha256:" + "a" * 64,
            chain_ids=[1, 31337],
            rpc_urls={1: "http://anvil"},
        )

    session.initialize.assert_awaited_once()
    init_cfg = session.initialize.await_args.args[0]
    assert init_cfg["chain_ids"] == [1, 31337]
    assert init_cfg["rpc_urls"] == {1: "http://anvil"}
    assert solver.metadata().name == "champion"


@pytest.mark.asyncio
async def test_generate_plan_forwards_to_session():
    expected_plan = ExecutionPlan(
        intent_id="app_test",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0x",
                chain_id=1,
            ),
        ],
        deadline=9999999999,
        nonce=0,
        metadata={},
    )

    session = AsyncMock()
    session._closed = False  # see test_create_initializes_docker_session
    session.metadata.return_value = SolverMetadata(
        name="champion",
        version="1.2.3",
        author="miner",
    )
    session.generate_plan.return_value = expected_plan

    with patch(
        "minotaur_subnet.harness.runtime_solver.SolverOrchestrator.start_docker",
        new=AsyncMock(return_value=session),
    ):
        solver = await DockerRuntimeSolver.create(
            image_ref="sha256:" + "b" * 64,
            chain_ids=[1],
            rpc_urls={1: "http://anvil"},
        )

    intent = AppIntentDefinition(
        app_id="app_test",
        name="Test",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports={score:()=>({score:1,valid:true})}",
        config=AppIntentConfig(supported_chains=[1]),
    )
    state = IntentState(
        contract_address="0x" + "22" * 20,
        chain_id=1,
        nonce=7,
        owner="0x" + "33" * 20,
    )

    plan = await solver.generate_plan(intent, state, snapshot=None)
    assert plan == expected_plan
    session.generate_plan.assert_awaited_once()

    await solver.shutdown()
    session.shutdown.assert_awaited_once()


def test_reap_orphan_live_solvers_noop_when_none():
    from minotaur_subnet.harness import runtime_solver as rs

    fake_ps = AsyncMock()
    with patch("subprocess.run") as run:
        run.return_value = type("R", (), {"stdout": "", "returncode": 0})()
        assert rs._reap_orphan_live_solvers() == 0
    # One call (docker ps); no docker rm because there was nothing to reap.
    run.assert_called_once()
    assert run.call_args.args[0][:3] == ["docker", "ps", "-aq"]


def test_reap_orphan_live_solvers_removes_found():
    from minotaur_subnet.harness import runtime_solver as rs

    def fake_run(cmd, **_kw):
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return type("R", (), {"stdout": "abc\ndef\n", "returncode": 0})()
        assert cmd[:3] == ["docker", "rm", "-f"]
        assert cmd[3:] == ["abc", "def"]
        return type("R", (), {"stdout": "", "returncode": 0})()

    with patch("subprocess.run", side_effect=fake_run) as run:
        assert rs._reap_orphan_live_solvers() == 2
    assert run.call_count == 2


@pytest.mark.asyncio
async def test_create_reaps_orphans_and_labels_container():
    session = AsyncMock()
    session._closed = False  # see test_create_initializes_docker_session
    session.metadata.return_value = SolverMetadata(
        name="champion", version="1.0.0", author="m",
    )

    start_docker = AsyncMock(return_value=session)
    with patch(
        "minotaur_subnet.harness.runtime_solver.SolverOrchestrator.start_docker",
        new=start_docker,
    ), patch(
        "minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers",
    ) as reap:
        await DockerRuntimeSolver.create(
            image_ref="sha256:" + "c" * 64,
            chain_ids=[1],
            rpc_urls={1: "http://anvil"},
        )
    # Reaper fires exactly once before the new container comes up.
    reap.assert_called_once()
    # The container is labelled so the next boot's reaper can find it —
    # scoped to THIS instance's launcher id so sibling api instances don't
    # reap each other's in-use live solvers.
    from minotaur_subnet.harness.runtime_solver import _live_solver_launcher_id
    kwargs = start_docker.await_args.kwargs
    assert kwargs["labels"] == {
        "minotaur.role": "live-solver",
        "minotaur.launcher": _live_solver_launcher_id(),
    }
    assert kwargs["live"] is True


# ── FORCE_SOLVER_IMAGE operator break-glass (#digest-pin recovery lever) ───────

from minotaur_subnet.harness.runtime_solver import (  # noqa: E402
    forced_solver_image,
    resolve_boot_solver_image,
)


def test_forced_solver_image_unset_is_none(monkeypatch):
    monkeypatch.delenv("FORCE_SOLVER_IMAGE", raising=False)
    assert forced_solver_image() is None


def test_forced_solver_image_whitespace_is_none(monkeypatch):
    monkeypatch.setenv("FORCE_SOLVER_IMAGE", "   ")
    assert forced_solver_image() is None


def test_forced_solver_image_returns_ref(monkeypatch):
    ref = "ghcr.io/subnet112/minotaur-solver@sha256:" + "a" * 64
    monkeypatch.setenv("FORCE_SOLVER_IMAGE", "  " + ref + "  ")
    assert forced_solver_image() == ref  # stripped


def test_resolve_boot_prefers_force_over_genesis(monkeypatch):
    monkeypatch.setenv("GENESIS_SOLVER_IMAGE", "ghcr.io/x/solver:latest")
    monkeypatch.setenv("FORCE_SOLVER_IMAGE", "ghcr.io/x/solver:known-good")
    assert resolve_boot_solver_image() == ("ghcr.io/x/solver:known-good", True)


def test_resolve_boot_falls_back_to_genesis(monkeypatch):
    monkeypatch.delenv("FORCE_SOLVER_IMAGE", raising=False)
    monkeypatch.setenv("GENESIS_SOLVER_IMAGE", "ghcr.io/x/solver:latest")
    assert resolve_boot_solver_image() == ("ghcr.io/x/solver:latest", False)


def test_resolve_boot_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("FORCE_SOLVER_IMAGE", raising=False)
    monkeypatch.delenv("GENESIS_SOLVER_IMAGE", raising=False)
    assert resolve_boot_solver_image() == (None, False)
