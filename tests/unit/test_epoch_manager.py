"""Tests for the EpochManager — solver lifecycle across epochs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncio
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Disable provenance requirements for all tests in this module.
# The champion_policy now defaults REQUIRE_SIGNED_PROVENANCE=True
# and REQUIRE_ASYMMETRIC_PROVENANCE=True, which would reject all
# test submissions that lack provenance signatures.
_PROVENANCE_OFF = {
    "REQUIRE_SIGNED_PROVENANCE": "0",
    "REQUIRE_ASYMMETRIC_PROVENANCE": "0",
}

@pytest.fixture(autouse=True)
def _disable_provenance(monkeypatch):
    for k, v in _PROVENANCE_OFF.items():
        monkeypatch.setenv(k, v)

from minotaur_subnet.epoch.manager import EpochManager, ChampionInfo
from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    RoundStatus,
    RoundStore,
)
from minotaur_subnet.harness.submission_store import (
    Submission,
    SubmissionStatus,
    SubmissionStore,
)
from minotaur_subnet.weight_policy import (
    CHAMPION_MINER_WEIGHT_FRACTION,
    GENESIS_HOTKEY,
)


# Anvil's well-known account #0 — public test fixture used to satisfy
# the VALIDATOR_PRIVATE_KEY requirement of the new queue-POST emit path.
_TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class _FakeQueueResponse:
    """200 response stand-in for the validator's /internal/weights/queue."""

    def __init__(self, status=200, text='{"queued": true}'):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeQueueSession:
    """aiohttp.ClientSession stand-in that captures the POST body.

    Used by the weight-emission tests below to assert on the mapping
    EpochManager would send to the validator daemon. Pre-refactor those
    tests checked ``emit_async.call_args``; post-refactor we check the
    HTTP body since that's where the mapping now travels.
    """

    def __init__(self, response: _FakeQueueResponse | None = None):
        self._response = response or _FakeQueueResponse()
        self.posted_body: bytes | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, data, headers):
        self.posted_body = data
        return self._response

    def posted_mapping(self) -> dict:
        """Decode the captured body and return its ``mapping`` field."""
        import json
        return json.loads(self.posted_body)["mapping"]


