"""Tests for the submission API routes and submission store."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.submission_store import (
    Submission,
    SubmissionStatus,
    SubmissionStore,
)
from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    ChampionSnapshot,
    RoundStatus,
    RoundStore,
)


# ═══════════════════════════════════════════════════════════════════════════════
#                          SUBMISSION STORE TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubmissionStore(unittest.TestCase):
    """Tests for SubmissionStore in-memory operations."""

    def setUp(self):
        self.store = SubmissionStore()

    def test_create_submission(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123def456",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        self.assertTrue(sub.submission_id.startswith("sub_"))
        self.assertEqual(sub.status, SubmissionStatus.QUEUED)
        self.assertEqual(sub.epoch, 42)
        self.assertEqual(sub.repo_url, "https://github.com/miner/solver")
        self.assertIsNotNone(sub.created_at)

    def test_duplicate_submission_rejected(self):
        """One submission per hotkey per epoch."""
        self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        with self.assertRaises(ValueError) as ctx:
            self.store.create(
                repo_url="https://github.com/miner/solver2",
                commit_hash="def456",
                epoch=42,
                hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            )
        self.assertIn("already submitted", str(ctx.exception))

    def test_different_epoch_allowed(self):
        """Same hotkey can submit for different epochs."""
        s1 = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        s2 = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="def456",
            epoch=43,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        self.assertNotEqual(s1.submission_id, s2.submission_id)

    def test_same_hotkey_same_epoch_different_round_allowed(self):
        """Round ID, not epoch, is the primary duplicate boundary."""
        s1 = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            round_id="round-a",
        )
        s2 = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="def456",
            epoch=42,
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            round_id="round-b",
        )
        self.assertNotEqual(s1.submission_id, s2.submission_id)

    def test_max_per_round_default_is_one(self):
        """Default cap is 1: a second submission for the same round is rejected."""
        hk = "5GrwvaEF_cap"
        self.store.create(repo_url="r", commit_hash="c1", epoch=1, hotkey=hk, round_id="r-1")
        with self.assertRaises(ValueError) as ctx:
            self.store.create(repo_url="r", commit_hash="c2", epoch=1, hotkey=hk, round_id="r-1")
        self.assertIn("already submitted", str(ctx.exception))
        self.assertIn("max 1 per round", str(ctx.exception))

    def test_max_per_round_configurable_higher(self):
        """A cap of 3 allows three submissions for a round and rejects the fourth."""
        hk = "5GrwvaEF_cap3"
        for i in range(3):
            self.store.create(
                repo_url="r", commit_hash=f"c{i}", epoch=1, hotkey=hk,
                round_id="r-3", max_per_round=3,
            )
        self.assertEqual(self.store.count_by_hotkey_round(hk, "r-3"), 3)
        with self.assertRaises(ValueError):
            self.store.create(
                repo_url="r", commit_hash="c4", epoch=1, hotkey=hk,
                round_id="r-3", max_per_round=3,
            )

    def test_max_per_round_unlimited_when_non_positive(self):
        """max_per_round <= 0 disables the cap entirely."""
        hk = "5GrwvaEF_unl"
        for i in range(5):
            self.store.create(
                repo_url="r", commit_hash=f"u{i}", epoch=1, hotkey=hk,
                round_id="r-0", max_per_round=0,
            )
        self.assertEqual(self.store.count_by_hotkey_round(hk, "r-0"), 5)

    def test_count_by_hotkey_round_isolation(self):
        """The count is scoped per (hotkey, round) — other miners/rounds don't leak."""
        a, b = "5Gminer_A", "5Gminer_B"
        self.store.create(repo_url="r", commit_hash="a1", epoch=1, hotkey=a, round_id="r-1")
        self.store.create(
            repo_url="r", commit_hash="a2", epoch=1, hotkey=a, round_id="r-2", max_per_round=2,
        )
        self.store.create(repo_url="r", commit_hash="b1", epoch=1, hotkey=b, round_id="r-1")
        self.assertEqual(self.store.count_by_hotkey_round(a, "r-1"), 1)
        self.assertEqual(self.store.count_by_hotkey_round(a, "r-2"), 1)
        self.assertEqual(self.store.count_by_hotkey_round(b, "r-1"), 1)
        self.assertEqual(self.store.count_by_hotkey_round(a, "r-nope"), 0)

    def test_get_by_hotkey_round(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
            round_id="round-e42-n1",
        )
        found = self.store.get_by_hotkey_round("5GrwvaEF_test", "round-e42-n1")
        self.assertEqual(found.submission_id, sub.submission_id)

        not_found = self.store.get_by_hotkey_round("5GrwvaEF_test", "round-e42-n2")
        self.assertIsNone(not_found)

    def test_get_submission(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        fetched = self.store.get(sub.submission_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.submission_id, sub.submission_id)

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get("sub_doesnotexist"))

    def test_get_by_hotkey_epoch(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        found = self.store.get_by_hotkey_epoch("5GrwvaEF_test", 42)
        self.assertEqual(found.submission_id, sub.submission_id)

        not_found = self.store.get_by_hotkey_epoch("5GrwvaEF_test", 999)
        self.assertIsNone(not_found)

    def test_update_status(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_1)
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.SCREENING_STAGE_1)
        self.assertGreater(updated.updated_at, updated.created_at - 1)

    def test_update_nonexistent_raises(self):
        with self.assertRaises(KeyError):
            self.store.update_status("sub_nope", SubmissionStatus.SCREENING_STAGE_1)

    def test_set_screening_result_pass(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_screening_result(
            sub.submission_id,
            stage=1, passed=True, duration_ms=150,
            details="All static checks passed",
        )
        updated = self.store.get(sub.submission_id)
        self.assertTrue(updated.screening["stage_1"]["passed"])
        self.assertEqual(updated.screening["stage_1"]["duration_ms"], 150)
        # Status should NOT change on pass
        self.assertEqual(updated.status, SubmissionStatus.QUEUED)

    def test_set_screening_result_fail_rejects(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_screening_result(
            sub.submission_id,
            stage=1, passed=False, duration_ms=10,
            details="Missing required file: Dockerfile",
            error_code="missing_dockerfile",
        )
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.REJECTED)
        self.assertIn("Stage 1", updated.rejection_reason)
        self.assertIn("missing_dockerfile", updated.rejection_reason)

    def test_set_image_tag(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_image_tag(sub.submission_id, "solver-abc123:screening")
        self.assertEqual(self.store.get(sub.submission_id).image_tag, "solver-abc123:screening")

    def test_set_image_id(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "a" * 64)
        self.assertEqual(self.store.get(sub.submission_id).image_id, "sha256:" + "a" * 64)

    def test_set_provenance(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        provenance = {
            "alg": "hmac-sha256",
            "payload": {"submission_id": sub.submission_id},
            "signature": "deadbeef",
        }
        self.store.set_provenance(sub.submission_id, provenance)
        self.assertEqual(self.store.get(sub.submission_id).provenance, provenance)

    def test_set_solver_info(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_solver_info(sub.submission_id, name="MySolver", version="1.0.0")
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.solver_name, "MySolver")
        self.assertEqual(updated.solver_version, "1.0.0")

    def test_set_benchmark_result(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        details = {"per_intent": [{"intent_id": "app:scn", "raw_output": "1000"}]}
        self.store.set_benchmark_result(
            sub.submission_id,
            valid=True,
            rank=1,
            details=details,
        )
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.status, SubmissionStatus.SCORED)
        self.assertEqual(updated.benchmark_rank, 1)
        self.assertEqual(updated.benchmark_details, details)

    def test_reject_and_adopt(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.reject(sub.submission_id, "manual rejection")
        self.assertEqual(self.store.get(sub.submission_id).status, SubmissionStatus.REJECTED)

        # Create another and adopt it
        sub2 = self.store.create(
            repo_url="https://github.com/miner/solver2",
            commit_hash="def456",
            epoch=43,
            hotkey="5GrwvaEF_test",
        )
        self.store.adopt(sub2.submission_id)
        self.assertEqual(self.store.get(sub2.submission_id).status, SubmissionStatus.ADOPTED)

    def test_list_by_epoch(self):
        self.store.create(
            repo_url="https://github.com/a/s", commit_hash="a1",
            epoch=42, hotkey="miner_a",
        )
        self.store.create(
            repo_url="https://github.com/b/s", commit_hash="b1",
            epoch=42, hotkey="miner_b",
        )
        self.store.create(
            repo_url="https://github.com/c/s", commit_hash="c1",
            epoch=43, hotkey="miner_a",
        )

        epoch_42 = self.store.list_by_epoch(42)
        self.assertEqual(len(epoch_42), 2)

        epoch_43 = self.store.list_by_epoch(43)
        self.assertEqual(len(epoch_43), 1)

    def test_list_queued(self):
        s1 = self.store.create(
            repo_url="https://github.com/a/s", commit_hash="a1",
            epoch=42, hotkey="miner_a",
        )
        s2 = self.store.create(
            repo_url="https://github.com/b/s", commit_hash="b1",
            epoch=42, hotkey="miner_b",
        )
        self.store.update_status(s1.submission_id, SubmissionStatus.SCREENING_STAGE_1)

        queued = self.store.list_queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].submission_id, s2.submission_id)

    def test_set_solver_path(self):
        sub = self.store.create(
            repo_url="source://inline",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_solver_path(sub.submission_id, "/tmp/solver.py")
        updated = self.store.get(sub.submission_id)
        self.assertEqual(updated.solver_path, "/tmp/solver.py")

    def test_set_solver_path_nonexistent_raises(self):
        with self.assertRaises(KeyError):
            self.store.set_solver_path("sub_nope", "/tmp/solver.py")

    def test_solver_path_in_to_dict(self):
        sub = self.store.create(
            repo_url="source://inline",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        self.store.set_solver_path(sub.submission_id, "/tmp/solver.py")
        d = self.store.get(sub.submission_id).to_dict()
        self.assertEqual(d["solver_path"], "/tmp/solver.py")

    def test_solver_path_persists(self):
        """solver_path survives JSON persistence and reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "subs.json"
            store1 = SubmissionStore(persist_path=persist_path)
            sub = store1.create(
                repo_url="source://inline",
                commit_hash="abc123",
                epoch=42,
                hotkey="5GrwvaEF_test",
            )
            store1.set_solver_path(sub.submission_id, "/tmp/solver.py")

            store2 = SubmissionStore(persist_path=persist_path)
            loaded = store2.get(sub.submission_id)
            self.assertEqual(loaded.solver_path, "/tmp/solver.py")

    def test_to_dict(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        d = sub.to_dict()
        self.assertEqual(d["submission_id"], sub.submission_id)
        self.assertIn("round_id", d)
        self.assertEqual(d["status"], "queued")
        self.assertIn("screening", d)
        self.assertIn("stage_1", d["screening"])

    def test_status_dict(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
        )
        d = sub.status_dict()
        self.assertEqual(d["submission_id"], sub.submission_id)
        self.assertIn("round_id", d)
        self.assertNotIn("repo_url", d)
        self.assertNotIn("hotkey", d)


class TestSubmissionStorePersistence(unittest.TestCase):
    """Tests for JSON file persistence."""

    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "submissions.json"

            # Create store and add submissions
            store1 = SubmissionStore(persist_path=persist_path)
            s1 = store1.create(
                repo_url="https://github.com/miner/solver",
                commit_hash="abc123",
                epoch=42,
                hotkey="5GrwvaEF_test",
            )
            store1.set_screening_result(
                s1.submission_id, stage=1, passed=True,
                duration_ms=100, details="OK",
            )
            store1.set_image_tag(s1.submission_id, "solver-abc:test")
            store1.set_image_id(s1.submission_id, "sha256:" + "f" * 64)
            store1.set_provenance(
                s1.submission_id,
                {
                    "alg": "hmac-sha256",
                    "payload": {"submission_id": s1.submission_id},
                    "signature": "cafebabe",
                },
            )

            # Verify file was written
            self.assertTrue(persist_path.exists())

            # Load into new store
            store2 = SubmissionStore(persist_path=persist_path)
            loaded = store2.get(s1.submission_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.repo_url, "https://github.com/miner/solver")
            self.assertEqual(loaded.image_tag, "solver-abc:test")
            self.assertEqual(loaded.image_id, "sha256:" + "f" * 64)
            self.assertIsNotNone(loaded.provenance)
            self.assertEqual(loaded.provenance.get("signature"), "cafebabe")
            self.assertTrue(loaded.screening["stage_1"]["passed"])

    def test_duplicate_check_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "submissions.json"

            store1 = SubmissionStore(persist_path=persist_path)
            store1.create(
                repo_url="https://github.com/miner/solver",
                commit_hash="abc123",
                epoch=42,
                hotkey="5GrwvaEF_test",
            )

            # New store loaded from disk should block duplicates
            store2 = SubmissionStore(persist_path=persist_path)
            with self.assertRaises(ValueError):
                store2.create(
                    repo_url="https://github.com/miner/solver",
                    commit_hash="def456",
                    epoch=42,
                    hotkey="5GrwvaEF_test",
                )

    def test_concurrent_create_dedup_across_processes(self):
        """Concurrent creates on a shared file must accept exactly one.

        Each ``SubmissionStore`` opens its own advisory-lock fd, and separate
        open-file descriptions contend under ``flock`` exactly as separate
        processes do — so several stores + a barrier reproduce the
        multi-worker race the lock is meant to close. Without the cross-process
        guard, several creates pass the per-round cap before any persists and
        the whole-file rewrites clobber each other; with it, exactly one wins.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "submissions.json"
            workers = 8
            stores = [
                SubmissionStore(persist_path=persist_path) for _ in range(workers)
            ]
            barrier = threading.Barrier(workers)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def attempt(store: SubmissionStore) -> None:
                barrier.wait()  # release all threads onto the check-and-write together
                try:
                    store.create(
                        repo_url="source://inline",
                        commit_hash="abc123",
                        epoch=7,
                        hotkey="5GrwvaEF_test",
                        round_id="round-e7-n1",
                    )
                    outcome = "created"
                except ValueError:
                    outcome = "rejected"
                with outcomes_lock:
                    outcomes.append(outcome)

            threads = [
                threading.Thread(target=attempt, args=(store,)) for store in stores
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(outcomes.count("created"), 1, outcomes)
            self.assertEqual(outcomes.count("rejected"), workers - 1, outcomes)

            # The persisted file must hold exactly one submission for the round.
            verifier = SubmissionStore(persist_path=persist_path)
            self.assertEqual(len(verifier.list_by_round("round-e7-n1")), 1)


# ═══════════════════════════════════════════════════════════════════════════════
#                          API ROUTE TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubmissionAPI(unittest.TestCase):
    """Tests for the FastAPI submission endpoints using TestClient."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            raise unittest.SkipTest("fastapi[testclient] not available")

    def setUp(self):
        from fastapi.testclient import TestClient
        from minotaur_subnet.api.routes import submissions as sub_mod

        # Disable benchmark worker during tests
        os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
        os.environ["SUBMISSIONS_ACCEPTING"] = "1"
        os.environ["ENABLE_SOURCE_SUBMISSIONS"] = "1"
        os.environ["SUBMISSIONS_RATE_LIMIT_PER_MINUTE"] = "0"
        # M1 (2026-05-25 audit) made the metagraph gate fail CLOSED.
        # Test fixtures don't wire a real metagraph, so opt into the
        # operator-override env for the duration of these tests.
        os.environ["SUBMISSIONS_ALLOW_UNREGISTERED"] = "1"
        os.environ.pop("SUBMISSIONS_API_KEY", None)
        os.environ["SOLVER_ROUND_INTERNAL_API_KEY"] = "internal-secret"
        os.environ.pop("SOLVER_ROUND_EPOCH_SECONDS", None)
        os.environ.pop("SOLVER_ROUND_EPOCH_BLOCKS", None)
        os.environ.pop("ALLOW_INSECURE_REPO_URLS", None)
        os.environ.pop("ALLOW_FILE_REPO_URLS", None)
        os.environ.pop("SUBMISSION_ALLOWED_REPO_HOSTS", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_USERNAME", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_PASSWORD", None)

        # Use a fresh in-memory store for each test
        self.store = SubmissionStore()
        self.round_store = RoundStore()
        sub_mod.set_store(self.store)
        sub_mod.set_round_store(self.round_store)
        sub_mod.set_epoch_manager(None)
        sub_mod.set_champion_consensus_manager(None)
        sub_mod.set_champion_peer_network(None)
        sub_mod.set_solver_round_epoch_provider(None)

        # Reset the global champion-proposal rate-limiter between tests. It is
        # module-level state keyed by (signer-or-client-IP, round_id); without
        # this clear, a second unsigned POST from the shared TestClient IP is
        # rate-limited before reaching the round-state logic under test.
        from minotaur_subnet.api.routes.submissions.routes import (
            _CHAMPION_PROPOSAL_LAST_SEEN,
        )
        _CHAMPION_PROPOSAL_LAST_SEEN.clear()

        # Mock signature verification to always pass (unless testing sig failure)
        self._sig_patcher = patch(
            "minotaur_subnet.api.routes.submissions.routes.verify_hotkey_signature",
            return_value=True,
        )
        self._sig_patcher.start()

        # Mock PR resolution: create_submission resolves a solver-repo PR number to
        # the fork clone_url + head SHA without hitting GitHub. head_sha matches the
        # "a"*40 used in the submission bodies below so the force-push guard passes.
        self._pr_patcher = patch(
            "minotaur_subnet.api.routes.submissions.github_pr.resolve_pr",
            return_value={
                "clone_url": "https://github.com/miner/minotaur-solver.git",
                "head_sha": "a" * 40,
                "state": "open",
                "base": "subnet112/minotaur-solver",
            },
        )
        self._pr_patcher.start()

        # Import the app after setting the store
        from minotaur_subnet.api.server import app
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("SUBMISSIONS_API_KEY", None)
        os.environ.pop("SUBMISSIONS_ALLOW_UNREGISTERED", None)
        os.environ.pop("SOLVER_ROUND_INTERNAL_API_KEY", None)
        os.environ.pop("SOLVER_ROUND_EPOCH_SECONDS", None)
        os.environ.pop("SOLVER_ROUND_EPOCH_BLOCKS", None)
        os.environ.pop("ALLOW_INSECURE_REPO_URLS", None)
        os.environ.pop("ALLOW_FILE_REPO_URLS", None)
        os.environ.pop("SUBMISSION_ALLOWED_REPO_HOSTS", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_USERNAME", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_PASSWORD", None)
        from minotaur_subnet.api.routes import submissions as sub_mod
        sub_mod.set_epoch_manager(None)
        sub_mod.set_champion_consensus_manager(None)
        sub_mod.set_champion_peer_network(None)
        sub_mod.set_solver_round_epoch_provider(None)
        self._sig_patcher.stop()
        self._pr_patcher.stop()

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("solver_round_role", data)
        self.assertIn("champion_consensus", data)
        self.assertIn("provenance_policy", data)
        self.assertIsInstance(data["provenance_policy"]["startup_validated"], bool)
        self.assertIn("runtime_security_policy", data)
        self.assertIsInstance(data["runtime_security_policy"]["startup_validated"], bool)

    def test_solver_round_endpoint(self):
        resp = self.client.get("/v1/solver/round")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["round_id"].startswith("round-e0-"))
        self.assertEqual(data["status"], "open")
        self.assertTrue(data["accepting_submissions"])

    def test_solver_round_by_id_endpoint(self):
        current = self.round_store.ensure_open_round(opened_epoch=42)

        resp = self.client.get(f"/v1/solver/round/{current.round_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["round_id"], current.round_id)
        self.assertEqual(data["status"], "open")

    def test_solver_round_by_id_not_found(self):
        resp = self.client.get("/v1/solver/round/round-missing")
        self.assertEqual(resp.status_code, 404)

    def test_close_solver_round_endpoint(self):
        current = self.round_store.ensure_open_round(opened_epoch=42)

        resp = self.client.post("/v1/solver/round/close", json={
            "round_id": current.round_id,
            "close_epoch": 43,
            "benchmark_pack_hash": "pack-43",
            "committee_hash": "committee-43",
            "quorum_required": 2,
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["round_id"], current.round_id)
        self.assertEqual(data["status"], "closed")
        self.assertEqual(data["close_epoch"], 43)
        self.assertEqual(data["benchmark_pack_hash"], "pack-43")
        self.assertEqual(data["committee_hash"], "committee-43")
        self.assertEqual(data["quorum_required"], 2)

    def test_close_solver_round_endpoint_broadcasts_internal_sync(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        peer_network = MagicMock()
        peer_network.peers = [SimpleNamespace(validator_id="0xpeer")]
        peer_network.broadcast_json = AsyncMock(return_value=[{"status": "closed"}])
        sub_mod.set_champion_peer_network(peer_network)

        resp = self.client.post("/v1/solver/round/close", json={
            "round_id": current.round_id,
            "close_epoch": 43,
            "benchmark_pack_hash": "pack-43",
            "committee_hash": "committee-43",
            "quorum_required": 2,
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        peer_network.broadcast_json.assert_awaited_once()
        path, payload = peer_network.broadcast_json.await_args.args
        self.assertEqual(path, "/v1/solver/round/internal/close")
        self.assertEqual(payload["round_id"], current.round_id)
        self.assertEqual(payload["close_epoch"], 43)

    def test_certify_solver_round_endpoint(self):
        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "c" * 64)
        self.store.set_solver_info(sub.submission_id, name="solver-final", version="1.0.0")
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )

        resp = self.client.post("/v1/solver/round/certify", json={
            "round_id": current.round_id,
            "candidate_submission_id": sub.submission_id,
            "effective_epoch": 44,
            "quorum_required": 1,
            "approvals": [
                {
                    "validator_id": "0xabc",
                    "signature": "sig",
                },
            ],
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "certified")
        self.assertEqual(data["certificate_candidate_submission_id"], sub.submission_id)
        self.assertEqual(data["certificate_quorum_required"], 1)
        self.assertEqual(data["certificate_approvals"], 1)

    def test_certify_solver_round_requires_internal_key(self):
        # Public operator certify route must reject when no internal key header is sent.
        os.environ["SOLVER_ROUND_INTERNAL_API_KEY"] = "internal-secret"
        resp = self.client.post("/v1/solver/round/certify", json={
            "round_id": "round-x", "effective_epoch": 1,
        })
        self.assertEqual(resp.status_code, 401)

    def test_certify_solver_round_endpoint_rejects_after_deadline(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
            decision_deadline_epoch=43,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "c" * 64)
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )
        sub_mod.set_solver_round_epoch_provider(lambda: 44)

        resp = self.client.post("/v1/solver/round/certify", json={
            "round_id": current.round_id,
            "candidate_submission_id": sub.submission_id,
            "effective_epoch": 44,
            "quorum_required": 1,
            "approvals": [
                {
                    "validator_id": "0xabc",
                    "signature": "sig",
                },
            ],
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 409)
        self.assertIn("exceeded certification deadline", resp.json()["detail"])

    def test_certify_solver_round_uses_consensus_when_no_manual_approvals(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
            effective_epoch=44,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "c" * 64)
        self.store.set_solver_info(sub.submission_id, name="solver-final", version="1.0.0")
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )
        certificate = ChampionCertificate(
            round_id=current.round_id,
            committee_hash="committee-43",
            candidate_submission_id=sub.submission_id,
            candidate_image_id=sub.image_id,
            incumbent_image_id=None,
            benchmark_pack_hash="pack-43",
            effective_epoch=44,
            quorum_required=1,
            approvals=[
                ChampionApproval(
                    validator_id="0xabc",
                    round_id=current.round_id,
                    candidate_submission_id=sub.submission_id,
                    candidate_image_id=sub.image_id,
                    effective_epoch=44,
                    signature="sig",
                ),
            ],
        )
        consensus_manager = MagicMock()
        consensus_manager.quorum_required = 1
        consensus_manager.propose = AsyncMock(
            return_value=SimpleNamespace(
                reached=True,
                certificate=certificate,
                collected=1,
                quorum=1,
            )
        )
        peer_network = MagicMock()
        peer_network.broadcast_champion_proposal = AsyncMock(return_value=[])
        sub_mod.set_champion_consensus_manager(consensus_manager)
        sub_mod.set_champion_peer_network(peer_network)

        resp = self.client.post("/v1/solver/round/certify", json={
            "round_id": current.round_id,
            "candidate_submission_id": sub.submission_id,
            "effective_epoch": 44,
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "certified")
        self.assertEqual(data["certificate_candidate_submission_id"], sub.submission_id)
        consensus_manager.propose.assert_awaited_once()
        # broadcast_champion_proposal now fires TWICE: once for consensus
        # (collector=consensus_manager) and once from the post-cert best-effort
        # quorum MONITOR (collector=None, read-only, added after this test). Assert
        # the consensus broadcast happened exactly once. Use call_args_list (not
        # await_args_list): the consensus broadcast is fired via asyncio.create_task,
        # so it is CALLED (coroutine created) but not necessarily awaited in-test.
        consensus_broadcasts = [
            c for c in peer_network.broadcast_champion_proposal.call_args_list
            if c.kwargs.get("collector") is consensus_manager
        ]
        self.assertEqual(len(consensus_broadcasts), 1)

    def test_solver_round_consensus_proposal_endpoint_signs_matching_tuple(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
            effective_epoch=44,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "e" * 64)
        self.store.set_solver_info(sub.submission_id, name="solver-final", version="1.0.0")
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )
        approval = ChampionApproval(
            validator_id="0xvalidator",
            round_id=current.round_id,
            committee_hash="committee-43",
            incumbent_image_id=None,
            candidate_submission_id=sub.submission_id,
            candidate_image_id=sub.image_id,
            benchmark_pack_hash="pack-43",
            shadow_case_log_hash=None,
            effective_epoch=44,
            signature="sig",
            timestamp=123.0,
        )
        consensus_manager = MagicMock()
        consensus_manager.quorum_required = 1
        consensus_manager.sign_approval.return_value = approval
        sub_mod.set_champion_consensus_manager(consensus_manager)

        # The EIP-712 signature is now the sole cross-validator auth for this
        # route (the internal-key gate was removed). This test POSTs an
        # unsigned body and stays focused on the round-state -> sign path, so
        # stub the signature check to pass. The pack-hash pre-flight and the
        # reactive Docker benchmark are independent downstream gates exercised
        # elsewhere; stub them so we reach sign_approval.
        with patch(
            "minotaur_subnet.api.routes.submissions.routes."
            "_verify_champion_proposal_signature",
            return_value=None,
        ), patch(
            "minotaur_subnet.api.startup."
            "_build_solver_round_benchmark_pack_hash",
            return_value="pack-43",
        ), patch(
            "minotaur_subnet.api.routes.submissions.routes."
            "_reactive_benchmark_candidate",
            new=AsyncMock(
                return_value=(True, {"better": 1, "worse": 0, "matched": 0, "compared": 1})
            ),
        ):
            resp = self.client.post("/v1/solver/round/consensus/proposal", json={
                "round_id": current.round_id,
                "candidate_submission_id": sub.submission_id,
                "candidate_image_id": sub.image_id,
                "committee_hash": "committee-43",
                "benchmark_pack_hash": "pack-43",
                "effective_epoch": 44,
                "close_epoch": 43,
                "quorum_required": 1,
            })

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["approved"])
        self.assertEqual(data["validator_id"], "0xvalidator")
        self.assertEqual(data["candidate_submission_id"], sub.submission_id)
        consensus_manager.sign_approval.assert_called_once()

    def test_solver_round_consensus_proposal_rejects_after_deadline(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
            decision_deadline_epoch=43,
            effective_epoch=44,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "e" * 64)
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )
        consensus_manager = MagicMock()
        sub_mod.set_champion_consensus_manager(consensus_manager)
        sub_mod.set_solver_round_epoch_provider(lambda: 44)

        # Unsigned body — stub the signature check (now the sole auth) so the
        # test stays focused on the deadline-rejection round-state logic.
        with patch(
            "minotaur_subnet.api.routes.submissions.routes."
            "_verify_champion_proposal_signature",
            return_value=None,
        ):
            resp = self.client.post("/v1/solver/round/consensus/proposal", json={
                "round_id": current.round_id,
                "candidate_submission_id": sub.submission_id,
                "candidate_image_id": sub.image_id,
                "committee_hash": "committee-43",
                "benchmark_pack_hash": "pack-43",
                "effective_epoch": 44,
                "close_epoch": 43,
                "quorum_required": 1,
                "decision_deadline_epoch": 43,
            })

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["approved"])
        self.assertIn("exceeded certification deadline", data["reason"])

    def test_internal_close_solver_round_requires_eip712(self):
        # The cross-validator /internal/ close receiver is EIP-712 ONLY: the legacy
        # shared key is no longer a fallback here (it never worked across operators).
        # The operator /solver/round/close keeps the shared key (tested elsewhere).
        os.environ["SOLVER_ROUND_INTERNAL_API_KEY"] = "internal-secret"
        current = self.round_store.ensure_open_round(opened_epoch=42)
        body = {
            "round_id": current.round_id, "close_epoch": 43,
            "benchmark_pack_hash": "pack-43", "committee_hash": "committee-43",
            "quorum_required": 1,
        }
        # no auth -> 401
        self.assertEqual(
            self.client.post("/v1/solver/round/internal/close", json=body).status_code, 401
        )
        # shared-key header alone -> 401 (no fallback on /internal/); requires a signature
        keyed = self.client.post(
            "/v1/solver/round/internal/close", json=body,
            headers={"x-solver-round-internal-key": "internal-secret"},
        )
        self.assertEqual(keyed.status_code, 401)
        self.assertIn("eip-712", keyed.json()["detail"].lower())

    def test_internal_abort_solver_round_requires_eip712(self):
        os.environ["SOLVER_ROUND_INTERNAL_API_KEY"] = "internal-secret"
        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(close_epoch=43)
        body = {"round_id": current.round_id, "reason": "no_champion_candidate"}
        # no auth -> 401
        self.assertEqual(
            self.client.post("/v1/solver/round/internal/abort", json=body).status_code, 401
        )
        # shared-key header alone -> 401 (EIP-712 only on /internal/)
        keyed = self.client.post(
            "/v1/solver/round/internal/abort", json=body,
            headers={"x-solver-round-internal-key": "internal-secret"},
        )
        self.assertEqual(keyed.status_code, 401)
        self.assertIn("eip-712", keyed.json()["detail"].lower())

    def test_abort_solver_round_endpoint(self):
        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(close_epoch=43)

        resp = self.client.post("/v1/solver/round/abort", json={
            "round_id": current.round_id,
            "reason": "operator_abort",
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], RoundStatus.ABORTED.value)
        self.assertEqual(data["abort_reason"], "operator_abort")

    def test_activate_solver_round_endpoint(self):
        from minotaur_subnet.api.routes import submissions as sub_mod

        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(
            close_epoch=43,
            benchmark_pack_hash="pack-43",
            committee_hash="committee-43",
            quorum_required=1,
        )
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id=current.round_id,
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "d" * 64)
        self.store.set_solver_info(sub.submission_id, name="solver-final", version="1.0.0")
        self.round_store.set_round_finalist(
            current.round_id,
            submission_id=sub.submission_id,
            image_id=sub.image_id,
        )
        self.round_store.certify_round(
            current.round_id,
            ChampionCertificate(
                round_id=current.round_id,
                committee_hash="committee-43",
                candidate_submission_id=sub.submission_id,
                candidate_image_id=sub.image_id,
                incumbent_image_id=None,
                benchmark_pack_hash="pack-43",
                effective_epoch=44,
                quorum_required=1,
                approvals=[
                    ChampionApproval(
                        validator_id="0xabc",
                        round_id=current.round_id,
                        candidate_submission_id=sub.submission_id,
                        candidate_image_id=sub.image_id,
                        effective_epoch=44,
                        signature="sig",
                    ),
                ],
            ),
        )
        sub_mod.set_epoch_manager(None)

        resp = self.client.post("/v1/solver/round/activate", json={
            "round_id": current.round_id,
            "activation_epoch": 44,
        }, headers={"x-solver-round-internal-key": "internal-secret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["champion_changed"])
        self.assertEqual(self.store.get(sub.submission_id).status, SubmissionStatus.ADOPTED)
        self.assertEqual(self.round_store.get_round(current.round_id).status, RoundStatus.ACTIVATED)
        self.assertEqual(self.round_store.get_current_round().status, RoundStatus.OPEN)

    def test_create_submission(self):
        resp = self.client.post("/v1/submissions", json={
            "pr_number": 1,
            "head_sha": "a" * 40,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("submission_id", data)
        self.assertEqual(data["status"], "queued")
        self.assertTrue(data["round_id"].startswith("round-e42-"))
        self.assertTrue(data["status_url"].startswith("/v1/submissions/sub_"))
        created = self.store.get(data["submission_id"])
        self.assertEqual(created.round_id, data["round_id"])
        self.assertEqual(created.epoch, 42)

    def test_create_submission_round_id_mismatch_returns_409(self):
        self.round_store.ensure_open_round(opened_epoch=42)
        resp = self.client.post("/v1/submissions", json={
            "pr_number": 1,
            "head_sha": "a" * 40,
            "round_id": "round-e42-n999",
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("current open round", resp.json()["detail"])

    def test_create_submission_rejected_when_round_closed(self):
        current = self.round_store.ensure_open_round(opened_epoch=42)
        self.round_store.close_current_round(close_epoch=43)

        resp = self.client.post("/v1/submissions", json={
            "pr_number": 1,
            "head_sha": "a" * 40,
            "round_id": current.round_id,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("not accepted", resp.json()["detail"])

    def test_create_duplicate_returns_409(self):
        body = {
            "pr_number": 1,
            "head_sha": "a" * 40,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        }
        self.client.post("/v1/submissions", json=body)
        resp = self.client.post("/v1/submissions", json=body)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already submitted", resp.json()["detail"])

    def test_create_missing_fields_returns_422(self):
        resp = self.client.post("/v1/submissions", json={
            "repo_url": "https://github.com/miner/solver",
        })
        self.assertEqual(resp.status_code, 422)

    def test_create_short_commit_returns_422(self):
        resp = self.client.post("/v1/submissions", json={
            "repo_url": "https://github.com/miner/solver",
            "commit_hash": "abc",  # too short (min 7)
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        })
        self.assertEqual(resp.status_code, 422)

    def test_invalid_signature_returns_401(self):
        """Submission with an invalid signature is rejected with 401."""
        self._sig_patcher.stop()  # Use real verification (will fail)
        with patch(
            "minotaur_subnet.api.routes.submissions.routes.verify_hotkey_signature",
            return_value=False,
        ):
            resp = self.client.post("/v1/submissions", json={
                "pr_number": 1,
                "head_sha": "a" * 40,
                "epoch": 42,
                "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                "signature": "bm90YXZhbGlkc2ln",
            })
            self.assertEqual(resp.status_code, 401)
            self.assertIn("Invalid hotkey signature", resp.json()["detail"])
        self._sig_patcher.start()  # Restore mock for tearDown

    def test_missing_signature_returns_422(self):
        """Submission without signature field returns 422 (required field)."""
        resp = self.client.post("/v1/submissions", json={
            "pr_number": 1,
            "head_sha": "a" * 40,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        })
        self.assertEqual(resp.status_code, 422)

    def test_get_status(self):
        # Create directly in the store
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
            round_id="round-e42-n1",
        )
        resp = self.client.get(f"/v1/submissions/{sub.submission_id}/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["submission_id"], sub.submission_id)
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["round_id"], "round-e42-n1")
        self.assertIn("screening", data)

    def test_get_status_not_found(self):
        resp = self.client.get("/v1/submissions/sub_doesnotexist/status")
        self.assertEqual(resp.status_code, 404)

    def test_get_status_with_screening_results(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5GrwvaEF_test",
            round_id="round-e42-n1",
        )
        self.store.set_screening_result(
            sub.submission_id, stage=1, passed=True,
            duration_ms=50, details="All OK",
        )
        self.store.update_status(sub.submission_id, SubmissionStatus.SCREENING_STAGE_2)

        resp = self.client.get(f"/v1/submissions/{sub.submission_id}/status")
        data = resp.json()
        self.assertEqual(data["status"], "screening_stage_2")
        self.assertTrue(data["screening"]["stage_1"]["passed"])
        self.assertEqual(data["screening"]["stage_1"]["duration_ms"], 50)

    def test_list_submissions_empty(self):
        resp = self.client.get("/v1/submissions")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["submissions"], [])

    def test_list_omits_benchmark_details_by_default(self):
        """The heavy benchmark_details blob is stripped from the list by default
        (it dominated the payload — ~16 MB for ~800 subs) but stays available via
        ?include_details=true and on the single-submission status endpoint."""
        sub = self.store.create(
            repo_url="https://github.com/a/s", commit_hash="aaaa123",
            epoch=42, hotkey="miner_aaaa", round_id="round-e42-n1",
        )
        sub.benchmark_details = {"per_intent": [{"intent_id": "x", "score": 1}], "huge": "blob"}

        # Default: omitted, but the light fields the dashboard uses remain.
        data = self.client.get("/v1/submissions").json()
        self.assertEqual(data["count"], 1)
        row = data["submissions"][0]
        self.assertNotIn("benchmark_details", row)
        self.assertEqual(row["round_id"], "round-e42-n1")
        self.assertEqual(row["submission_id"], sub.submission_id)

        # Opt-in: present and intact.
        data = self.client.get("/v1/submissions?include_details=true").json()
        self.assertEqual(
            data["submissions"][0]["benchmark_details"], sub.benchmark_details
        )

    def test_list_submissions_by_epoch(self):
        self.store.create(
            repo_url="https://github.com/a/s", commit_hash="aaaa123",
            epoch=42, hotkey="miner_aaaa", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/b/s", commit_hash="bbbb123",
            epoch=42, hotkey="miner_bbbb", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/c/s", commit_hash="cccc123",
            epoch=43, hotkey="miner_aaaa", round_id="round-e43-n1",
        )

        resp = self.client.get("/v1/submissions?epoch=42")
        data = resp.json()
        self.assertEqual(data["count"], 2)

        resp = self.client.get("/v1/submissions?epoch=43")
        data = resp.json()
        self.assertEqual(data["count"], 1)

    def test_list_submissions_by_hotkey(self):
        self.store.create(
            repo_url="https://github.com/a/s", commit_hash="aaaa123",
            epoch=42, hotkey="miner_aaaa", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/b/s", commit_hash="bbbb123",
            epoch=42, hotkey="miner_bbbb", round_id="round-e42-n1",
        )

        resp = self.client.get("/v1/submissions?hotkey=miner_aaaa")
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["submissions"][0]["hotkey"], "miner_aaaa")

    def test_list_submissions_by_epoch_and_hotkey(self):
        self.store.create(
            repo_url="https://github.com/a/s", commit_hash="aaaa123",
            epoch=42, hotkey="miner_aaaa", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/b/s", commit_hash="bbbb123",
            epoch=43, hotkey="miner_aaaa", round_id="round-e43-n1",
        )

        resp = self.client.get("/v1/submissions?epoch=42&hotkey=miner_aaaa")
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["submissions"][0]["epoch"], 42)

    def test_list_submissions_by_round_id(self):
        self.store.create(
            repo_url="https://github.com/a/s", commit_hash="aaaa123",
            epoch=42, hotkey="miner_aaaa", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/b/s", commit_hash="bbbb123",
            epoch=42, hotkey="miner_bbbb", round_id="round-e42-n1",
        )
        self.store.create(
            repo_url="https://github.com/c/s", commit_hash="cccc123",
            epoch=43, hotkey="miner_aaaa", round_id="round-e43-n1",
        )

        resp = self.client.get("/v1/submissions?round_id=round-e42-n1")
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertTrue(all(s["round_id"] == "round-e42-n1" for s in data["submissions"]))

    def test_source_submission_creates_and_queues(self):
        """POST /submissions/source creates a submission and sets it to BENCHMARKING."""
        solver_code = "class MySolver:\n    pass\n"
        resp = self.client.post("/v1/submissions/source", json={
            "solver_source": solver_code,
            "hotkey": "test-miner",
            "epoch": 0,
            "solver_name": "test-solver",
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("submission_id", data)
        self.assertEqual(data["status"], "benchmarking")
        self.assertTrue(data["round_id"].startswith("round-e0-"))
        self.assertTrue(data["status_url"].startswith("/v1/submissions/sub_"))

        # Verify internal state
        sub = self.store.get(data["submission_id"])
        self.assertEqual(sub.status, SubmissionStatus.BENCHMARKING)
        self.assertIsNotNone(sub.solver_path)
        self.assertTrue(sub.solver_path.endswith("solver.py"))
        self.assertEqual(sub.solver_name, "test-solver")
        self.assertEqual(sub.round_id, data["round_id"])

    def test_source_submission_duplicate_returns_409(self):
        """Duplicate source submissions for same hotkey+epoch return 409."""
        body = {
            "solver_source": "class X: pass",
            "hotkey": "dup-miner",
            "epoch": 0,
        }
        self.client.post("/v1/submissions/source", json=body)
        resp = self.client.post("/v1/submissions/source", json=body)
        self.assertEqual(resp.status_code, 409)

    def test_source_submission_disabled_returns_403(self):
        os.environ["ENABLE_SOURCE_SUBMISSIONS"] = "0"
        resp = self.client.post("/v1/submissions/source", json={
            "solver_source": "class X: pass",
            "hotkey": "test-miner",
            "epoch": 0,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn("disabled by policy", resp.json()["detail"])
        os.environ["ENABLE_SOURCE_SUBMISSIONS"] = "1"

    def test_submissions_accepting_kill_switch_returns_503(self):
        os.environ["SUBMISSIONS_ACCEPTING"] = "0"
        resp = self.client.post("/v1/submissions", json={
            "pr_number": 1,
            "head_sha": "a" * 40,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        })
        self.assertEqual(resp.status_code, 503)
        self.assertIn("temporarily disabled", resp.json()["detail"])
        os.environ["SUBMISSIONS_ACCEPTING"] = "1"

    def test_submission_api_key_required(self):
        os.environ["SUBMISSIONS_API_KEY"] = "secret-key"
        body = {
            "pr_number": 1,
            "head_sha": "a" * 40,
            "epoch": 42,
            "hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "signature": "dGVzdHNpZw==",
        }
        denied = self.client.post("/v1/submissions", json=body)
        self.assertEqual(denied.status_code, 401)
        allowed = self.client.post(
            "/v1/submissions",
            json=body,
            headers={"x-submission-api-key": "secret-key"},
        )
        self.assertEqual(allowed.status_code, 201)

    def test_solver_champion_endpoint_reflects_adopted_submission(self):
        sub = self.store.create(
            repo_url="https://github.com/miner/solver",
            commit_hash="abc123",
            epoch=42,
            hotkey="5Gchampion",
            round_id="round-e42-n1",
        )
        self.store.set_image_id(sub.submission_id, "sha256:" + "a" * 64)
        self.store.set_solver_info(sub.submission_id, name="solver-z", version="1.2.3")
        self.store.adopt(sub.submission_id)

        resp = self.client.get("/v1/solver/champion")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["submission_id"], sub.submission_id)
        self.assertEqual(data["image_id"], "sha256:" + "a" * 64)
        self.assertEqual(data["solver_name"], "solver-z")
        self.assertEqual(data["hotkey"], "5Gchampion")

    def test_solver_champion_endpoint_preserves_round_store_snapshot(self):
        self.round_store.set_active_champion(
            ChampionSnapshot(
                submission_id="sub_live",
                image_id="sha256:" + "b" * 64,
                solver_name="solver-live",
                solver_version="2.0.0",
                hotkey="5Glive",
                activated_round_id="round-e42-n1",
                activated_epoch=42,
                activated_at=123.0,
            ),
            sync_open_round=False,
        )

        resp = self.client.get("/v1/solver/champion")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["submission_id"], "sub_live")
        self.assertEqual(data["image_id"], "sha256:" + "b" * 64)
        self.assertEqual(data["solver_name"], "solver-live")
        self.assertEqual(data["activated_round_id"], "round-e42-n1")


# ═══════════════════════════════════════════════════════════════════════════════
#                          SCREENING PIPELINE INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestScreeningBackground(unittest.TestCase):
    """Tests for the background screening pipeline function."""

    def tearDown(self):
        os.environ.pop("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_USERNAME", None)
        os.environ.pop("SUBMISSION_GIT_CLONE_PASSWORD", None)

    def test_clone_repo_bad_url(self):
        """_clone_repo returns False for unreachable URLs."""
        import asyncio
        from minotaur_subnet.api.routes.submissions import _clone_repo

        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(
                _clone_repo(
                    "https://example.com/nonexistent/repo.git",
                    "abc123",
                    tmpdir,
                )
            )
            self.assertFalse(result)

    def test_build_git_process_env_scopes_private_credentials_by_host(self):
        from minotaur_subnet.api.routes.submissions import (
            _build_git_process_env,
            _cleanup_temp_file,
        )

        os.environ["SUBMISSION_GIT_CLONE_ALLOWED_HOSTS"] = "github.com"
        os.environ["SUBMISSION_GIT_CLONE_USERNAME"] = "x-access-token"
        os.environ["SUBMISSION_GIT_CLONE_PASSWORD"] = "demo-token"

        env, askpass_path = _build_git_process_env(
            "https://github.com/subnet112/minotaur-solver"
        )

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["MINOTAUR_GIT_CLONE_USERNAME"], "x-access-token")
        self.assertEqual(env["MINOTAUR_GIT_CLONE_PASSWORD"], "demo-token")
        self.assertIsNotNone(askpass_path)
        self.assertTrue(os.path.exists(askpass_path))

        _cleanup_temp_file(askpass_path)
        self.assertFalse(os.path.exists(askpass_path))

    def test_build_git_process_env_skips_unlisted_hosts(self):
        from minotaur_subnet.api.routes.submissions import _build_git_process_env

        os.environ["SUBMISSION_GIT_CLONE_ALLOWED_HOSTS"] = "github.com"
        os.environ["SUBMISSION_GIT_CLONE_USERNAME"] = "x-access-token"
        os.environ["SUBMISSION_GIT_CLONE_PASSWORD"] = "demo-token"

        env, askpass_path = _build_git_process_env("https://gitlab.com/team/private-solver")

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_ASKPASS", env)
        self.assertNotIn("MINOTAUR_GIT_CLONE_USERNAME", env)
        self.assertNotIn("MINOTAUR_GIT_CLONE_PASSWORD", env)
        self.assertIsNone(askpass_path)

    def test_build_git_process_env_allows_scoped_file_repo_clone(self):
        from minotaur_subnet.api.routes.submissions import (
            _build_git_process_env,
            _cleanup_temp_file,
        )

        env, helper_path = _build_git_process_env("file:///solver-submissions/local-repo")

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], helper_path)
        self.assertIsNotNone(helper_path)
        self.assertTrue(os.path.exists(helper_path))
        helper_contents = Path(helper_path).read_text()
        self.assertIn("directory = *", helper_contents)
        self.assertIn('[protocol "file"]', helper_contents)
        self.assertIn("allow = always", helper_contents)

        _cleanup_temp_file(helper_path)
        self.assertFalse(os.path.exists(helper_path))

    def test_clone_repo_sandboxed_passes_scoped_noninteractive_git_env(self):
        """https(s) clones run in the hardened docker sandbox (``_clone_repo_sandboxed``),
        not an in-process ``git clone``. Assert the sandbox's security properties: git is
        non-interactive (``GIT_TERMINAL_PROMPT=0``), and repo/commit/private-repo auth are
        passed via ENV (``REPO_URL``/``COMMIT``/``GIT_BASIC_AUTH`` → an ``http.extraHeader``),
        never on the argv where a secret could leak into logs/ps.

        (Rewritten from the old in-process assertion — http(s) no longer uses
        ``_build_git_process_env``/``GIT_ASKPASS``; that path is now file://-only.)"""
        import asyncio
        import base64

        from minotaur_subnet.api.routes.submissions import screening_pipeline as sp

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"<tarball-bytes>", b""

        os.environ["SUBMISSION_GIT_CLONE_ALLOWED_HOSTS"] = "github.com"
        os.environ["SUBMISSION_GIT_CLONE_USERNAME"] = "x-access-token"
        os.environ["SUBMISSION_GIT_CLONE_PASSWORD"] = "demo-token"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "minotaur_subnet.api.routes.submissions.screening_pipeline.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc()),
            ) as mock_exec, patch.object(sp, "_safe_extract_tar", return_value=True):
                result = asyncio.run(
                    sp._clone_repo(
                        "https://github.com/subnet112/minotaur-solver",
                        "abc1234",
                        tmpdir,
                    )
                )

        self.assertTrue(result)
        cmd = list(mock_exec.await_args.args)
        run_env = mock_exec.await_args.kwargs["env"]
        cmd_str = " ".join(str(c) for c in cmd)

        self.assertEqual(cmd[0], "docker")             # sandboxed, not host git
        self.assertIn("GIT_TERMINAL_PROMPT=0", cmd)    # non-interactive
        # Repo, commit, and auth ride the ENV — never the argv.
        self.assertEqual(run_env["REPO_URL"], "https://github.com/subnet112/minotaur-solver")
        self.assertEqual(run_env["COMMIT"], "abc1234")
        self.assertEqual(
            run_env["GIT_BASIC_AUTH"],
            base64.b64encode(b"x-access-token:demo-token").decode(),
        )
        self.assertNotIn("demo-token", cmd_str)        # secret never on the command line

    def test_pipeline_nonexistent_submission(self):
        """Pipeline handles missing submission gracefully."""
        import asyncio
        from minotaur_subnet.api.routes.submissions import (
            _run_screening_pipeline,
            set_store,
        )

        store = SubmissionStore()
        set_store(store)

        # Should not raise
        asyncio.run(
            _run_screening_pipeline("sub_doesnotexist")
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                  CANDIDATE PUSH DIGEST-VERIFY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestPushDigestVerify(unittest.TestCase):
    """Post-push manifest-retrievability verify (anti spurious-REJECT-on-pull).

    A push can return rc=0 with the manifest not yet registry-retrievable (the
    containerd image store reads RepoDigests locally), so the leader must verify
    <repo>@sha256:D is actually pullable — re-pushing if not — before proposing
    it, else fail closed so a follower never 404s on the candidate image.
    """

    DIGEST_REF = "ghcr.io/subnet112/minotaur-solver@sha256:" + "a" * 64
    REF = "ghcr.io/subnet112/minotaur-solver:pr-9"

    def _drive(self, inspect_rcs):
        """Run the verify loop with a fake docker runner scripted to return the
        given rc sequence for `manifest inspect`; record every docker call."""
        from minotaur_subnet.api.routes.submissions.screening_pipeline import (
            _verify_digest_retrievable,
        )
        calls = []
        rcs = iter(inspect_rcs)

        async def fake_docker(*args, timeout):
            calls.append(args)
            if args[:2] == ("manifest", "inspect"):
                return (next(rcs), "")
            return (0, "")  # push / tag / anything else

        async def no_sleep(_seconds):
            return None

        result = asyncio.run(
            _verify_digest_retrievable(
                self.REF, self.DIGEST_REF, fake_docker,
                attempts=5, sleep_fn=no_sleep,
            )
        )
        inspects = sum(1 for c in calls if c[:2] == ("manifest", "inspect"))
        pushes = sum(1 for c in calls if c[0] == "push")
        return result, inspects, pushes

    def test_returns_digest_when_immediately_retrievable(self):
        result, inspects, pushes = self._drive([0])
        self.assertEqual(result, self.DIGEST_REF)
        self.assertEqual(inspects, 1)
        self.assertEqual(pushes, 0)  # no re-push needed

    def test_repushes_then_succeeds(self):
        result, inspects, pushes = self._drive([1, 1, 0])
        self.assertEqual(result, self.DIGEST_REF)
        self.assertEqual(inspects, 3)
        self.assertEqual(pushes, 2)  # one re-push before each retry

    def test_fails_closed_when_never_retrievable(self):
        result, inspects, pushes = self._drive([1, 1, 1, 1, 1])
        self.assertIsNone(result)  # never propose an unpullable digest
        self.assertEqual(inspects, 5)  # attempts
        self.assertEqual(pushes, 4)  # attempts - 1 re-pushes


# ═══════════════════════════════════════════════════════════════════════════════
#                  REGISTERED-MINER GATE TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireRegisteredMiner(unittest.TestCase):
    """Tests for the _require_registered_miner gate on submission endpoints."""

    def _make_metagraph_sync(self, hotkeys: list[str]):
        peers = [SimpleNamespace(hotkey=hk) for hk in hotkeys]
        state = SimpleNamespace(peers=peers)
        return SimpleNamespace(state=state)

    def test_failclosed_when_metagraph_sync_unset(self):
        """M1 (2026-05-25 audit): gate now fails CLOSED when metagraph sync
        is not wired. Operators must opt in via LOCAL_TESTNET=1 or
        SUBMISSIONS_ALLOW_UNREGISTERED=1 to bypass."""
        from fastapi import HTTPException
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        try:
            ctx.solver_round_metagraph_sync = None
            with self.assertRaises(HTTPException) as exc_ctx:
                _require_registered_miner("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
            self.assertEqual(exc_ctx.exception.status_code, 503)
        finally:
            ctx.solver_round_metagraph_sync = prev

    def test_failclosed_when_state_never_synced(self):
        """M1: state-None now rejects with 503 instead of accepting silently."""
        from fastapi import HTTPException
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        try:
            ctx.solver_round_metagraph_sync = SimpleNamespace(state=None)
            with self.assertRaises(HTTPException) as exc_ctx:
                _require_registered_miner("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY")
            self.assertEqual(exc_ctx.exception.status_code, 503)
        finally:
            ctx.solver_round_metagraph_sync = prev

    def test_local_testnet_bypasses_metagraph_check(self):
        """M1 carve-out: LOCAL_TESTNET=1 preserves the previous open behavior
        for dev workflows where there's no subtensor to sync against."""
        import os
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev_env = os.environ.get("LOCAL_TESTNET")
        try:
            os.environ["LOCAL_TESTNET"] = "1"
            _require_registered_miner("any_hotkey")  # no raise
        finally:
            if prev_env is None:
                os.environ.pop("LOCAL_TESTNET", None)
            else:
                os.environ["LOCAL_TESTNET"] = prev_env

    def test_accepts_registered_hotkey(self):
        from fastapi import HTTPException
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        try:
            ctx.solver_round_metagraph_sync = self._make_metagraph_sync(
                ["hk_owner", "hk_miner_1", "hk_miner_2"],
            )
            try:
                _require_registered_miner("hk_miner_1")
            except HTTPException:
                self.fail("registered hotkey should pass the gate")
        finally:
            ctx.solver_round_metagraph_sync = prev

    def test_rejects_unregistered_hotkey(self):
        from fastapi import HTTPException
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        try:
            ctx.solver_round_metagraph_sync = self._make_metagraph_sync(
                ["hk_owner", "hk_miner_1"],
            )
            with self.assertRaises(HTTPException) as exc_ctx:
                _require_registered_miner("hk_outsider")
            self.assertEqual(exc_ctx.exception.status_code, 403)
            self.assertIn("not registered", exc_ctx.exception.detail)
        finally:
            ctx.solver_round_metagraph_sync = prev

    def test_emergency_override_env_bypasses_gate(self):
        """SUBMISSIONS_ALLOW_UNREGISTERED=1 is the operator escape hatch."""
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        prev_env = os.environ.get("SUBMISSIONS_ALLOW_UNREGISTERED")
        try:
            ctx.solver_round_metagraph_sync = self._make_metagraph_sync(["hk_owner"])
            os.environ["SUBMISSIONS_ALLOW_UNREGISTERED"] = "1"
            _require_registered_miner("hk_outsider")  # would normally 403
        finally:
            ctx.solver_round_metagraph_sync = prev
            if prev_env is None:
                os.environ.pop("SUBMISSIONS_ALLOW_UNREGISTERED", None)
            else:
                os.environ["SUBMISSIONS_ALLOW_UNREGISTERED"] = prev_env

    def test_strips_whitespace_before_lookup(self):
        """Whitespace in body.hotkey shouldn't masquerade an outsider as registered."""
        from fastapi import HTTPException
        from minotaur_subnet.api.server_context import ctx
        from minotaur_subnet.api.routes.submissions.routes import (
            _require_registered_miner,
        )

        prev = ctx.solver_round_metagraph_sync
        try:
            ctx.solver_round_metagraph_sync = self._make_metagraph_sync(["hk_miner_1"])
            # Trailing whitespace on a registered hotkey is normalized → accepted.
            _require_registered_miner("  hk_miner_1  ")
            # Outsider with whitespace still rejected.
            with self.assertRaises(HTTPException):
                _require_registered_miner(" hk_outsider ")
        finally:
            ctx.solver_round_metagraph_sync = prev


if __name__ == "__main__":
    unittest.main()
