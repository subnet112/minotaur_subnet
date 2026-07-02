"""Follower DISSENT path on the reactive-benchmark champion verification.

When a follower receives a champion proposal it independently re-benchmarks the
challenger AND the current champion over the SAME shared corpus and applies the
AUTHORITATIVE relative per-order rule itself (#242) — the IDENTICAL rule the leader
runs. It must REJECT (verified=False) — i.e. DISSENT from the leader — whenever it
cannot conclude the challenger genuinely beats the incumbent per order:

  (a) challenger only ties the champion (no per-order win) -> REJECT
  (b) challenger strictly wins an order with no regression -> ADOPT
  (c) challenger drops/regresses an order the champion served -> REJECT (veto)
  (d) bootstrap (no incumbent + delivers value) -> ADOPT (must not deadlock)
  (e) champion exists but its image is unresolvable -> REJECT (conservative)
  (f) the challenger benchmark needs a real simulator and none is available
      (RealSimulationUnavailable) -> fail CLOSED -> (False, {})
  (g) the CHAMPION benchmark needs a real simulator and none is available
      -> fail CLOSED -> REJECT

The scalar composite / leader-score-tolerance check was removed: the verdict is now
the pure relative per-order rule and the second tuple element is a COUNTS dict
(``better``/``worse``/``matched``/``compared``), not an aggregate score.

Hermetic: Docker, Anvil, the orchestrator and every BenchmarkWorker IO method are
mocked. The PURE ``evaluate_relative_adoption`` rule runs for real so the verdict
is the genuine consensus decision, not a stub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.api.routes.submissions.champion_consensus import (
    _reactive_benchmark_candidate,
)
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.orchestrator import BenchmarkResult


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


def _results(pairs):
    """Per-order BenchmarkResults carrying intent_id + RAW output (decimal str)."""
    return [BenchmarkResult(intent_id=iid, raw_output=sc) for iid, sc in pairs]


async def _run_dissent(
    *,
    chal_orders,
    champ_orders,
    has_incumbent: bool = True,
    champ_image: str | None = "champ:latest",
    challenger_sim_unavailable: bool = False,
    champion_sim_unavailable: bool = False,
):
    """Drive _reactive_benchmark_candidate with the IO mocked, the worker's two
    benchmark passes returning controllable per-order RAW outputs, and the REAL
    evaluate_relative_adoption() making the verdict.

    The challenger pass runs first (inside _reactive_benchmark_candidate), then the
    champion pass runs inside _independent_adopt_vote — distinguished by a call
    counter on the patched run_benchmark. Returns ``(verified, counts)`` where
    counts is the relative better/worse/matched breakdown (``{}`` on a fail-closed
    reject).
    """
    from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable

    state = {"calls": 0}
    chal_results = _results(chal_orders)
    champ_results = _results(champ_orders)

    async def fake_run_benchmark(session, intents, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1 and challenger_sim_unavailable:
            raise RealSimulationUnavailable("no real sim (challenger)")
        if state["calls"] == 2 and champion_sim_unavailable:
            raise RealSimulationUnavailable("no real sim (champion)")
        return chal_results if state["calls"] == 1 else champ_results

    def fake_scorecard(self, results):
        # Used only by the determinism-logging block (reads .app_onchain).
        card = MagicMock()
        card.app_onchain = {}
        return card

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
            BenchmarkWorker, "_build_reference_quotes",
            new=AsyncMock(return_value={"dex": {"quoted_output": "1"}}),
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
            round_id="round-dissent",
        )


# ── (a) challenger only ties the champion -> DISSENT (REJECT) ────────────────


@pytest.mark.asyncio
async def test_challenger_only_matched_rejects():
    # Identical per-order output everywhere -> matched, no win -> not adopted.
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "100")], champ_orders=[("o1", "100")],
    )
    assert verified is False, "a challenger that only ties must not be adopted"
    assert counts["better"] == 0, "no order is better on a pure tie"
    assert counts["matched"] == 1


# ── (b) challenger strictly wins an order -> AGREE (ADOPT) ────────────────────


@pytest.mark.asyncio
async def test_challenger_wins_an_order_adopts():
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "200")], champ_orders=[("o1", "100")],
    )
    assert verified is True, "a strict per-order win with no regression must adopt"
    assert counts["better"] == 1
    assert counts["worse"] == 0


# ── (c) regression veto: challenger drops an order the champion served ────────


@pytest.mark.asyncio
async def test_challenger_drops_order_vetoes():
    # Challenger delivers nothing ("0") on an order the champion served -> dropped
    # -> regression -> REJECT, even though it never under-delivers a positive amount.
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "0")], champ_orders=[("o1", "100")],
    )
    assert verified is False, "a dropped order must veto regardless of the tally"
    assert counts["worse"] >= 1, "the dropped order counts on the worse side"


@pytest.mark.asyncio
async def test_challenger_regresses_an_order_vetoes():
    # One clear win but one regression -> the regression vetoes adoption.
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "200"), ("o2", "50")],
        champ_orders=[("o1", "100"), ("o2", "100")],
    )
    assert verified is False, "any per-order regression vetoes"
    assert counts["better"] >= 1 and counts["worse"] >= 1


# ── (d) bootstrap: no incumbent + delivers value -> ADOPT (no deadlock) ───────


@pytest.mark.asyncio
async def test_bootstrap_no_incumbent_adopts_when_delivers_value():
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "100")], champ_orders=[],
        has_incumbent=False,
    )
    assert verified is True, "first champion delivering value must adopt (no deadlock)"
    assert counts["better"] == 1, "the delivered order is a blind-spot cover (better)"


@pytest.mark.asyncio
async def test_bootstrap_rejects_when_no_value_delivered():
    verified, _ = await _run_dissent(
        chal_orders=[("o1", "0")], champ_orders=[],
        has_incumbent=False,
    )
    assert verified is False, "a first champion delivering nothing must be rejected"


# ── (e) champion exists but image unresolvable -> conservative REJECT ────────


@pytest.mark.asyncio
async def test_incumbent_image_unresolvable_rejects():
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "200")], champ_orders=[("o1", "100")],
        has_incumbent=True, champ_image=None,
    )
    assert verified is False, (
        "with an incumbent whose image can't be resolved improvement can't be "
        "proven -> dissent"
    )
    assert counts == {}, "an unresolvable champion yields no comparison counts"


# ── (f) challenger benchmark requires real sim, none available -> fail closed ─


@pytest.mark.asyncio
async def test_challenger_real_sim_unavailable_fails_closed():
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "200")], champ_orders=[("o1", "100")],
        challenger_sim_unavailable=True,
    )
    assert verified is False
    assert counts == {}, "RealSimulationUnavailable must fail closed to (False, {})"


# ── (g) champion benchmark requires real sim, none available -> REJECT ───────


@pytest.mark.asyncio
async def test_champion_real_sim_unavailable_rejects():
    # The challenger pass succeeds; the CHAMPION re-benchmark hits
    # RealSimulationUnavailable -> we cannot verify improvement -> REJECT.
    verified, counts = await _run_dissent(
        chal_orders=[("o1", "200")], champ_orders=[("o1", "100")],
        champion_sim_unavailable=True,
    )
    assert verified is False, (
        "if the champion benchmark needs a real sim and none is available the "
        "follower cannot verify improvement -> dissent"
    )
    # _independent_adopt_vote fails closed to (False, {}) — no counts on an
    # unverifiable champion (was the retired (False, chal_score)).
    assert counts == {}