def _patch_queue_post(monkeypatch, session: _FakeQueueSession):
    """Wire a fake aiohttp module so EpochManager.emit_weights POSTs to ``session``."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", _TEST_KEY)
    fake_module = MagicMock()
    fake_module.ClientSession = MagicMock(return_value=session)
    fake_module.ClientTimeout = MagicMock()
    monkeypatch.setitem(sys.modules, "aiohttp", fake_module)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_submission(
    submission_id: str = "sub_1",
    epoch: int = 1,
    round_id: str = "",
    status: SubmissionStatus = SubmissionStatus.SCORED,
    score: float = 0.8,
    solver_name: str = "test-solver",
    image_tag: str = "solver:v1",
    image_id: str | None = None,
    hotkey: str = "5Gtest",
) -> Submission:
    return Submission(
        submission_id=submission_id,
        repo_url="https://github.com/test/solver",
        commit_hash="abc123",
        epoch=epoch,
        hotkey=hotkey,
        round_id=round_id,
        status=status,
        created_at=time.time(),
        updated_at=time.time(),
        image_tag=image_tag,
        image_id=image_id or ("sha256:" + submission_id.replace("_", "").ljust(64, "0")[:64]),
        solver_name=solver_name,
        solver_version="1.0.0",
        benchmark_rank=1,
        benchmark_details={
            "total_intents": 5,
            # The relative per-order rule decides adoption on the RAW delivered
            # output (raw_output), not the aggregate score. Derive a single
            # proportional order from `score` so a higher-scoring challenger WINS,
            # an equal one MATCHES, and a lower one REGRESSES — the same ordering
            # these fixtures relied on under the legacy aggregate rule.
            "per_intent": [
                {
                    "intent_id": "o1",
                    "score": score,
                    "raw_output": str(int(round(score * 1_000_000))),
                },
            ],
        },
    )


def _make_store_with_subs(*submissions: Submission) -> SubmissionStore:
    store = SubmissionStore()
    for sub in submissions:
        store._submissions[sub.submission_id] = sub
    return store


def _make_mock_block_loop():
    loop = MagicMock()
    loop.set_solver = MagicMock()
    return loop


def _make_mock_benchmark_worker():
    worker = MagicMock()
    worker.run_once = AsyncMock()
    return worker


def _make_mock_orchestrator():
    orch = MagicMock()
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.restore_state = AsyncMock()
    session.serialize_state = AsyncMock(return_value=b"state")
    session.shutdown = AsyncMock()
    session.metadata = MagicMock(return_value=MagicMock(name="test-solver"))
    orch.start_docker = AsyncMock(return_value=session)
    return orch, session


def _make_certified_round(score: float = 0.94, quorum: int = 1):
    """Build a round_store with a certified finalist + matching submission store.

    Returns ``(round_store, current_round, submission, store)`` ready for
    ``activate_certified_round``. Mirrors test_activate_certified_round_adopts_finalist.
    """
    round_store = RoundStore()
    current_round = round_store.ensure_open_round(opened_epoch=5)
    round_store.close_current_round(
        close_epoch=5, benchmark_pack_hash="pack-5",
        committee_hash="committee-5", quorum_required=quorum,
    )
    sub = _make_submission(
        submission_id="sub_certified", epoch=5,
        round_id=current_round.round_id, score=score,
    )
    store = _make_store_with_subs(sub)
    round_store.set_round_finalist(
        current_round.round_id, submission_id="sub_certified",
        image_id=sub.image_id,
    )
    round_store.certify_round(
        current_round.round_id,
        ChampionCertificate(
            round_id=current_round.round_id, committee_hash="committee-5",
            candidate_submission_id="sub_certified", candidate_image_id=sub.image_id,
            incumbent_image_id=None, benchmark_pack_hash="pack-5",
            effective_epoch=6, quorum_required=quorum,
            approvals=[ChampionApproval(
                validator_id="0xabc", round_id=current_round.round_id,
                candidate_submission_id="sub_certified", candidate_image_id=sub.image_id,
                effective_epoch=6, signature="sig",
            )],
        ),
    )
    return round_store, current_round, sub, store


# ── Tests ────────────────────────────────────────────────────────────────────


class TestEpochManager:

    @pytest.mark.asyncio
    async def test_first_epoch_adopts_champion(self):
        """First epoch with a scored submission adopts it as champion."""
        sub = _make_submission(epoch=1, score=0.85)
        store = _make_store_with_subs(sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["champion_changed"] is True
        assert mgr.champion.submission_id == "sub_1"
        assert mgr.champion.hotkey == "5Gtest"
        assert mgr.champion.epoch_adopted == 1

    def test_finalist_tiebreak_is_deterministic_not_insertion_order(self):
        """On a TRUE benchmark-score tie the finalist is chosen by a deterministic,
        content-addressed key (image_id, then submission_id) — NOT the submissions'
        local-clock insertion order — so every validator (and a failed-over leader)
        nominates the SAME finalist for the same tie. Guards consensus determinism."""
        mgr = EpochManager(
            block_loop=_make_mock_block_loop(),
            submission_store=_make_store_with_subs(),
        )
        a = _make_submission(submission_id="sub_aaa", score=0.952)
        b = _make_submission(submission_id="sub_bbb", score=0.952)
        c = _make_submission(submission_id="sub_ccc", score=0.952)
        # Deterministic ascending (image_id, submission_id): aaa < bbb < ccc, regardless
        # of the order the (equally-scored) submissions arrive in.
        for order in ([a, b, c], [c, b, a], [b, c, a], [c, a, b]):
            ranked = mgr._eligible_candidates(list(order))
            assert [s.submission_id for s in ranked] == ["sub_aaa", "sub_bbb", "sub_ccc"]
        # The PRIMARY ranking key still trumps the content-addressed tie-break.
        # Under the relative net-better rule "strictly better" means net-better vs
        # the CHAMPION, so seed a champion baseline: a challenger that WINS an order
        # the equally-delivering peers only MATCH sorts first, ahead of the (aaa/bbb)
        # tie-break it would otherwise lose.
        champ = _make_submission(submission_id="sub_champ", score=0.952)
        mgr2 = EpochManager(
            block_loop=_make_mock_block_loop(),
            submission_store=_make_store_with_subs(champ),
        )
        mgr2._champion = ChampionInfo(submission_id="sub_champ")
        top = _make_submission(submission_id="sub_zzz", score=0.99)  # wins o1 vs 0.952 champ
        assert mgr2._eligible_candidates([a, top, b])[0].submission_id == "sub_zzz"

    @pytest.mark.asyncio
    async def test_no_submissions_keeps_current(self):
        """Epoch with no submissions keeps the current solver."""
        store = SubmissionStore()
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["champion_changed"] is False
        assert mgr.champion.submission_id is None

    @pytest.mark.asyncio
    async def test_dethrone_margin_enforced(self):
        """A challenger within the relative noise band (±0.1%) only MATCHES the
        champion per order — no strict win — so it is NOT adopted."""
        # Set up existing champion at 0.80
        old_sub = _make_submission(
            submission_id="sub_old", epoch=1, score=0.80,
            solver_name="old-solver",
        )
        # New challenger at 0.8005 (+0.0625%, inside the ±0.1% per-order noise band
        # -> "matched", not a win) -> not adopted.
        new_sub = _make_submission(
            submission_id="sub_new", epoch=2, score=0.8005,
            solver_name="new-solver",
        )
        store = _make_store_with_subs(old_sub, new_sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()

        rejected = []
        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
            on_champion_rejected=lambda sub, reason: rejected.append((sub.submission_id, reason)),
        )

        # First epoch: adopt old champion
        await mgr.on_epoch_boundary(epoch=1)
        assert mgr.champion.submission_id == "sub_old"

        # Second epoch: challenger doesn't beat margin
        result = await mgr.on_epoch_boundary(epoch=2)
        assert result["champion_changed"] is False
        assert mgr.champion.submission_id == "sub_old"
        # The rejected challenger gets its PR-mirror callback fired (no pr_number on
        # this fixture -> _notify_champion_rejected guards, so it does NOT fire here).
        assert rejected == []  # fixture submissions have no pr_number

        # With a pr_number, the reject callback fires for the losing challenger.
        new_sub.pr_number = 42
        store2 = _make_store_with_subs(old_sub, new_sub)
        rejected2 = []
        mgr2 = EpochManager(
            block_loop=_make_mock_block_loop(),
            benchmark_worker=_make_mock_benchmark_worker(),
            submission_store=store2,
            on_champion_rejected=lambda sub, reason: rejected2.append(sub.submission_id),
        )
        await mgr2.on_epoch_boundary(epoch=1)
        await mgr2.on_epoch_boundary(epoch=2)
        assert "sub_new" in rejected2

    @pytest.mark.asyncio
    async def test_challenger_beats_margin(self):
        """Challenger beating champion by >5% gets adopted."""
        old_sub = _make_submission(
            submission_id="sub_old", epoch=1, score=0.80,
            solver_name="old-solver",
        )
        # New challenger at 0.90 (12.5% better, exceeds 5%)
        new_sub = _make_submission(
            submission_id="sub_new", epoch=2, score=0.90,
            solver_name="new-solver",
        )
        store = _make_store_with_subs(old_sub, new_sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
        )

        # Adopt old champion
        await mgr.on_epoch_boundary(epoch=1)
        assert mgr.champion.submission_id == "sub_old"

        # Challenger beats margin
        result = await mgr.on_epoch_boundary(epoch=2)
        assert result["champion_changed"] is True
        assert mgr.champion.submission_id == "sub_new"
        assert mgr.champion.solver_name == "new-solver"

    @pytest.mark.asyncio
    async def test_hot_swap_with_orchestrator(self):
        """When orchestrator is available, solver session is started and swapped."""
        sub = _make_submission(epoch=1, score=0.85, image_tag="solver:v1")
        store = _make_store_with_subs(sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()
        orch, session = _make_mock_orchestrator()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
            orchestrator=orch,
        )

        # 4.3: hot-swap verifies local image_id matches certified image_id.
        # In this test, both come from _make_submission's deterministic
        # sha256 fake; patch the docker-inspect helper to return the same.
        from unittest.mock import patch, AsyncMock as _AM
        with patch(
            "minotaur_subnet.epoch.manager._resolve_image_id_via_docker",
            new=_AM(return_value=sub.image_id),
        ):
            await mgr.on_epoch_boundary(epoch=1)

        # Orchestrator should have started a docker session
        orch.start_docker.assert_awaited_once_with("solver:v1")
        session.initialize.assert_awaited_once_with({"epoch": 1})

        # Block loop should have received the new session
        block_loop.set_solver.assert_called_once_with(session)

    @pytest.mark.asyncio
    async def test_hot_swap_with_runtime_builder(self):
        """Custom runtime builder is used for live solver activation."""
        sub = _make_submission(epoch=1, score=0.85, image_tag="solver:v1")
        store = _make_store_with_subs(sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()
        live_solver = MagicMock()
        builder_calls = []

        async def runtime_builder(submission, epoch):
            builder_calls.append((submission.submission_id, epoch))
            return live_solver

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
            runtime_builder=runtime_builder,
        )

        await mgr.on_epoch_boundary(epoch=1)

        assert builder_calls == [("sub_1", 1)]
        block_loop.set_solver.assert_called_once_with(live_solver)

    @pytest.mark.asyncio
    async def test_previous_session_shutdown(self):
        """Previous solver session is shut down and state serialized."""
        sub1 = _make_submission(
            submission_id="sub_1", epoch=1, score=0.80,
            image_tag="solver:v1", solver_name="solver-a",
        )
        sub2 = _make_submission(
            submission_id="sub_2", epoch=2, score=0.95,
            image_tag="solver:v2", solver_name="solver-b",
        )
        store = _make_store_with_subs(sub1, sub2)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()
        orch, session1 = _make_mock_orchestrator()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
            orchestrator=orch,
        )

        from unittest.mock import patch, AsyncMock as _AM

        def _match(image_tag):
            return sub1.image_id if image_tag == "solver:v1" else sub2.image_id

        # Epoch 1: adopt first champion
        with patch(
            "minotaur_subnet.epoch.manager._resolve_image_id_via_docker",
            new=_AM(side_effect=lambda tag: _match(tag)),
        ):
            await mgr.on_epoch_boundary(epoch=1)
        first_session = session1

        # Create a new session for epoch 2
        session2 = AsyncMock()
        session2.initialize = AsyncMock()
        session2.restore_state = AsyncMock()
        orch.start_docker = AsyncMock(return_value=session2)

        # Epoch 2: new champion
        with patch(
            "minotaur_subnet.epoch.manager._resolve_image_id_via_docker",
            new=_AM(side_effect=lambda tag: _match(tag)),
        ):
            await mgr.on_epoch_boundary(epoch=2)

        # Previous session should have been shut down
        first_session.serialize_state.assert_awaited_once()
        first_session.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_benchmark_worker_run_once_called(self):
        """Benchmark worker's run_once is called on epoch boundary."""
        store = SubmissionStore()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        await mgr.on_epoch_boundary(epoch=1)
        worker.run_once.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_benchmark_failure_recorded(self):
        """If benchmark worker fails, error is recorded but manager continues."""
        store = SubmissionStore()
        worker = _make_mock_benchmark_worker()
        worker.run_once = AsyncMock(side_effect=RuntimeError("Docker not available"))

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["error"] == "Docker not available"
        assert result["champion_changed"] is False

    @pytest.mark.asyncio
    async def test_get_champion_returns_dict(self):
        """get_champion returns a dict with all expected fields."""
        mgr = EpochManager()
        champion = mgr.get_champion()

        assert isinstance(champion, dict)
        assert "submission_id" in champion
        assert "solver_name" in champion
        assert "hotkey" in champion
        assert "epoch_adopted" in champion

    @pytest.mark.asyncio
    async def test_epoch_history_tracked(self):
        """Each epoch boundary is recorded in history."""
        sub1 = _make_submission(submission_id="s1", epoch=1, score=0.7)
        sub2 = _make_submission(submission_id="s2", epoch=2, score=0.9)
        store = _make_store_with_subs(sub1, sub2)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        await mgr.on_epoch_boundary(epoch=1)
        await mgr.on_epoch_boundary(epoch=2)

        history = mgr.get_epoch_history()
        assert len(history) == 2
        assert history[0]["epoch"] == 1
        assert history[1]["epoch"] == 2

    @pytest.mark.asyncio
    async def test_same_submission_not_readopted(self):
        """Same submission doesn't trigger a champion change."""
        sub = _make_submission(submission_id="sub_1", epoch=1, score=0.85)
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        # First epoch adopts
        result1 = await mgr.on_epoch_boundary(epoch=1)
        assert result1["champion_changed"] is True

        # Same epoch/submission — no change
        result2 = await mgr.on_epoch_boundary(epoch=1)
        assert result2["champion_changed"] is False

    @pytest.mark.asyncio
    async def test_fallback_to_previous_epoch_champion(self):
        """If no submissions in current epoch, falls back to recent epochs."""
        sub = _make_submission(submission_id="sub_old", epoch=1, score=0.85)
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        # Epoch 1: adopt
        await mgr.on_epoch_boundary(epoch=1)
        assert mgr.champion.submission_id == "sub_old"

        # Epoch 2: no new submissions, same champion stays
        result = await mgr.on_epoch_boundary(epoch=2)
        assert result["champion_changed"] is False
        assert mgr.champion.submission_id == "sub_old"

    @pytest.mark.asyncio
    async def test_no_orchestrator_updates_metadata_only(self):
        """Without orchestrator, champion metadata is updated but no session started."""
        sub = _make_submission(epoch=1, score=0.85)
        store = _make_store_with_subs(sub)
        block_loop = _make_mock_block_loop()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            block_loop=block_loop,
            benchmark_worker=worker,
            submission_store=store,
            # No orchestrator
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["champion_changed"] is True
        assert mgr.champion.submission_id == "sub_1"
        # Block loop should NOT have set_solver called (no session)
        block_loop.set_solver.assert_not_called()

    @pytest.mark.asyncio
    async def test_round_store_activates_round_and_opens_next_round(self):
        """Round-aware epoch flow activates the processed round and reopens intake."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=1)
        sub = _make_submission(
            submission_id="sub_round",
            epoch=1,
            round_id=current_round.round_id,
            score=0.85,
        )
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        processed_round = round_store.get_round(current_round.round_id)
        next_round = round_store.get_current_round()
        champion = round_store.get_active_champion()

        assert result["champion_changed"] is True
        assert result["round_id"] == current_round.round_id
        assert result["next_round_id"] == next_round.round_id
        assert processed_round.status == RoundStatus.ACTIVATED
        assert processed_round.effective_epoch == 1
        assert next_round.status == RoundStatus.OPEN
        assert next_round.round_id != current_round.round_id
        assert next_round.incumbent_submission_id == sub.submission_id
        assert champion.submission_id == sub.submission_id
        assert champion.activated_round_id == current_round.round_id
        assert store.get(sub.submission_id).status == SubmissionStatus.ADOPTED

    @pytest.mark.asyncio
    async def test_round_store_aborts_round_when_no_candidate(self):
        """If no eligible candidate exists, the round aborts and a new one opens."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=3)
        store = SubmissionStore()
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )

        result = await mgr.on_epoch_boundary(epoch=3)

        processed_round = round_store.get_round(current_round.round_id)
        next_round = round_store.get_current_round()

        assert result["champion_changed"] is False
        assert processed_round.status == RoundStatus.ABORTED
        assert processed_round.abort_reason == "no_champion_candidate"
        assert next_round.status == RoundStatus.OPEN
        assert next_round.round_id != current_round.round_id

    @pytest.mark.asyncio
    async def test_evaluate_round_defers_when_submission_still_benchmarking(self):
        """Contrast with test_round_store_aborts_round_when_no_candidate: closed-round
        evaluation with a still-BENCHMARKING submission and no finalist must DEFER (status
        stays REPLAYING, no abort) rather than abort no_champion_candidate — so the
        post-close benchmark can finish. This is the fork-pin defer fix; a not-yet-scored
        submission (incl. one that waited on an unsealed pin) sits in BENCHMARKING."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=4)
        round_store.close_current_round(
            close_epoch=4,
            benchmark_pack_hash="pack-4",
            committee_hash="committee-4",
            quorum_required=2,
        )
        inflight = _make_submission(
            submission_id="sub_inflight",
            epoch=4,
            round_id=current_round.round_id,
            status=SubmissionStatus.BENCHMARKING,
        )
        store = _make_store_with_subs(inflight)
        worker = _make_mock_benchmark_worker()
        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        updated_round = round_store.get_round(current_round.round_id)
        assert result.get("deferred") is True
        assert result.get("abort_reason") is None
        assert updated_round.status != RoundStatus.ABORTED

    @pytest.mark.asyncio
    async def test_round_scope_ignores_other_round_submissions(self):
        """Round-aware champion selection only considers the active round cohort."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=1)
        current_sub = _make_submission(
            submission_id="sub_current",
            epoch=1,
            round_id=current_round.round_id,
            score=0.80,
        )
        other_round_sub = _make_submission(
            submission_id="sub_other",
            epoch=1,
            round_id="round-e1-n999",
            score=0.99,
        )
        store = _make_store_with_subs(current_sub, other_round_sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["champion_changed"] is True
        assert mgr.champion.submission_id == "sub_current"

    @pytest.mark.asyncio
    async def test_evaluate_round_selects_finalist_without_adoption(self):
        """Closed-round evaluation should pick a finalist but not activate it."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=4)
        round_store.close_current_round(
            close_epoch=4,
            benchmark_pack_hash="pack-4",
            committee_hash="committee-4",
            quorum_required=2,
        )
        sub = _make_submission(
            submission_id="sub_finalist",
            epoch=4,
            round_id=current_round.round_id,
            score=0.93,
        )
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        updated_round = round_store.get_round(current_round.round_id)
        assert result["status_after"] == RoundStatus.CERTIFYING.value
        assert result["finalist_submission_id"] == "sub_finalist"
        assert updated_round.finalist_submission_id == "sub_finalist"
        assert store.get("sub_finalist").status == SubmissionStatus.SCORED
        assert mgr.current_epoch == 4
        assert mgr.champion.submission_id is None

    @staticmethod
    def _fallthrough_fixture(fresh_champ_rows, *challengers):
        """Closed round + adopted champion whose STORED rows (rank-time bar,
        two orders at raw 1000000) differ from the FRESH re-bench rows the
        verdict grades against — the skew the fall-through walk covers. Returns
        (mgr, round, rejected) with _refresh_incumbent_score patched to write
        ``fresh_champ_rows`` onto the champion's stored per_intent, exactly like
        the real refresh persists the same-round re-bench."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=4)
        round_store.close_current_round(
            close_epoch=4, benchmark_pack_hash="pack-4",
            committee_hash="committee-4", quorum_required=1,
        )
        champ = _make_submission(submission_id="sub_champ", epoch=1)
        champ.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "1000000"},
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        for c in challengers:
            c.round_id = current_round.round_id
        store = _make_store_with_subs(champ, *challengers)

        rejected = []
        mgr = EpochManager(
            benchmark_worker=_make_mock_benchmark_worker(),
            submission_store=store,
            round_store=round_store,
            on_champion_rejected=lambda sub, reason: rejected.append(
                (sub.submission_id, reason)
            ),
        )
        mgr._champion = ChampionInfo(submission_id="sub_champ")

        async def _fresh_rebench():
            mgr._incumbent_refresh_failed = False
            champ.benchmark_details = {"per_intent": fresh_champ_rows}

        mgr._refresh_incumbent_score = _fresh_rebench
        return mgr, current_round, rejected

    @pytest.mark.asyncio
    async def test_evaluate_round_falls_through_when_fresh_bar_rejects_top(self):
        """The rank grades vs the champion's STORED rows; the verdict grades vs
        the FRESH re-bench. When the fresh bar rejects the top-ranked candidate,
        the round must fall through to a runner-up the fresh bar adopts instead
        of aborting."""
        # Rank-time bar: o1=1000000. Both candidates rank adoptable (net +1),
        # tie-broken by image_id -> top first.
        top = _make_submission(
            submission_id="sub_top", epoch=4,
            image_id="sha256:" + "a" * 64,
        )
        top.pr_number = 41
        top.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "2000000"},  # win vs stored bar
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        runner = _make_submission(
            submission_id="sub_runner", epoch=4,
            image_id="sha256:" + "b" * 64,
        )
        runner.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "2500000"},  # win vs stored bar
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        # Fresh bar: o1=2000000 -> top only MATCHES (no win -> reject), runner
        # still strictly wins -> adopt.
        mgr, current_round, rejected = self._fallthrough_fixture(
            [{"intent_id": "o1", "raw_output": "2000000"},
             {"intent_id": "o2", "raw_output": "1000000"}],
            top, runner,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        assert result["status_after"] == RoundStatus.CERTIFYING.value
        assert result["finalist_submission_id"] == "sub_runner"
        # The passed-over top candidate got its own reject feedback.
        assert [r[0] for r in rejected] == ["sub_top"]
        assert "matched" in rejected[0][1]

    @pytest.mark.asyncio
    async def test_evaluate_round_notifies_outranked_candidates_after_adoption(self):
        """Candidates ranked BELOW the adopted finalist are never evaluated (the
        fall-through walk stops at the first adoption) — they must still get
        reject feedback with an explicit 'outranked' reason instead of silence
        (live gap 2026-07-03: losers to an adopted champion got no PR comment)."""
        top = _make_submission(
            submission_id="sub_top", epoch=4,
            image_id="sha256:" + "a" * 64,
        )
        top.pr_number = 41
        top.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "3000000"},  # wins stored AND fresh bar
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        runner = _make_submission(
            submission_id="sub_runner", epoch=4,
            image_id="sha256:" + "b" * 64,
        )
        runner.pr_number = 42
        runner.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "2500000"},  # adoptable at rank time
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        # Fresh bar: o1=2000000 -> top still strictly wins -> adopted first;
        # runner is never evaluated.
        mgr, current_round, rejected = self._fallthrough_fixture(
            [{"intent_id": "o1", "raw_output": "2000000"},
             {"intent_id": "o2", "raw_output": "1000000"}],
            top, runner,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        assert result["finalist_submission_id"] == "sub_top"
        assert [r[0] for r in rejected] == ["sub_runner"]
        assert "outranked" in rejected[0][1]
        assert "sub_top" in rejected[0][1]

    @pytest.mark.asyncio
    async def test_evaluate_round_defers_with_adoptable_finalist_and_inflight_slate(self):
        """Wait for the FULL slate even when a scored candidate is already adoptable.

        The two-process split (a separate worker benches the slate asynchronously)
        let the coordinator finalize the moment a dethroning candidate scored, while a
        later slate member was still BENCHMARKING — crowning the best of a PARTIAL
        slate and orphaning the straggler's same-pin relative block ("comparison report
        unavailable"). Under rotation seniority the raced-out member is de-prioritised,
        so it does NOT re-compete next round. evaluate_round must DEFER until the slate
        is terminal, exactly like the no-finalist defer, rather than finalize early."""
        # `top` strictly wins the fresh bar -> adoptable finalist RIGHT NOW.
        top = _make_submission(
            submission_id="sub_top", epoch=4, image_id="sha256:" + "a" * 64,
        )
        top.pr_number = 41
        top.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "3000000"},  # wins stored AND fresh bar
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        # A later slate member still BENCHMARKING in the SAME round — no score yet.
        straggler = _make_submission(
            submission_id="sub_straggler", epoch=4,
            image_id="sha256:" + "c" * 64, status=SubmissionStatus.BENCHMARKING,
        )
        straggler.benchmark_details = None
        # Fresh bar: o1=2000000 -> top still strictly wins -> would be adopted.
        mgr, current_round, rejected = self._fallthrough_fixture(
            [{"intent_id": "o1", "raw_output": "2000000"},
             {"intent_id": "o2", "raw_output": "1000000"}],
            top, straggler,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        # DEFERRED, not finalized — the round waits for the straggler to score so the
        # FULL slate is judged (and every member gets its relative block).
        assert result.get("deferred") is True
        assert result.get("finalist_submission_id") is None
        assert result["status_after"] != RoundStatus.ABORTED.value
        assert (
            mgr._round_store.get_round(current_round.round_id).status
            != RoundStatus.ABORTED
        )
        # No premature finalist recorded, no outranked/reject feedback fired yet, and
        # the straggler is left BENCHMARKING to finish (not reaped/waitlisted).
        assert mgr._round_store.get_round(
            current_round.round_id
        ).finalist_submission_id is None
        assert rejected == []
        assert mgr._sub_store.get("sub_straggler").status == SubmissionStatus.BENCHMARKING

    @pytest.mark.asyncio
    async def test_evaluate_round_defers_when_a_candidate_still_benchmarking(self):
        """Restart-survival: when every SCORED candidate rejects but a benched
        candidate is still BENCHMARKING (e.g. a mid-round update.sh restart re-scored
        the slate and one is a straggler), the round DEFERS instead of aborting — so
        the straggler is not orphaned ("benchmark window elapsed") and its report is
        not stranded for a round it might still win. Mirrors the finalist-is-None
        defer; bounded by the decision deadline."""
        top = _make_submission(
            submission_id="sub_top", epoch=4, image_id="sha256:" + "a" * 64,
        )
        top.pr_number = 41
        top.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "1010000"},
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        # A still-BENCHMARKING straggler in the SAME round — no score yet.
        straggler = _make_submission(
            submission_id="sub_straggler", epoch=4,
            image_id="sha256:" + "c" * 64, status=SubmissionStatus.BENCHMARKING,
        )
        straggler.benchmark_details = None
        # Fresh bar rejects the only scored candidate (o1 doubled -> hard-floor cut).
        mgr, current_round, _rejected = self._fallthrough_fixture(
            [{"intent_id": "o1", "raw_output": "2000000"},
             {"intent_id": "o2", "raw_output": "1000000"}],
            top, straggler,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        # DEFERRED, not aborted — the round stays REPLAYING for the straggler to finish.
        assert result.get("deferred") is True
        assert result.get("abort_reason") is None
        assert result["status_after"] != RoundStatus.ABORTED.value
        assert (
            mgr._round_store.get_round(current_round.round_id).status
            != RoundStatus.ABORTED
        )
        # The straggler was NOT reaped/waitlisted — it stays BENCHMARKING to finish.
        assert mgr._sub_store.get("sub_straggler").status == SubmissionStatus.BENCHMARKING

    @pytest.mark.asyncio
    async def test_evaluate_round_aborts_with_top_reason_when_fresh_bar_rejects_all(self):
        """When the fresh bar rejects every ranked candidate, the round aborts
        with the TOP-RANKED candidate's reason (the pre-fall-through headline)
        and every evaluated candidate gets its own reject feedback."""
        top = _make_submission(
            submission_id="sub_top", epoch=4,
            image_id="sha256:" + "a" * 64,
        )
        top.pr_number = 41
        top.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "1010000"},  # +1% vs stored bar: win
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        runner = _make_submission(
            submission_id="sub_runner", epoch=4,
            image_id="sha256:" + "b" * 64,
        )
        runner.pr_number = 42
        runner.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "1000000"},  # matched vs stored bar
            {"intent_id": "o2", "raw_output": "1000000"},
        ]}
        # Stale OUTPERFORMS badges from an earlier pass — the walk must overwrite
        # them from each candidate's authoritative (reject) verdict so a
        # no-change round never leaves a "dethrone" badge standing.
        top.benchmark_details["relative"] = {"verdict": "dethrone", "better": 1}
        runner.benchmark_details["relative"] = {"verdict": "dethrone", "better": 1}
        # Fresh bar: o1=2000000 -> top is cut ~50% (hard floor), runner too.
        mgr, current_round, rejected = self._fallthrough_fixture(
            [{"intent_id": "o1", "raw_output": "2000000"},
             {"intent_id": "o2", "raw_output": "1000000"}],
            top, runner,
        )

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        assert result["status_after"] == RoundStatus.ABORTED.value
        assert "hard floor" in result["abort_reason"]  # top candidate's reason
        assert {r[0] for r in rejected} == {"sub_top", "sub_runner"}
        # Both evaluated candidates' badges reflect the reject verdict, not the
        # stale dethrone (no OUTPERFORMS survives the no-change round).
        for sid in ("sub_top", "sub_runner"):
            rel = mgr._sub_store.get(sid).benchmark_details.get("relative")
            assert rel is not None and rel["verdict"] != "dethrone"

    @pytest.mark.asyncio
    async def test_evaluate_round_is_noop_on_follower(self):
        """A non-leader must NOT evaluate/transition a round — it follows the leader's
        synced outcome. Same setup as the finalist test (a winnable closed round), but
        as a FOLLOWER evaluate_round is a no-op: the round stays CLOSED, no finalist, no
        abort. Regression for the fleet-wide bug where third-party validators ran the
        coordinator (default ON) with no benchmark worker → aborted every round locally
        ('no_champion_candidate') → the fleet rejected the leader's cert (ROUND_WRONG_STATE
        → fleet-abort) and quorum never formed."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=4)
        round_store.close_current_round(
            close_epoch=4,
            benchmark_pack_hash="pack-4",
            committee_hash="committee-4",
            quorum_required=2,
        )
        sub = _make_submission(
            submission_id="sub_finalist",
            epoch=4,
            round_id=current_round.round_id,
            score=0.93,
        )
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            round_store=round_store,
        )
        mgr.set_leader_check(lambda: False)  # this node is a FOLLOWER

        result = await mgr.evaluate_round(current_round.round_id, epoch=4)

        updated_round = round_store.get_round(current_round.round_id)
        # No status transition (would have gone CERTIFYING as leader); follow the sync.
        assert updated_round.status == RoundStatus.CLOSED
        assert result["status_after"] == RoundStatus.CLOSED.value
        assert result["finalist_submission_id"] is None
        assert result["abort_reason"] is None
        assert updated_round.abort_reason is None

    @pytest.mark.asyncio
    async def test_persist_round_relative_counts_is_same_pin(self):
        """DISPLAY: _persist_round_relative_counts writes each competitor's SAME-PIN
        relative counts (vs the re-benched champion) onto its own
        benchmark_details['relative'], tagged with the round_id. The champion record
        is left untouched, and a competitor without shadow rows is skipped."""
        champ = _make_submission(submission_id="champ", round_id="round-e1-n0")
        champ.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "100"},
            {"intent_id": "o2", "raw_output": "200"},
        ]}
        chal = _make_submission(submission_id="chal", round_id="round-e1-n1")
        chal.benchmark_details = {"per_intent": [
            {"intent_id": "o1", "raw_output": "120"},
            {"intent_id": "o2", "raw_output": "250"},
        ]}
        # No shadow rows → must be skipped (no relative block written).
        no_shadow = _make_submission(submission_id="noshadow", round_id="round-e1-n1")
        no_shadow.benchmark_details = {"per_intent": [{"intent_id": "o1", "score": 0.9}]}
        store = _make_store_with_subs(champ, chal, no_shadow)
        mgr = EpochManager(submission_store=store, round_store=RoundStore())
        mgr._champion = ChampionInfo(submission_id="champ")

        await mgr._persist_round_relative_counts("round-e1-n1")

        rel = store.get("chal").benchmark_details["relative"]
        assert rel["better"] == 2 and rel["worse"] == 0
        assert rel["verdict"] == "dethrone"
        assert rel["round_id"] == "round-e1-n1"
        # Deadwood rule context attached None-safely: the metric fields
        # (unproductive_nodes / unproductive_metric_version) live on the #575
        # lineage and do NOT exist on Submission here — getattr ⇒ None nodes ⇒
        # version-guarded delta 0 (clause data-inert), armed COMPUTED from the
        # live constant.
        from minotaur_subnet.epoch.relative_scoring import UNPRODUCTIVE_MARGIN

        assert rel["deadwood"] == {
            "candidate_nodes": None,
            "champion_nodes": None,
            "deadwood_delta": 0,
            "margin": UNPRODUCTIVE_MARGIN,
            "armed": UNPRODUCTIVE_MARGIN is not None,
        }
        # Champion + no-shadow competitor untouched.
        assert "relative" not in store.get("champ").benchmark_details
        assert "relative" not in store.get("noshadow").benchmark_details

    @pytest.mark.asyncio
    async def test_author_candidate_badge_overwrites_stale_dethrone(self):
        """The decision authors the badge: after the adoption walk evaluates a
        candidate, its stored `relative` block is overwritten from the SAME
        verdict the decision used. A STALE `dethrone` block (e.g. persisted in an
        earlier pass against slightly-drifted champion rows) must NOT survive a
        `matched` authoritative verdict — that stale OUTPERFORMS badge on a
        no-change round is exactly what a miner misread as a merge."""
        from minotaur_subnet.epoch.relative_scoring import evaluate_relative_adoption

        champ = _make_submission(submission_id="champ")
        champ.benchmark_details = {"per_intent": [{"intent_id": "o1", "raw_output": "1000"}]}
        cand = _make_submission(submission_id="cand", round_id="round-e1-n1")
        cand.benchmark_details = {
            "per_intent": [{"intent_id": "o1", "raw_output": "1000"}],  # matched
            # Stale winning badge left over from a prior pass.
            "relative": {"verdict": "dethrone", "better": 1, "worse": 0, "matched": 0},
        }
        store = _make_store_with_subs(champ, cand)
        mgr = EpochManager(submission_store=store, round_store=RoundStore())
        mgr._champion = ChampionInfo(submission_id="champ")

        # Authoritative verdict for THIS candidate vs the champion: all-matched.
        mgr._last_adopt_verdict = evaluate_relative_adoption(
            mgr._per_intent(champ), mgr._per_intent(cand),
        )
        assert mgr._last_adopt_verdict["adopt"] is False  # matched, not adopted

        await mgr._author_candidate_badge(cand, champ, "round-e1-n1")

        rel = store.get("cand").benchmark_details["relative"]
        assert rel["verdict"] == "matched"          # overwrote the stale dethrone
        assert rel["better"] == 0 and rel["worse"] == 0
        assert rel["round_id"] == "round-e1-n1"
        assert "factorization" in rel and "deadwood" in rel  # shared context attached

    @pytest.mark.asyncio
    async def test_author_candidate_badge_noop_when_abstained(self):
        """When the verdict is unavailable (abstain: no data / stale bar), the
        badge author leaves any existing block untouched — it never writes a
        previous candidate's verdict onto this one."""
        cand = _make_submission(submission_id="cand", round_id="round-e1-n1")
        stale = {"verdict": "dethrone", "better": 1, "worse": 0, "matched": 0}
        cand.benchmark_details = {"per_intent": [{"intent_id": "o1", "raw_output": "1"}],
                                  "relative": dict(stale)}
        store = _make_store_with_subs(cand)
        mgr = EpochManager(submission_store=store, round_store=RoundStore())
        mgr._champion = ChampionInfo(submission_id="champ")
        mgr._last_adopt_verdict = None  # abstained

        await mgr._author_candidate_badge(cand, None, "round-e1-n1")

        assert store.get("cand").benchmark_details["relative"] == stale

    @pytest.mark.asyncio
    async def test_activate_certified_round_adopts_finalist(self):
        """Certified finalists activate only through the explicit activation path."""
        round_store = RoundStore()
        current_round = round_store.ensure_open_round(opened_epoch=5)
        round_store.close_current_round(
            close_epoch=5,
            benchmark_pack_hash="pack-5",
            committee_hash="committee-5",
            quorum_required=1,
        )
        sub = _make_submission(
            submission_id="sub_certified",
            epoch=5,
            round_id=current_round.round_id,
            score=0.94,
        )
        store = _make_store_with_subs(sub)
        block_loop = _make_mock_block_loop()
        live_solver = MagicMock()

        async def runtime_builder(submission, epoch):
            assert submission.submission_id == "sub_certified"
            assert epoch == 6
            return live_solver

        round_store.set_round_finalist(
            current_round.round_id,
            submission_id="sub_certified",
            image_id=sub.image_id,
        )
        round_store.certify_round(
            current_round.round_id,
            ChampionCertificate(
                round_id=current_round.round_id,
                committee_hash="committee-5",
                candidate_submission_id="sub_certified",
                candidate_image_id=sub.image_id,
                incumbent_image_id=None,
                benchmark_pack_hash="pack-5",
                effective_epoch=6,
                quorum_required=1,
                approvals=[
                    ChampionApproval(
                        validator_id="0xabc",
                        round_id=current_round.round_id,
                        candidate_submission_id="sub_certified",
                        candidate_image_id=sub.image_id,
                        effective_epoch=6,
                        signature="sig",
                    ),
                ],
            ),
        )

        mgr = EpochManager(
            block_loop=block_loop,
            submission_store=store,
            round_store=round_store,
            runtime_builder=runtime_builder,
        )

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        activated_round = round_store.get_round(current_round.round_id)
        next_round = round_store.get_current_round()
        assert result["champion_changed"] is True
        assert activated_round.status == RoundStatus.ACTIVATED
        assert mgr.champion.submission_id == "sub_certified"
        assert store.get("sub_certified").status == SubmissionStatus.ADOPTED
        assert next_round.status == RoundStatus.OPEN
        block_loop.set_solver.assert_called_once_with(live_solver)

    @pytest.mark.asyncio
    async def test_activate_certified_round_defers_on_unknown_finalize(self):
        """UNKNOWN finalize outcome (stage='client': the leader couldn't reach or
        parse the relayer) must DEFER — leave the round CERTIFIED for the
        coordinator to re-drive — NOT abort. Aborting here orphaned the 2026-07-17
        merge: the relayer had already merged, the leader lost the reply across an
        update.sh restart, aborted, and stranded the win on main."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "relayer_unreachable", "client", "conn dropped")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)  # leader runs the finalize

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        # Deferred: neither adopted nor aborted — the round STAYS certified for retry.
        assert result.get("deferred") is True
        assert result.get("champion_changed") is False
        assert result.get("abort_reason") is None
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.CERTIFIED
        )
        block_loop.set_solver.assert_not_called()  # champion unchanged, no hot-swap

    @pytest.mark.asyncio
    async def test_activate_certified_round_aborts_on_definitive_refusal(self):
        """A DEFINITIVE relayer refusal (stage='merge': e.g. no on-chain quorum
        cert) still ABORTS — champion unchanged — as before. Only an UNKNOWN
        outcome defers, so a genuinely-invalid win is never pinned open."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "no_quorum_cert", "merge", "no cert binds head")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("deferred") is not True
        assert result.get("champion_changed") is False
        assert result.get("abort_reason") == "merge_failed:no_quorum_cert"
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.ABORTED
        )

    @pytest.mark.asyncio
    async def test_activate_certified_round_aborts_unknown_past_deadline(self):
        """An UNKNOWN outcome stops deferring past ``decision_deadline_epoch`` and
        ABORTS (bounded retry) — a permanently-unreachable relayer cannot pin a
        certified round open forever."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        # get_round returns a deepcopy, so set the bound on the STORED round object.
        round_store._rounds[current_round.round_id].decision_deadline_epoch = 10
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "relayer_unreachable", "client")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        result = await mgr.activate_certified_round(current_round.round_id, epoch=11)

        assert result.get("deferred") is not True
        assert str(result.get("abort_reason") or "").startswith("merge_failed")
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.ABORTED
        )

    @pytest.mark.asyncio
    async def test_activate_certified_round_defers_on_vr_read_failed(self):
        """A transient PRE-WRITE finalize failure (``vr_read_failed``) DEFERS — even at
        the PRODUCTION timing the naive bound got wrong. The merge-gate only runs at
        ``effective_epoch``, which is ALWAYS past ``decision_deadline_epoch`` (effective
        = close+22, deadline = close+20). Here deadline=4 < effective=6 and the gate runs
        at epoch=6, so a decision-deadline bound would abort on the first attempt and
        discard the win — but the transient defer is measured from ACTIVATION, so it
        correctly defers + leaves the round CERTIFIED for the coordinator to re-drive."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        # Production relationship: decision deadline is already PAST when the gate runs
        # (effective_epoch=6 from the helper; set the deadline strictly below it).
        round_store._rounds[current_round.round_id].decision_deadline_epoch = 4
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "vr_read_failed", "validation", "VR read timed out")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        # Gate runs at epoch == effective_epoch (6) — PAST the decision deadline (4).
        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("deferred") is True
        assert result.get("champion_changed") is False
        assert result.get("abort_reason") is None
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.CERTIFIED
        )
        block_loop.set_solver.assert_not_called()

    @pytest.mark.asyncio
    async def test_activate_certified_round_defers_on_publish_failed(self):
        """``publish_failed`` is the POST-attest transient: the certify() already
        landed on-chain and a transient GitHub 5xx failed the publish. It DEFERS on
        the same activation-relative window as ``vr_read_failed`` — the re-drive
        relies on the finalize's on-chain-cert idempotency to complete the merge
        (incident 2026-07-20). stage='merge' (not 'validation') also proves the defer
        matches on the reason CODE, not the stage."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        round_store._rounds[current_round.round_id].decision_deadline_epoch = 4
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "publish_failed", "merge", "GitHub 503 at publish")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("deferred") is True
        assert result.get("champion_changed") is False
        assert result.get("abort_reason") is None
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.CERTIFIED
        )
        block_loop.set_solver.assert_not_called()

    @pytest.mark.asyncio
    async def test_activate_certified_round_aborts_vr_read_past_window(self):
        """``vr_read_failed`` defers only within ``effective_epoch + grace`` — a
        SUSTAINED BT-EVM RPC outage (not a transient blip) cannot pin a certified round
        open forever; past the activation-relative window it ABORTS."""
        from minotaur_subnet.relayer.solver_repo import MergeResult
        from minotaur_subnet.epoch.manager import _finalize_transient_defer_epochs

        round_store, current_round, _sub, store = _make_certified_round()
        round_store._rounds[current_round.round_id].decision_deadline_epoch = 4
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "vr_read_failed", "validation")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        # effective_epoch=6; step one epoch PAST the activation-relative retry window.
        past = 6 + _finalize_transient_defer_epochs() + 1
        result = await mgr.activate_certified_round(current_round.round_id, epoch=past)

        assert result.get("deferred") is not True
        assert result.get("abort_reason") == "merge_failed:vr_read_failed"
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.ABORTED
        )

    @pytest.mark.asyncio
    async def test_activate_certified_round_aborts_other_validation_refusal(self):
        """The transient defer is scoped to ``vr_read_failed`` ONLY. A DIFFERENT
        validation-stage outcome — a genuine refusal, not a transient read — still
        ABORTS at the SAME epoch where ``vr_read_failed`` would defer, so a truly invalid
        win is never pinned open merely for sharing the 'validation' stage. Guards
        against the scope silently widening to all validation failures."""
        from minotaur_subnet.relayer.solver_repo import MergeResult

        round_store, current_round, _sub, store = _make_certified_round()
        round_store._rounds[current_round.round_id].decision_deadline_epoch = 4
        block_loop = _make_mock_block_loop()

        def merge_cb(submission, round_id, *, certificate):
            return MergeResult(False, "no_quorum_cert", "validation", "quorum not reached")

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)

        # epoch=6 is WITHIN the vr_read window, but this is a different code → abort.
        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("deferred") is not True
        assert result.get("abort_reason") == "merge_failed:no_quorum_cert"
        assert (
            round_store.get_round(current_round.round_id).status == RoundStatus.ABORTED
        )

    @pytest.mark.asyncio
    async def test_follower_adopts_verified_cert_without_finalizing(self):
        """A FOLLOWER adopts the quorum-certified champion on the verified
        certificate WITHOUT running the leader-only finalization (attest + PR
        merge): it must NOT call on_champion_adopted (a follower has no PAT — the
        call would fail and wrongly block adoption), yet must still set the active
        champion so its daemon emits the matching champion weights."""
        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()
        called = []

        def merge_cb(submission, round_id, *, certificate):
            called.append(round_id)
            return False  # a follower running this (no PAT) fails — must be SKIPPED

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: False)  # this node is a FOLLOWER
        # The follower self-adopts only a champion IT independently verified
        # (candidate-bound provenance) — mark this round's candidate as self-verified.
        round_store.mark_self_verified(current_round.round_id, "sub_certified")

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert called == []  # finalization skipped on the follower
        assert result["champion_changed"] is True  # adopted on the verified cert
        assert mgr.champion.submission_id == "sub_certified"
        # active champion set → the daemon (#332) emits the matching champion weights
        assert round_store.get_active_champion().submission_id == "sub_certified"
        assert round_store.get_round(current_round.round_id).status == RoundStatus.ACTIVATED

    @pytest.mark.asyncio
    async def test_follower_refuses_when_leader_merge_failed(self):
        """FIX #4: a follower must NOT weight a champion the leader REFUSED to finalize
        (leader_champion_changed=False) even when it verified the image — but it MUST
        still advance (open the next round), never stranded in CERTIFIED."""
        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=lambda *a, **k: True,
        )
        mgr.set_leader_check(lambda: False)  # FOLLOWER
        round_store.mark_self_verified(current_round.round_id, "sub_certified")

        result = await mgr.activate_certified_round(
            current_round.round_id, epoch=6, leader_champion_changed=False,
        )

        assert result.get("champion_changed") is not True          # NOT adopted
        assert result["abort_reason"] == "leader_merge_failed"
        assert result.get("next_round_id")                          # advanced — not stranded
        assert not round_store.get_active_champion().submission_id   # burn-to-owner

    @pytest.mark.asyncio
    async def test_follower_adopts_at_quorum1_without_self_verify(self):
        """Quorum-1 trust: at quorum<=1 a follower adopts the leader's SIGNED champion
        WITHOUT independently self-verifying it (it cannot reproduce the leader's pack
        at q=1). Without this every follower burns the champion miner's emissions while
        the leader certs alone. NOTE: NO mark_self_verified() — adoption rides on trust."""
        round_store, current_round, _sub, store = _make_certified_round()  # quorum_required=1
        block_loop = _make_mock_block_loop()

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=lambda *a, **k: True,
        )
        mgr.set_leader_check(lambda: False)  # FOLLOWER, deliberately NOT self-verified

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result["champion_changed"] is True  # adopted on quorum<=1 leader-trust
        assert round_store.get_active_champion().submission_id == "sub_certified"
        assert round_store.get_round(current_round.round_id).status == RoundStatus.ACTIVATED

    @pytest.mark.asyncio
    async def test_follower_quorum1_trust_can_be_disabled(self, monkeypatch):
        """FOLLOWER_TRUST_LEADER_QUORUM1=off restores the strict self_verify gate even at
        quorum=1 — a follower that did NOT self-verify burns."""
        monkeypatch.setenv("FOLLOWER_TRUST_LEADER_QUORUM1", "off")
        round_store, current_round, _sub, store = _make_certified_round()  # quorum_required=1
        block_loop = _make_mock_block_loop()

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=lambda *a, **k: True,
        )
        mgr.set_leader_check(lambda: False)  # FOLLOWER, not self-verified

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("champion_changed") is not True          # gated -> burn
        assert not round_store.get_active_champion().submission_id

    @pytest.mark.asyncio
    async def test_follower_quorum_gt1_trust_is_inert(self):
        """At quorum>1 the q1-trust is INERT: a follower that did NOT self-verify still
        burns — the independent decentralized check always stands above quorum 1."""
        round_store, current_round, _sub, store = _make_certified_round(quorum=2)
        block_loop = _make_mock_block_loop()

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=lambda *a, **k: True,
        )
        mgr.set_leader_check(lambda: False)  # FOLLOWER, not self-verified

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert result.get("champion_changed") is not True          # q>1 + no verify -> burn
        assert not round_store.get_active_champion().submission_id

    def test_trust_leader_quorum1_flag_default_on(self, monkeypatch):
        from minotaur_subnet.epoch.manager import _follower_trust_leader_quorum1_enabled
        monkeypatch.delenv("FOLLOWER_TRUST_LEADER_QUORUM1", raising=False)
        assert _follower_trust_leader_quorum1_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "OFF"])
    def test_trust_leader_quorum1_flag_off_values(self, monkeypatch, v):
        from minotaur_subnet.epoch.manager import _follower_trust_leader_quorum1_enabled
        monkeypatch.setenv("FOLLOWER_TRUST_LEADER_QUORUM1", v)
        assert _follower_trust_leader_quorum1_enabled() is False

    @pytest.mark.asyncio
    async def test_follower_reset_unadopts_submission_store_durably(self):
        """FIX #2: reverting a follower self-adopt must un-adopt in the SUBMISSION store
        too, else _sync_round_incumbent_from_submission_store / boot resurrect it."""
        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=lambda *a, **k: True,
        )
        mgr.set_leader_check(lambda: False)  # FOLLOWER
        round_store.mark_self_verified(current_round.round_id, "sub_certified")
        await mgr.activate_certified_round(current_round.round_id, epoch=6)
        assert store.get_champion() is not None  # adopted in the submission store

        mgr._reset_self_adopted_champion_to_burn()

        # Durably reverted across ALL three resurrection sources:
        assert store.get_champion() is None                          # submission store
        assert not round_store.get_active_champion().submission_id   # round store
        assert not mgr.champion.submission_id                        # in-memory

    @pytest.mark.asyncio
    async def test_leader_runs_merge_gate_and_refuses_on_failure(self):
        """The LEADER still runs the finalization gate; a failed attest/merge
        UNCONDITIONALLY refuses adoption (champion unchanged, round aborted)."""
        round_store, current_round, _sub, store = _make_certified_round()
        block_loop = _make_mock_block_loop()
        called = []

        def merge_cb(submission, round_id, *, certificate):
            called.append(round_id)
            return False  # attest/merge did not both succeed

        async def runtime_builder(submission, epoch):
            return MagicMock()

        mgr = EpochManager(
            block_loop=block_loop, submission_store=store, round_store=round_store,
            runtime_builder=runtime_builder, on_champion_adopted=merge_cb,
        )
        mgr.set_leader_check(lambda: True)  # this node is the LEADER

        result = await mgr.activate_certified_round(current_round.round_id, epoch=6)

        assert called == [current_round.round_id]  # finalization ran on the leader
        assert result["champion_changed"] is False
        assert result.get("abort_reason") == "merge_failed"
        assert mgr.champion.submission_id != "sub_certified"  # NOT adopted


class TestChampionInfo:

    def test_to_dict(self):
        info = ChampionInfo(
            submission_id="s1",
            solver_name="my-solver",
            solver_version="2.0.0",
            epoch_adopted=5,
            image_tag="solver:v2",
            hotkey="5Gtest",
            adopted_at=1000.0,
        )
        d = info.to_dict()
        assert d["submission_id"] == "s1"
        assert d["solver_name"] == "my-solver"
        assert d["image_tag"] == "solver:v2"
        assert d["epoch_adopted"] == 5

    def test_default_values(self):
        info = ChampionInfo()
        assert info.submission_id is None
        assert info.solver_name is None
        assert info.epoch_adopted == 0


# ── Weight Emission Tests ────────────────────────────────────────────────────


class TestWeightEmission:

    @pytest.mark.asyncio
    async def test_weights_emitted_after_adoption(self, monkeypatch):
        """Weights are queued for emission after champion selection in
        on_epoch_boundary. Post single-emit-path refactor, EpochManager
        POSTs the per-miner mapping to the validator daemon's
        /internal/weights/queue endpoint rather than calling
        emit_async directly."""
        sub = _make_submission(epoch=1, score=0.85, hotkey="5Gminer1")
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()
        session = _FakeQueueSession()
        _patch_queue_post(monkeypatch, session)

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["weights_emitted"] is True
        # Check the mapping POSTed to the validator has the miner's hotkey
        mapping = session.posted_mapping()
        assert "5Gminer1" in mapping
        assert abs(sum(mapping.values()) - 1.0) < 1e-6

    @pytest.mark.asyncio
    async def test_weights_winner_takes_all(self, monkeypatch):
        """Winner-takes-all: ONLY the adopted champion earns weight (0.10), 0.90
        burns to owner — there is NO exponential-decay tail to other scored
        submissions. (Replaces the old decay-tail behavior.)"""
        sub1 = _make_submission(
            submission_id="sub_best", epoch=1, score=0.90,
            hotkey="5Gminer_best",
            status=SubmissionStatus.ADOPTED,
        )
        sub2 = _make_submission(
            submission_id="sub_mid", epoch=1, score=0.70,
            hotkey="5Gminer_mid",
            status=SubmissionStatus.SCORED,
        )
        sub3 = _make_submission(
            submission_id="sub_low", epoch=1, score=0.50,
            hotkey="5Gminer_low",
            status=SubmissionStatus.SCORED,
        )
        store = _make_store_with_subs(sub1, sub2, sub3)
        worker = _make_mock_benchmark_worker()
        session = _FakeQueueSession()
        _patch_queue_post(monkeypatch, session)

        owner = "5OwnerHotkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            owner_hotkey=owner,
        )

        await mgr.on_epoch_boundary(epoch=1)

        mapping = session.posted_mapping()
        # Only the champion earns weight; the runners-up get nothing.
        assert "5Gminer_best" in mapping
        assert "5Gminer_mid" not in mapping
        assert "5Gminer_low" not in mapping
        # The champion takes CHAMPION_MINER_WEIGHT_FRACTION, the owner the rest.
        # Symbolic on purpose: the split is a tunable constant, and this test is
        # about the champion-takes-all SHAPE, not the current number. The literal
        # is pinned once, in test_weight_policy.py.
        assert mapping["5Gminer_best"] == pytest.approx(CHAMPION_MINER_WEIGHT_FRACTION)
        assert mapping[owner] == pytest.approx(1 - CHAMPION_MINER_WEIGHT_FRACTION)

    @pytest.mark.asyncio
    async def test_no_champion_burns_to_owner(self, monkeypatch):
        """Before any real champion exists, 100% of weights burn to owner.

        This is the burn-fallback property the single-emit-path refactor
        is built around: even when EpochManager has no scored miners and
        only the owner-hotkey burn mapping to send, the queue POST still
        carries that mapping to the validator daemon, which emits it.
        Burn to UID-0 (owner) remains the unconditional safety net.
        """
        store = SubmissionStore()
        worker = _make_mock_benchmark_worker()
        session = _FakeQueueSession()
        _patch_queue_post(monkeypatch, session)

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            owner_hotkey="5Gowner",
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        assert result["champion_changed"] is False
        assert result["weights_emitted"] is True
        mapping = session.posted_mapping()
        assert mapping == {"5Gowner": 1.0}

    @pytest.mark.asyncio
    async def test_genesis_champion_burns_to_owner(self, monkeypatch):
        """Synthetic genesis champion still burns 100% until a real miner wins.

        An ADOPTED genesis submission is restored at init, so on_epoch_boundary
        does not re-adopt it (champion_changed=False). Weights still burn to owner.
        """
        genesis = _make_submission(
            submission_id="sub_genesis",
            epoch=1,
            status=SubmissionStatus.ADOPTED,
            score=0.85,
            solver_name="baseline-swap-solver",
            hotkey=GENESIS_HOTKEY,
        )
        store = _make_store_with_subs(genesis)
        worker = _make_mock_benchmark_worker()
        session = _FakeQueueSession()
        _patch_queue_post(monkeypatch, session)

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            owner_hotkey="5Gowner",
        )

        # Genesis submission is restored at init from ADOPTED status
        assert mgr.champion.submission_id == "sub_genesis"
        assert mgr.champion.hotkey == GENESIS_HOTKEY

        result = await mgr.on_epoch_boundary(epoch=1)

        # Already adopted at init, so no change on epoch boundary
        assert result["champion_changed"] is False
        assert mgr.champion.hotkey == GENESIS_HOTKEY
        assert result["weights_emitted"] is True
        mapping = session.posted_mapping()
        assert mapping == {"5Gowner": 1.0}

    @pytest.mark.asyncio
    async def test_no_emitter_no_crash(self):
        """When no weights_emitter is configured, weights_emitted is False."""
        sub = _make_submission(epoch=1, score=0.85)
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            # No weights_emitter
        )

        result = await mgr.on_epoch_boundary(epoch=1)
        assert result["weights_emitted"] is False

    @pytest.mark.asyncio
    async def test_emission_failure_nonfatal(self):
        """Weight emission failure doesn't crash the epoch boundary."""
        sub = _make_submission(epoch=1, score=0.85, hotkey="5Gminer1")
        store = _make_store_with_subs(sub)
        worker = _make_mock_benchmark_worker()
        emitter = MagicMock()
        emitter.emit_async = AsyncMock(side_effect=RuntimeError("network error"))

        mgr = EpochManager(
            benchmark_worker=worker,
            submission_store=store,
            weights_emitter=emitter,
        )

        result = await mgr.on_epoch_boundary(epoch=1)

        # Should not crash, weights_emitted should be False
        assert result["weights_emitted"] is False
        assert result["champion_changed"] is True  # Champion still adopted


