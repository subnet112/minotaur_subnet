"""Follower DISSENT path on the reactive-benchmark champion verification.

When a follower receives a champion proposal it independently re-benchmarks
the challenger AND the current champion over the SAME shared corpus and applies
the fleet-uniform ``evaluate_adoption`` rule itself (#242). It must REJECT
(verified=False) — i.e. DISSENT from the leader — whenever it cannot conclude
the challenger genuinely beats the incumbent:

  (a) challenger does NOT beat the champion by the dethrone margin -> REJECT
  (b) challenger clears the margin (and on-chain gate) -> ADOPT
  (c) bootstrap (no incumbent + above floor) -> ADOPT (must not deadlock)
  (d) champion exists but its image is unresolvable -> REJECT (conservative)
  (e) the legacy {{.Id}} compare mismatches the leader's image_id -> REJECT
  (f) the challenger benchmark needs a real simulator and none is available
      (RealSimulationUnavailable) -> fail CLOSED -> (False, 0.0)
  (g) the CHAMPION benchmark needs a real simulator and none is available
      -> fail CLOSED -> REJECT

These complement test_reactive_determinism_parity.py (fork-pin threading) and
test_reactive_digest_pull.py (image-resolution branch) which do NOT exercise the
adopt-verdict / fail-closed dissent logic.

Hermetic: Docker, Anvil, the orchestrator and every BenchmarkWorker IO method
are mocked. The PURE ``evaluate_adoption`` rule runs for real so the verdict is
the genuine consensus decision, not a stub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.api.routes.submissions.champion_consensus import (
    _reactive_benchmark_candidate,
)
from minotaur_subnet.epoch.manager import DETHRONE_MARGIN
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


# ── fixtures ────────────────────────────────────────────────────────────────


def _intents():
    from minotaur_subnet.harness.test_harness import (
        make_intent,
        make_snapshot,
        make_state,
    )

    return [(make_intent(), make_state(), make_snapshot())]


def _candidate():
    # image_id empty -> legacy mode skips the {{.Id}} compare (its own tests
    # live in test_reactive_digest_pull.py); we drive the ADOPT-verdict tail.
    return MagicMock(
        submission_id="sub_dissent",
        image_tag="solver-x:screening",
        image_id="",
    )


def _scorecard(app_scores, app_onchain):
    """A stand-in for BenchmarkWorker._build_scorecard(...) whose .to_dict()
    feeds the real evaluate_adoption() on-chain gate + per-app checks."""
    card = MagicMock()
    card.app_onchain = app_onchain
    card.to_dict.return_value = {
        "app_scores": app_scores,
        "app_onchain": app_onchain,
        "mock_simulation_count": 0,
    }
    return card


async def _run_dissent(
    *,
    chal_avg: float,
    champ_avg: float,
    chal_card: dict,
    champ_card: dict,
    has_incumbent: bool = True,
    champ_image: str | None = "champ:latest",
    challenger_sim_unavailable: bool = False,
    champion_sim_unavailable: bool = False,
):
    """Drive _reactive_benchmark_candidate with the IO mocked, the worker's two
    benchmark passes returning controllable scorecards/avg-scores, and the REAL
    evaluate_adoption() making the verdict.

    The challenger pass runs first (inside _reactive_benchmark_candidate), then
    the champion pass runs inside _independent_adopt_vote. We distinguish them by
    a call counter on the patched worker methods.
    """
    from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable

    # run_benchmark is invoked twice (challenger, then champion). Optionally raise
    # RealSimulationUnavailable on the chosen pass to exercise the fail-closed path.
    state = {"calls": 0}

    async def fake_run_benchmark(session, intents, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1 and challenger_sim_unavailable:
            raise RealSimulationUnavailable("no real sim (challenger)")
        if state["calls"] == 2 and champion_sim_unavailable:
            raise RealSimulationUnavailable("no real sim (champion)")
        # Tag the results list so _compute_avg_score / _build_scorecard can tell
        # which pass produced them.
        return ["challenger"] if state["calls"] == 1 else ["champion"]

    def fake_compute_avg(self, results):
        return chal_avg if results == ["challenger"] else champ_avg

    def fake_scorecard(self, results):
        card = chal_card if results == ["challenger"] else champ_card
        return _scorecard(card["app_scores"], card["app_onchain"])

    fake_session = MagicMock()
    fake_session.shutdown = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.start_docker = AsyncMock(return_value=fake_session)

    incumbent = MagicMock() if has_incumbent else None

    with (
        patch(
            "minotaur_subnet.api.server_context.ctx",
            MagicMock(store=MagicMock()),
        ),
        patch(
            "minotaur_subnet.api.routes.submissions.champion_consensus.get_store",
            return_value=MagicMock(),
        ),
        # Both _reactive_benchmark_candidate and _independent_adopt_vote import
        # run_benchmark / SolverOrchestrator from the orchestrator module, so a
        # single module-level patch covers both passes.
        patch(
            "minotaur_subnet.harness.orchestrator.run_benchmark",
            new=fake_run_benchmark,
        ),
        patch(
            "minotaur_subnet.harness.orchestrator.SolverOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(
            BenchmarkWorker, "_load_benchmark_intents", return_value=_intents(),
        ),
        patch.object(
            BenchmarkWorker, "_build_score_fn",
            new=AsyncMock(return_value=AsyncMock()),
        ),
        patch.object(
            BenchmarkWorker, "_enrich_intents_with_manifests",
            side_effect=lambda self, i: i, autospec=True,
        ),
        patch.object(
            BenchmarkWorker, "_load_historical_scenarios", return_value=[],
        ),
        patch.object(
            BenchmarkWorker, "_compute_avg_score", new=fake_compute_avg,
        ),
        patch.object(
            BenchmarkWorker, "_build_scorecard", new=fake_scorecard,
        ),
        patch.object(
            BenchmarkWorker, "_resolve_incumbent_submission",
            return_value=incumbent,
        ),
        patch.object(
            BenchmarkWorker, "_resolve_champion_image", return_value=champ_image,
        ),
    ):
        return await _reactive_benchmark_candidate(
            candidate=_candidate(),
            leader_score=chal_avg,
            round_id="round-dissent",
        )


# A clean per-app scorecard with a valid, present on-chain score so the
# evaluate_adoption on-chain HARD VETO passes and the JS dethrone-margin gate is
# the operative check. app "dex" on-chain mean = 8000 BPS on both sides.
_CARD_OK = {"app_scores": {"dex": 0.7}, "app_onchain": {"dex": [8000]}}


# ── (a) challenger does NOT beat the champion -> DISSENT (REJECT) ────────────


@pytest.mark.asyncio
async def test_challenger_not_better_rejects():
    # Challenger avg == champion avg: evaluate_adoption requires strictly-better
    # AND the dethrone margin, so an equal challenger is rejected.
    verified, score = await _run_dissent(
        chal_avg=0.70, champ_avg=0.70,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
    )
    assert verified is False, "an equal challenger must not be adopted"
    assert score == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_challenger_better_but_below_dethrone_margin_rejects():
    # +2% over the champion, below the 5% dethrone margin -> REJECT.
    champ = 0.70
    chal = champ * (1 + DETHRONE_MARGIN / 2)  # +2.5% < 5%
    verified, score = await _run_dissent(
        chal_avg=chal, champ_avg=champ,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
    )
    assert verified is False, "below the dethrone margin the follower must dissent"
    assert score == pytest.approx(chal)


# ── (b) challenger clears the margin -> AGREE (ADOPT) ────────────────────────


@pytest.mark.asyncio
async def test_challenger_beats_margin_adopts():
    champ = 0.60
    chal = champ * (1 + DETHRONE_MARGIN) + 0.01  # comfortably past the 5% margin
    verified, score = await _run_dissent(
        chal_avg=chal, champ_avg=champ,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
    )
    assert verified is True, "a clearly-better challenger must be adopted"
    assert score == pytest.approx(chal)


# ── on-chain HARD VETO dissent: challenger reverted (None) where champion executed


@pytest.mark.asyncio
async def test_challenger_onchain_revert_vetoes_even_with_higher_js():
    # Challenger has a much higher JS score but its plan reverted on-chain (None)
    # for an app the champion executed -> the on-chain HARD VETO forces REJECT,
    # proving JS score alone cannot buy adoption.
    chal_card = {"app_scores": {"dex": 0.95}, "app_onchain": {"dex": [None]}}
    champ_card = {"app_scores": {"dex": 0.50}, "app_onchain": {"dex": [8000]}}
    verified, score = await _run_dissent(
        chal_avg=0.95, champ_avg=0.50,
        chal_card=chal_card, champ_card=champ_card,
    )
    assert verified is False, "an on-chain revert must veto regardless of JS score"


# ── (c) bootstrap: no incumbent + above floor -> ADOPT (no deadlock) ─────────


@pytest.mark.asyncio
async def test_bootstrap_no_incumbent_adopts_above_floor():
    verified, score = await _run_dissent(
        chal_avg=0.55, champ_avg=0.0,
        chal_card=_CARD_OK, champ_card={"app_scores": {}, "app_onchain": {}},
        has_incumbent=False,
    )
    assert verified is True, "first champion above the floor must adopt (no deadlock)"
    assert score == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_bootstrap_below_per_app_floor_rejects():
    # No incumbent but the single app is below PER_APP_MIN_SCORE (0.3) -> REJECT.
    low = {"app_scores": {"dex": 0.10}, "app_onchain": {"dex": [8000]}}
    verified, score = await _run_dissent(
        chal_avg=0.10, champ_avg=0.0,
        chal_card=low, champ_card={"app_scores": {}, "app_onchain": {}},
        has_incumbent=False,
    )
    assert verified is False, "a sub-floor first champion must be rejected"


# ── (d) champion exists but image unresolvable -> conservative REJECT ────────


@pytest.mark.asyncio
async def test_incumbent_image_unresolvable_rejects():
    verified, score = await _run_dissent(
        chal_avg=0.90, champ_avg=0.0,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
        has_incumbent=True, champ_image=None,
    )
    assert verified is False, (
        "with an incumbent whose image can't be resolved the margin can't be "
        "proven -> dissent"
    )
    assert score == pytest.approx(0.90)


# ── (f) challenger benchmark requires real sim, none available -> fail closed ─


@pytest.mark.asyncio
async def test_challenger_real_sim_unavailable_fails_closed():
    verified, score = await _run_dissent(
        chal_avg=0.90, champ_avg=0.50,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
        challenger_sim_unavailable=True,
    )
    assert verified is False
    assert score == 0.0, "RealSimulationUnavailable must fail closed to (False, 0.0)"


# ── (g) champion benchmark requires real sim, none available -> REJECT ───────


@pytest.mark.asyncio
async def test_champion_real_sim_unavailable_rejects():
    # The challenger pass succeeds; the CHAMPION re-benchmark hits
    # RealSimulationUnavailable -> we cannot verify the margin -> REJECT.
    verified, score = await _run_dissent(
        chal_avg=0.90, champ_avg=0.50,
        chal_card=_CARD_OK, champ_card=_CARD_OK,
        champion_sim_unavailable=True,
    )
    assert verified is False, (
        "if the champion benchmark needs a real sim and none is available the "
        "follower cannot verify the margin -> dissent"
    )
    # _independent_adopt_vote returns (False, chal_score) here, not (False, 0.0).
    assert score == pytest.approx(0.90)
