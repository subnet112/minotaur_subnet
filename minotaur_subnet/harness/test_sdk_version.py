"""Tests for the SDK contract version marker.

The marker answers one question: which generation of the SDK contract did a
given solver vendor? The load-bearing property is that **absence is the
signal** — solvers built before the marker existed report no key at all, and
must read as pre-marker rather than as an error.

These tests pin the two directions that let it roll out across a fleet that
promotes unevenly (new solver ↔ old validator, old solver ↔ new validator),
because a regression in either would only show up as silently wrong
population counts, never as a crash.
"""

from __future__ import annotations

import io
import unittest
from dataclasses import asdict

from minotaur_subnet.sdk import SDK_VERSION as SDK_VERSION_FROM_PACKAGE
from minotaur_subnet.sdk.intent_solver import IntentSolver, SolverMetadata
from minotaur_subnet.sdk.version import SDK_VERSION
from minotaur_subnet.harness.orchestrator import SolverSession
from minotaur_subnet.harness.protocol import HarnessResponse
from minotaur_subnet.harness.runner import SolverRunner
from minotaur_subnet.harness.screening import ScreeningResult, StageResult


class _StubSolver(IntentSolver):
    """Minimal solver whose metadata we control, including a spoof attempt."""

    def __init__(self, declared_sdk_version: str | None = None) -> None:
        self._declared = declared_sdk_version

    def initialize(self, config):  # pragma: no cover - trivial
        return None

    def generate_plan(self, intent, state, snapshot):  # pragma: no cover
        return None

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="stub",
            version="1.2.3",
            author="tester",
            sdk_version=self._declared,
        )


def _runner_metadata(solver: IntentSolver) -> dict:
    runner = SolverRunner(solver, input_stream=io.StringIO(), output_stream=io.StringIO())
    return runner._handle_metadata({})


class TestSdkVersionConstant(unittest.TestCase):
    def test_exported_from_package_root(self):
        """Screening probes `minotaur_subnet.sdk`, so the re-export is load-bearing."""
        self.assertEqual(SDK_VERSION_FROM_PACKAGE, SDK_VERSION)

    def test_is_a_non_empty_string(self):
        self.assertIsInstance(SDK_VERSION, str)
        self.assertTrue(SDK_VERSION)


class TestRunnerInjection(unittest.TestCase):
    def test_runner_injects_vendored_version(self):
        d = _runner_metadata(_StubSolver())
        self.assertEqual(d["sdk_version"], SDK_VERSION)

    def test_injection_overwrites_miner_declared_value(self):
        """The marker must report what the code IS, not what it claims.

        A solver that hand-sets `sdk_version` in its own metadata() must not be
        able to pass itself off as a generation it did not vendor — otherwise
        the field is a self-report and worthless as a migration signal.
        """
        d = _runner_metadata(_StubSolver(declared_sdk_version="99.99.99"))
        self.assertEqual(d["sdk_version"], SDK_VERSION)

    def test_other_metadata_fields_survive_injection(self):
        d = _runner_metadata(_StubSolver())
        self.assertEqual(d["name"], "stub")
        self.assertEqual(d["version"], "1.2.3")
        self.assertEqual(d["author"], "tester")


class TestOrchestratorParsing(unittest.IsolatedAsyncioTestCase):
    """`SolverSession.metadata()` against hand-built wire payloads."""

    def _session_returning(self, result: dict) -> SolverSession:
        # Bypass __init__ — it wants a live subprocess, and metadata() touches
        # nothing but _send.
        session = object.__new__(SolverSession)

        async def _send(_request):
            return HarnessResponse(success=True, result=result)

        session._send = _send  # type: ignore[method-assign]
        return session

    async def test_reads_injected_version(self):
        session = self._session_returning({
            "name": "s", "version": "1.0.0", "author": "a",
            "sdk_version": "1.0.0",
        })
        meta = await session.metadata()
        self.assertEqual(meta.sdk_version, "1.0.0")

    async def test_absent_key_means_pre_marker(self):
        """Old solver → new validator. Absence must be None, not a raise."""
        session = self._session_returning({
            "name": "s", "version": "1.0.0", "author": "a",
        })
        meta = await session.metadata()
        self.assertIsNone(meta.sdk_version)

    async def test_unknown_keys_are_ignored(self):
        """New solver → old validator, simulated.

        metadata() rebuilds SolverMetadata field-by-field rather than
        `SolverMetadata(**r)`. If anyone ever "simplifies" it to the latter,
        a solver reporting a field this validator predates would raise
        TypeError and fail the whole benchmark — this test is the guard.
        """
        session = self._session_returning({
            "name": "s", "version": "1.0.0", "author": "a",
            "sdk_version": "2.0.0",
            "some_future_field": {"nested": True},
        })
        meta = await session.metadata()
        self.assertEqual(meta.sdk_version, "2.0.0")
        self.assertEqual(meta.name, "s")


class TestSolverMetadataDataclass(unittest.TestCase):
    def test_defaults_to_none(self):
        meta = SolverMetadata(name="n", version="v", author="a")
        self.assertIsNone(meta.sdk_version)

    def test_round_trips_through_asdict(self):
        meta = SolverMetadata(name="n", version="v", author="a", sdk_version="1.0.0")
        self.assertEqual(asdict(meta)["sdk_version"], "1.0.0")


class TestScreeningCarriers(unittest.TestCase):
    def test_stage_result_defaults_to_none(self):
        self.assertIsNone(StageResult(stage=2, passed=True).sdk_version)

    def test_screening_result_serializes_version(self):
        result = ScreeningResult(
            passed=True,
            stages=[StageResult(stage=2, passed=True, sdk_version="1.0.0")],
            solver_sdk_version="1.0.0",
        )
        d = result.to_dict()
        self.assertEqual(d["solver_sdk_version"], "1.0.0")
        self.assertEqual(d["stages"]["stage_2"]["sdk_version"], "1.0.0")

    def test_screening_result_serializes_pre_marker_as_none(self):
        result = ScreeningResult(passed=True, stages=[StageResult(stage=2, passed=True)])
        d = result.to_dict()
        self.assertIsNone(d["solver_sdk_version"])
        self.assertIsNone(d["stages"]["stage_2"]["sdk_version"])


if __name__ == "__main__":
    unittest.main()