# ── Owner Resolution (chain-primary) Tests ───────────────────────────────────


class TestEpochManagerOwnerResolution:
    """The burn-target subnet owner is resolved CHAIN-PRIMARY when a chain source
    is wired, with the env/constructor owner only as a fallback."""

    def test_chain_source_is_primary_and_cached(self):
        """A wired chain source's resolve_subnet_owner() wins over the env owner,
        and the resolved value is cached (no repeat chain queries)."""
        source = MagicMock()
        source.resolve_subnet_owner = MagicMock(return_value="5Gchain")

        mgr = EpochManager(owner_hotkey="5Genv")
        mgr.set_owner_chain_source(source)

        assert mgr._resolve_owner_hotkey() == "5Gchain"  # chain-primary
        # Cached: a second call must not re-query the chain
        assert mgr._resolve_owner_hotkey() == "5Gchain"
        source.resolve_subnet_owner.assert_called_once()

    def test_falls_back_to_env_without_source(self, monkeypatch):
        """With no chain source wired, resolve to the env/constructor owner."""
        monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
        monkeypatch.delenv("OWNER_HOTKEY", raising=False)
        mgr = EpochManager(owner_hotkey="5Genv")
        assert mgr._resolve_owner_hotkey() == "5Genv"

    def test_falls_back_to_env_when_chain_empty(self):
        """When the chain source returns '', fall back to the env owner (a real
        resolved value, so it's cached)."""
        source = MagicMock()
        source.resolve_subnet_owner = MagicMock(return_value="")

        mgr = EpochManager(owner_hotkey="5Genv")
        mgr.set_owner_chain_source(source)

        assert mgr._resolve_owner_hotkey() == "5Genv"
        assert mgr._resolved_owner == "5Genv"  # real fallback value cached

    def test_empty_not_cached_when_no_owner_anywhere(self, monkeypatch):
        """When neither chain nor env yields an owner, '' is returned and NOT
        cached — so a later chain/env value can still win."""
        monkeypatch.delenv("SUBNET_OWNER_HOTKEY", raising=False)
        monkeypatch.delenv("OWNER_HOTKEY", raising=False)
        source = MagicMock()
        source.resolve_subnet_owner = MagicMock(return_value="")

        mgr = EpochManager(owner_hotkey="")
        mgr.set_owner_chain_source(source)

        assert mgr._resolve_owner_hotkey() == ""
        assert mgr._resolved_owner == ""  # empty not cached

        # A later chain value now wins (no stale empty cached)
        source.resolve_subnet_owner = MagicMock(return_value="5Gchain")
        assert mgr._resolve_owner_hotkey() == "5Gchain"

    def test_chain_failure_falls_back_to_env(self):
        """A raising chain source must not crash — fall back to the env owner."""
        source = MagicMock()
        source.resolve_subnet_owner = MagicMock(side_effect=RuntimeError("rpc down"))

        mgr = EpochManager(owner_hotkey="5Genv")
        mgr.set_owner_chain_source(source)

        assert mgr._resolve_owner_hotkey() == "5Genv"


