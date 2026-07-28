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
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from minotaur_subnet.sdk import SDK_VERSION as SDK_VERSION_FROM_PACKAGE
from minotaur_subnet.sdk.intent_solver import IntentSolver, SolverMetadata
from minotaur_subnet.sdk.version import SDK_VERSION
from minotaur_subnet.harness.orchestrator import SolverSession
from minotaur_subnet.harness.protocol import HarnessResponse
from minotaur_subnet.harness.runner import SolverRunner
from minotaur_subnet.harness.screening import ScreeningResult, StageResult
from minotaur_subnet.harness.submission_store import SubmissionStore


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


class TestSubmissionRecord(unittest.TestCase):
    """`sdk_version` on the submission record, and its independence from the
    self-declared solver name/version."""

    def setUp(self):
        self.store = SubmissionStore()

    def _new(self, hotkey: str = "5GrwvaEF_test"):
        return self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey=hotkey,
        )

    def test_defaults_to_none(self):
        self.assertIsNone(self._new().sdk_version)

    def test_set_and_read_back(self):
        sub = self._new()
        self.store.set_sdk_version(sub.submission_id, "1.0.0")
        self.assertEqual(self.store.get(sub.submission_id).sdk_version, "1.0.0")

    def test_none_is_written_through_not_ignored(self):
        """A re-screen that reads pre-marker must CLEAR a stale version.

        None is the meaningful 'this vendored a pre-marker SDK' observation,
        so treating it as 'no update' would let a resubmit that downgraded its
        vendored SDK keep reporting the older, higher version.
        """
        sub = self._new()
        self.store.set_sdk_version(sub.submission_id, "1.0.0")
        self.store.set_sdk_version(sub.submission_id, None)
        self.assertIsNone(self.store.get(sub.submission_id).sdk_version)

    def test_set_solver_info_does_not_disturb_it(self):
        """The two are written independently.

        `set_solver_info` is guarded by a string-parse of screening prose and
        also drives copycat name-coining. If the version rode along with it,
        a malformed `details` would silently cost the observation — this pins
        that they do not interact.
        """
        sub = self._new()
        self.store.set_sdk_version(sub.submission_id, "1.0.0")
        self.store.set_solver_info(sub.submission_id, name="MySolver", version="9.9.9")
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.sdk_version, "1.0.0")
        self.assertEqual(updated.solver_name, "MySolver")
        self.assertEqual(updated.solver_version, "9.9.9")

    def test_serializes_into_record_dicts(self):
        sub = self._new()
        self.store.set_sdk_version(sub.submission_id, "1.0.0")
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.to_dict()["sdk_version"], "1.0.0")
        self.assertEqual(updated.status_dict()["sdk_version"], "1.0.0")

    def test_survives_persist_reload_round_trip(self):
        """Both the writer and the record reconstructor must carry the field.

        If either side drops it the value never survives a restart, and every
        consumer silently reads None — which is indistinguishable from a real
        pre-marker solver, so the population count would be quietly wrong
        rather than visibly broken.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "subs.json"
            store1 = SubmissionStore(persist_path=persist_path)
            sub = store1.create(
                repo_url="https://github.com/miner/solver",
                commit_hash="abc123",
                epoch=42,
                hotkey="5GrwvaEF_test",
            )
            store1.set_sdk_version(sub.submission_id, "1.0.0")

            store2 = SubmissionStore(persist_path=persist_path)
            self.assertEqual(store2.get(sub.submission_id).sdk_version, "1.0.0")


if __name__ == "__main__":
    unittest.main()
