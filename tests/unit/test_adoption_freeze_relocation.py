"""Relocated DISABLE_CHAMPION_ADOPTION freeze (commit-boundary gate).

The freeze used to short-circuit the adopt DECISION (_should_adopt), which aborted
the consensus round before the leader ever broadcast — so followers never voted and
no cross-host agreement could be observed. The freeze is now enforced at the COMMIT
boundary (activate_certified_round), letting the real pipeline run observe-only under
the toggle. These tests pin the two safety properties:

  1. The SYNCHRONOUS standalone path keeps its freeze (via _should_adopt).
  2. The commit boundary never adopts under the freeze (no hot-swap / weights).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import minotaur_subnet.epoch.manager as manager_mod
from minotaur_subnet.epoch.manager import EpochManager
from minotaur_subnet.harness.round_store import RoundStatus


def _bare_manager() -> EpochManager:
    m = EpochManager.__new__(EpochManager)  # bypass the heavy ctor
    m._record_would_be_vote = MagicMock()
    m._current_epoch = 0
    return m


def _chal(sid="chal", raw_output="1000"):
    # A SCORED challenger under the new contract: the scalar composite
    # ``benchmark_score`` was removed, so "scored-with-delivered-value" is now
    # expressed as a per-order row whose ``raw_output`` (exact decimal wei string)
    # parses to > 0. The adoption path here reads ``submission_id`` and delegates
    # the verdict to the (mocked) relative per-order rule, so the row is just a
    # faithful stand-in for a real scored submission.
    return SimpleNamespace(
        submission_id=sid,
        benchmark_details={"per_intent": [{"intent_id": "app:scn", "raw_output": raw_output}]},
    )


# ── 1. The split: _should_adopt keeps the freeze (synchronous path safety) ──────

def test_should_adopt_freeze_short_circuits_before_criteria(monkeypatch):
    m = _bare_manager()
    m._meets_adoption_criteria = MagicMock(return_value=True)
    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")
    # _should_adopt is used by the synchronous path that commits immediately, so it
    # MUST still refuse under the freeze — and without even consulting the verdict.
    assert m._should_adopt(_chal()) is False
    m._meets_adoption_criteria.assert_not_called()


def test_should_adopt_delegates_to_criteria_when_unfrozen(monkeypatch):
    m = _bare_manager()
    m._meets_adoption_criteria = MagicMock(return_value=True)
    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    assert m._should_adopt(_chal()) is True
    m._meets_adoption_criteria.assert_called_once()


def test_meets_criteria_ignores_freeze(monkeypatch):
    # The PURE verdict (used by the consensus path) must NOT consult the freeze, so
    # the pipeline can broadcast + collect a would-be quorum observe-only.
    m = _bare_manager()
    # A scored incumbent: its delivered-value per-order rows are the champion "bar"
    # (the retired scalar benchmark_score is gone). The relative verdict is mocked
    # below, so the rows here just make the champion a faithful scored incumbent.
    m._champion = SimpleNamespace(
        submission_id="champ",
        benchmark_details={"per_intent": [{"intent_id": "app:scn", "raw_output": "500"}]},
    )
    m._incumbent_refresh_failed = False
    m._dethrone_margin = 0.05
    # The relative per-order rule is the sole decision; force it ADOPT here.
    m._evaluate_per_order_adoption = MagicMock(return_value={
        "adopt": True, "reason": "beats champ", "n_wins": 1, "n_regressions": 0,
        "n_blind_spots": 0, "n_matched": 0, "scenarios_compared": 1,
    })
    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")  # set, but must be ignored here
    assert m._meets_adoption_criteria(_chal()) is True


# ── 2. The commit boundary never adopts under the freeze ────────────────────────

@pytest.mark.asyncio
async def test_activation_frozen_does_not_commit(monkeypatch):
    m = _bare_manager()
    round_state = SimpleNamespace(
        status=RoundStatus.CERTIFIED,
        round_id="r1",
        effective_epoch=5,
        certificate=SimpleNamespace(candidate_submission_id="sub_chal", effective_epoch=5),
    )
    m._round_store = MagicMock()
    m._round_store.get_round.return_value = round_state
    m._hot_swap = AsyncMock()
    m._emit_weights = AsyncMock(return_value=True)
    m._complete_round = MagicMock(return_value=SimpleNamespace(round_id="r2"))
    m._on_champion_adopted = MagicMock()  # must NOT be called either

    monkeypatch.setenv("DISABLE_CHAMPION_ADOPTION", "1")
    result = await m.activate_certified_round("r1", epoch=5)

    # The commit is blocked: no hot-swap, no weight emit, no on-chain attest callback.
    m._hot_swap.assert_not_called()
    m._emit_weights.assert_not_called()
    m._on_champion_adopted.assert_not_called()
    # Champion unchanged; round advanced so the pipeline keeps running.
    assert result["champion_changed"] is False
    assert result["abort_reason"] == "adoption_frozen"
    assert result["next_round_id"] == "r2"
    m._complete_round.assert_called_once()


@pytest.mark.asyncio
async def test_activation_commits_when_unfrozen(monkeypatch):
    # Sanity: with the freeze OFF, activation proceeds to the real commit path.
    m = _bare_manager()
    round_state = SimpleNamespace(
        status=RoundStatus.CERTIFIED,
        round_id="r1",
        effective_epoch=5,
        certificate=SimpleNamespace(candidate_submission_id="sub_chal", effective_epoch=5),
    )
    m._round_store = MagicMock()
    m._round_store.get_round.return_value = round_state
    m._round_store.activate_round.return_value = SimpleNamespace(status=RoundStatus.ACTIVATED)
    m._round_store.open_next_round.return_value = SimpleNamespace(round_id="r2")
    m._sub_store = MagicMock()
    m._sub_store.get.return_value = SimpleNamespace(submission_id="sub_chal")
    m._hot_swap = AsyncMock()
    m._emit_weights = AsyncMock(return_value=True)
    m._champion = SimpleNamespace(submission_id="sub_chal", to_dict=lambda: {"submission_id": "sub_chal"})
    m._get_incumbent_snapshot = MagicMock(return_value=None)
    m._on_champion_adopted = None

    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    result = await m.activate_certified_round("r1", epoch=5)

    m._hot_swap.assert_awaited_once()
    assert result["champion_changed"] is True


# ── 3. The provenance gate: a failed attest/merge unconditionally vetoes adoption ──


def _wire_commit_mocks(m):
    """Common commit-path doubles so activation can reach the hot-swap."""
    round_state = SimpleNamespace(
        status=RoundStatus.CERTIFIED,
        round_id="r1",
        effective_epoch=5,
        certificate=SimpleNamespace(candidate_submission_id="sub_chal", effective_epoch=5),
    )
    m._round_store = MagicMock()
    m._round_store.get_round.return_value = round_state
    m._round_store.activate_round.return_value = SimpleNamespace(status=RoundStatus.ACTIVATED)
    m._round_store.open_next_round.return_value = SimpleNamespace(round_id="r2")
    m._sub_store = MagicMock()
    m._sub_store.get.return_value = SimpleNamespace(submission_id="sub_chal")
    m._hot_swap = AsyncMock()
    m._emit_weights = AsyncMock(return_value=True)
    m._champion = SimpleNamespace(
        submission_id="sub_chal", to_dict=lambda: {"submission_id": "sub_chal"},
    )
    m._get_incumbent_snapshot = MagicMock(return_value=None)
    m._complete_round = MagicMock(return_value=SimpleNamespace(round_id="r2"))


@pytest.mark.asyncio
async def test_merge_gate_aborts_when_merge_fails(monkeypatch):
    # The attest/merge callback returns False → the round aborts (merge_failed) and the
    # champion is NOT swapped. This is the head-drift / missing-on-chain-proof case:
    # provenance can't be established, so no weights. The gate is UNCONDITIONAL — no
    # env var enables or disables it.
    m = _bare_manager()
    _wire_commit_mocks(m)
    m._on_champion_adopted = MagicMock(return_value=False)  # attest or merge failed
    m._notify_champion_rejected = MagicMock()  # the gate mirrors feedback onto the PR

    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    result = await m.activate_certified_round("r1", epoch=5)

    # The callback DID run (the gate consults its result), but the commit is vetoed.
    m._on_champion_adopted.assert_called_once()
    m._hot_swap.assert_not_called()
    m._emit_weights.assert_not_called()
    m._round_store.activate_round.assert_not_called()
    # The miner is told WHY on the PR (same reject-feedback path as benchmark fails).
    m._notify_champion_rejected.assert_called_once()
    assert result["champion_changed"] is False
    assert result["abort_reason"] == "merge_failed"
    assert result["next_round_id"] == "r2"
    m._complete_round.assert_called_once()


@pytest.mark.asyncio
async def test_merge_gate_abort_reason_carries_callback_reason(monkeypatch):
    # A MergeResult(False, reason) callback surfaces its SPECIFIC reason into the
    # round's abort_reason as `merge_failed:<reason>` (self-diagnosing), while
    # preserving the `merge_failed` prefix so existing filters still match. The
    # miner notice reflects the reason and no longer blames PR-head drift.
    from minotaur_subnet.relayer.solver_repo import MergeResult

    m = _bare_manager()
    _wire_commit_mocks(m)
    m._on_champion_adopted = MagicMock(return_value=MergeResult(False, "no_quorum_cert"))
    m._notify_champion_rejected = MagicMock()

    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    result = await m.activate_certified_round("r1", epoch=5)

    assert result["champion_changed"] is False
    assert result["abort_reason"] == "merge_failed:no_quorum_cert"
    assert result["abort_reason"].startswith("merge_failed")  # backward-compatible prefix
    m._hot_swap.assert_not_called()
    m._emit_weights.assert_not_called()
    # Miner notice names the real reason; the stale drift-blame default is gone.
    notice = m._notify_champion_rejected.call_args.args[1]
    assert "no_quorum_cert" in notice
    assert "pushed PAST the certified commit" not in notice


@pytest.mark.asyncio
async def test_merge_gate_commits_when_merge_succeeds(monkeypatch):
    # The callback returns True (attest + merge both succeeded) → normal commit.
    m = _bare_manager()
    _wire_commit_mocks(m)
    m._on_champion_adopted = MagicMock(return_value=True)

    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    result = await m.activate_certified_round("r1", epoch=5)

    m._on_champion_adopted.assert_called_once()
    m._hot_swap.assert_awaited_once()
    assert result["champion_changed"] is True
    assert "abort_reason" not in result


@pytest.mark.asyncio
async def test_no_merge_callback_commits(monkeypatch):
    # With no merge callback wired (e.g. a testnet without a solver repo) the gate
    # no-ops: merge_ok stays True and the adoption commits normally.
    m = _bare_manager()
    _wire_commit_mocks(m)
    m._on_champion_adopted = None

    monkeypatch.delenv("DISABLE_CHAMPION_ADOPTION", raising=False)
    result = await m.activate_certified_round("r1", epoch=5)

    m._hot_swap.assert_awaited_once()
    assert result["champion_changed"] is True