# ── Benchmark Snapshot Wiring Tests ──────────────────────────────────────────


class TestBenchmarkSnapshotWiring:

    @pytest.mark.asyncio
    async def test_fallback_to_synthetic(self):
        """Without snapshot_builder, _build_snapshot returns synthetic."""
        from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

        store = SubmissionStore()
        worker = BenchmarkWorker(submission_store=store)

        snapshot = await worker._build_snapshot(chain_id=1)

        # Synthetic snapshot has block_number=18500000
        assert snapshot.block_number == 18500000
        assert snapshot.chain_id == 1
        assert "ETH/USD" in snapshot.prices

    @pytest.mark.asyncio
    async def test_uses_builder_when_available(self):
        """With snapshot_builder + epoch_block, uses the builder."""
        from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
        from minotaur_subnet.sdk.intent_solver import MarketSnapshot

        mock_builder = MagicMock()
        live_snapshot = MarketSnapshot(
            chain_id=1,
            block_number=19000000,
            timestamp=1700100000,
            prices={"ETH/USD": 2000.0},
            dex_config={},
        )
        mock_builder.build_chain_snapshot = AsyncMock(return_value=live_snapshot)

        store = SubmissionStore()
        worker = BenchmarkWorker(
            submission_store=store,
            snapshot_builder=mock_builder,
            epoch_block_number=19000000,
        )

        snapshot = await worker._build_snapshot(chain_id=1)

        assert snapshot.block_number == 19000000
        assert snapshot.prices == {"ETH/USD": 2000.0}
        mock_builder.build_chain_snapshot.assert_awaited_once_with(
            chain_id=1, block_number=19000000,
        )

    @pytest.mark.asyncio
    async def test_fallback_on_builder_error(self):
        """If builder raises, falls back to synthetic."""
        from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

        mock_builder = MagicMock()
        mock_builder.build_chain_snapshot = AsyncMock(
            side_effect=ConnectionError("RPC unreachable")
        )

        store = SubmissionStore()
        worker = BenchmarkWorker(
            submission_store=store,
            snapshot_builder=mock_builder,
            epoch_block_number=19000000,
        )

        snapshot = await worker._build_snapshot(chain_id=1)

        # Should fall back to synthetic
        assert snapshot.block_number == 18500000
        assert "ETH/USD" in snapshot.prices

    def test_set_epoch_block(self):
        """set_epoch_block updates the worker's block number."""
        from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

        store = SubmissionStore()
        worker = BenchmarkWorker(submission_store=store)

        assert worker._epoch_block_number is None
        worker.set_epoch_block(19500000)
        assert worker._epoch_block_number == 19500000


# ── Phase-2 factorization rank tie-break ──────────────────────────────────────


def test_finalist_tiebreak_prefers_cleaner_factorization():
    """On a performance tie the CLEANEST candidate (smallest persisted
    max_region_nodes) ranks first — ahead of the content-addressed fallback —
    and an UNMEASURED (None) candidate can never win a tie on cleanliness."""
    mgr = EpochManager(
        block_loop=_make_mock_block_loop(),
        submission_store=_make_store_with_subs(),
    )
    # Same score everywhere ⇒ identical (adoptable, net) ⇒ tie.
    dirty = _make_submission(submission_id="sub_aaa", score=0.9)   # aaa wins image_id order
    clean = _make_submission(submission_id="sub_bbb", score=0.9)
    unmeasured = _make_submission(submission_id="sub_000", score=0.9)  # image_id sorts FIRST
    dirty.max_region_nodes = 900
    clean.max_region_nodes = 120
    unmeasured.max_region_nodes = None
    for order in ([dirty, clean, unmeasured], [unmeasured, dirty, clean], [clean, unmeasured, dirty]):
        ranked = mgr._eligible_candidates(list(order))
        # cleanest first; unmeasured LAST despite winning every string tie-break.
        assert [s.submission_id for s in ranked] == ["sub_bbb", "sub_aaa", "sub_000"]
    # Among all-unmeasured records the legacy content-addressed order still holds.
    a = _make_submission(submission_id="sub_aaa", score=0.9)
    b = _make_submission(submission_id="sub_bbb", score=0.9)
    assert [s.submission_id for s in mgr._eligible_candidates([b, a])] == ["sub_aaa", "sub_bbb"]


# ── time-weighted emission: Phase 0 observe-only accrual wiring ───────────────

_TW_HK_B = "5MinerTimeWeightedBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
_TW_OWNER = "5OwnerBurnTimeWeightedAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _make_observe_manager():
    store = _make_store_with_subs(
        _make_submission(submission_id="sub_tw_b", hotkey=_TW_HK_B),
    )
    mgr = EpochManager(
        submission_store=store, round_store=RoundStore(), owner_hotkey=_TW_OWNER,
    )
    mgr._champion = ChampionInfo(submission_id="sub_tw_b", hotkey=_TW_HK_B)
    return mgr


def test_time_weighted_observe_does_not_change_emitted_mapping(monkeypatch):
    """With the observe flag ON, _build_weights_mapping still returns the exact
    winner-take-all vector — observation must never alter emission."""
    mgr = _make_observe_manager()

    monkeypatch.delenv("EMISSION_TIME_WEIGHTED_OBSERVE", raising=False)
    baseline = mgr._build_weights_mapping(1)
    # Current champion (B) takes the miner share; owner takes the remainder.
    assert set(baseline) == {_TW_HK_B, _TW_OWNER}
    assert baseline[_TW_HK_B] == pytest.approx(CHAMPION_MINER_WEIGHT_FRACTION)
    assert baseline[_TW_OWNER] == pytest.approx(1.0 - CHAMPION_MINER_WEIGHT_FRACTION)

    monkeypatch.setenv("EMISSION_TIME_WEIGHTED_OBSERVE", "1")
    mgr.observe_accrue_throne_time()  # a coordinator-loop tick
    with_observe = mgr._build_weights_mapping(1)
    assert with_observe == baseline


def test_observe_accrue_is_noop_when_flag_off(monkeypatch):
    """The accumulator is never touched while the observe flag is off."""
    monkeypatch.delenv("EMISSION_TIME_WEIGHTED_OBSERVE", raising=False)
    mgr = _make_observe_manager()
    mgr.observe_accrue_throne_time()
    assert mgr._throne_accumulator.debug_state()["tempo_index"] is None


def test_observe_accrue_samples_current_champion_when_on(monkeypatch):
    """With the flag on, a tick anchors the accumulator to the current tempo."""
    monkeypatch.setenv("EMISSION_TIME_WEIGHTED_OBSERVE", "1")
    mgr = _make_observe_manager()
    mgr.observe_accrue_throne_time()
    assert mgr._throne_accumulator.debug_state()["tempo_index"] is not None


def test_time_weighted_observe_survives_missing_stores(monkeypatch):
    """Observation is best-effort: a manager without a round store must not raise
    from _build_weights_mapping or a sample tick when the flag is on."""
    monkeypatch.setenv("EMISSION_TIME_WEIGHTED_OBSERVE", "1")
    store = _make_store_with_subs(_make_submission(submission_id="sub_only"))
    mgr = EpochManager(submission_store=store, owner_hotkey=_TW_OWNER)
    mgr._champion = ChampionInfo()  # no champion → 100% owner burn
    mgr.observe_accrue_throne_time()  # must not raise
    mapping = mgr._build_weights_mapping(1)
    assert mapping == {_TW_OWNER: 1.0}
