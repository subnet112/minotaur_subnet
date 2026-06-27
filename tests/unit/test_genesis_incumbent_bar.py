"""Genesis-as-bar (#242, user decision): the FIRST champion must BEAT the genesis.

The leader seeds self._champion from a SCORED genesis (score>0) at decision time so
the adoption rule sees has_champion=True (must beat genesis*(1+margin) + floor + veto).
This must be WEIGHT-SAFE: the seeded hotkey is GENESIS_HOTKEY, which is_real_miner_hotkey
rejects, so _build_weights_mapping still burns 100% to the owner — identical to the
empty-champion case. These tests lock both properties.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from minotaur_subnet.epoch.manager import ChampionInfo, EpochManager
from minotaur_subnet.harness.submission_store import SubmissionStatus
from minotaur_subnet.weight_policy import GENESIS_HOTKEY

OWNER = "5OwnerHotkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _genesis(score, *, status=SubmissionStatus.SCORED):
    return SimpleNamespace(
        submission_id="sub_genesis", solver_name="baseline-swap-solver",
        solver_version="1", benchmark_score=score, epoch=0, image_tag=None,
        hotkey=GENESIS_HOTKEY, updated_at=0, status=status,
    )


def _mgr(*, genesis=None, adopted=None):
    sub = MagicMock()
    sub.get_champion.return_value = adopted
    sub.get_by_hotkey_epoch.return_value = genesis
    rs = MagicMock()
    rs.get_active_champion.return_value = SimpleNamespace(submission_id="")
    mgr = EpochManager(submission_store=sub, round_store=rs, owner_hotkey=OWNER)
    if adopted is None:
        mgr._champion = ChampionInfo()  # ensure truly empty start
    return mgr


def test_seeds_scored_genesis_as_incumbent():
    mgr = _mgr(genesis=_genesis(0.5))
    assert not mgr._champion.submission_id  # empty before
    mgr._maybe_seed_genesis_incumbent()
    assert mgr._champion.submission_id == "sub_genesis"
    assert mgr._champion.hotkey == GENESIS_HOTKEY  # weight-safety invariant
    assert mgr._champion.benchmark_score == 0.5


def test_does_not_seed_unscored_genesis():
    mgr = _mgr(genesis=_genesis(0.0))  # no usable bar yet
    mgr._maybe_seed_genesis_incumbent()
    assert not mgr._champion.submission_id  # stays bootstrap


def test_does_not_seed_when_absent():
    mgr = _mgr(genesis=None)
    mgr._maybe_seed_genesis_incumbent()
    assert not mgr._champion.submission_id


def test_does_not_overwrite_real_champion():
    real = SimpleNamespace(
        submission_id="sub_real", solver_name="x", solver_version="1",
        benchmark_score=0.8, epoch=3, image_tag="img", hotkey="5RealMiner", updated_at=0,
    )
    mgr = _mgr(adopted=real, genesis=_genesis(0.9))
    mgr._champion = ChampionInfo(submission_id="sub_real", hotkey="5RealMiner", benchmark_score=0.8)
    mgr._maybe_seed_genesis_incumbent()
    assert mgr._champion.submission_id == "sub_real"  # NOT overwritten by genesis


def test_genesis_incumbent_still_burns_weights_to_owner():
    # The crux: even seeded as the incumbent bar, genesis routes 0 weight to any
    # miner — 100% burns to owner, byte-identical to the empty-champion case.
    mgr = _mgr(genesis=_genesis(0.5))
    mgr._maybe_seed_genesis_incumbent()
    assert mgr._champion.hotkey == GENESIS_HOTKEY
    weights = mgr._build_weights_mapping(epoch=1)
    assert weights == {OWNER: 1.0}


def test_winner_takes_all_only_champion_earns_weight():
    # With a REAL miner champion, _build_weights_mapping is winner-takes-all:
    # ONLY self._champion gets 0.05, owner gets 0.95 — NO score-ranked decay tail
    # to other scored submissions. Folds #329: a non-adopted candidate (e.g. a
    # merge-failed winner that's still SCORED) is never self._champion, so even if
    # it out-scores the champion it can never appear in the weight mapping.
    mgr = _mgr(genesis=None)
    mgr._champion = ChampionInfo(
        submission_id="sub_real", hotkey="5RealMiner",
        benchmark_score=0.8, image_tag="img",
    )
    higher_scoring_non_champion = SimpleNamespace(
        submission_id="sub_other", solver_name="x", solver_version="1",
        benchmark_score=0.99, epoch=1, image_tag="img2", hotkey="5OtherMiner",
        updated_at=0, status=SubmissionStatus.SCORED,
    )
    mgr._sub_store.list_by_round.return_value = [higher_scoring_non_champion]
    mgr._sub_store.list_by_epoch.return_value = [higher_scoring_non_champion]

    weights = mgr._build_weights_mapping(epoch=1, round_id="r1")

    assert set(weights) == {"5RealMiner", OWNER}
    assert weights["5RealMiner"] == pytest.approx(0.05)
    assert weights[OWNER] == pytest.approx(0.95)
    assert "5OtherMiner" not in weights  # no decay tail; non-champion earns nothing


# ── stale-bar guard: _refresh_incumbent_score flags an un-refreshable incumbent ──

def test_refresh_flags_stale_when_incumbent_image_unresolvable():
    import asyncio
    g = _genesis(0.5)  # image_tag=None
    mgr = _mgr(genesis=g)
    mgr._sub_store.get.return_value = g  # incumbent_sub = genesis (no image)
    bw = MagicMock()
    bw._resolve_champion_image.return_value = None  # genesis image unresolvable
    mgr._benchmark_worker = bw
    asyncio.run(mgr._refresh_incumbent_score())
    assert mgr._champion.submission_id == "sub_genesis"  # seeded as incumbent
    assert mgr._incumbent_refresh_failed is True  # couldn't re-benchmark -> stale


def test_refresh_resets_stale_flag_when_no_incumbent():
    import asyncio
    mgr = _mgr(genesis=None)  # no champion, no scored genesis
    mgr._incumbent_refresh_failed = True  # stale from a prior round
    asyncio.run(mgr._refresh_incumbent_score())
    # No incumbent -> early return after reset -> not stale (bootstrap proceeds).
    assert not mgr._champion.submission_id
    assert mgr._incumbent_refresh_failed is False


def test_refresh_flags_stale_when_corpus_empty():
    # Real benchmark_worker returns an EMPTY intents list (all apps non-operational
    # during a redeploy window) -> incumbent can't be re-benchmarked -> stale ->
    # _should_adopt will abstain. The non-list (mock) case must NOT flag (test-compat).
    import asyncio
    from minotaur_subnet.epoch.manager import ChampionInfo
    real = SimpleNamespace(
        submission_id="sub_real", solver_name="x", solver_version="1",
        benchmark_score=0.4, epoch=3, image_tag="img:1", hotkey="5Real", updated_at=0,
    )
    mgr = _mgr(genesis=None)
    mgr._champion = ChampionInfo(submission_id="sub_real", hotkey="5Real", benchmark_score=0.4, image_tag="img:1")
    mgr._sub_store.get.return_value = real
    bw = MagicMock()
    bw._load_benchmark_intents.return_value = []  # empty corpus (a real list)
    mgr._benchmark_worker = bw
    asyncio.run(mgr._refresh_incumbent_score())
    assert mgr._incumbent_refresh_failed is True


def test_refresh_does_not_flag_non_list_intents_mock_worker():
    # A MagicMock benchmark_worker returns a non-list from _load_benchmark_intents;
    # that's the test/degenerate guard -> return WITHOUT flagging stale.
    import asyncio
    from minotaur_subnet.epoch.manager import ChampionInfo
    real = SimpleNamespace(submission_id="sub_real", image_tag="img:1", hotkey="5Real", benchmark_score=0.4)
    mgr = _mgr(genesis=None)
    mgr._champion = ChampionInfo(submission_id="sub_real", hotkey="5Real", benchmark_score=0.4, image_tag="img:1")
    mgr._sub_store.get.return_value = real
    mgr._benchmark_worker = MagicMock()  # _load_benchmark_intents() -> MagicMock (not a list)
    asyncio.run(mgr._refresh_incumbent_score())
    assert mgr._incumbent_refresh_failed is False


def test_refresh_scores_incumbent_with_reference_quotes():
    import asyncio
    from minotaur_subnet.epoch.manager import ChampionInfo

    real = SimpleNamespace(
        submission_id="sub_real",
        solver_name="x",
        solver_version="1",
        benchmark_score=0.7,
        epoch=3,
        image_tag="champ:1",
        hotkey="5Real",
        updated_at=0,
        benchmark_details={"scorecard": {"app_onchain": {"dex": [7000]}}},
    )
    mgr = _mgr(genesis=None)
    mgr._champion = ChampionInfo(
        submission_id="sub_real",
        hotkey="5Real",
        benchmark_score=0.7,
        image_tag="champ:1",
    )
    mgr._sub_store.get.return_value = real
    mgr._round_store.get_current_round.return_value = SimpleNamespace(round_id="round-1")

    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    reference_quotes = {"dex": {"quoted_output": "100"}}
    seen: dict[str, object] = {}

    bw = MagicMock()
    bw._epoch_block_number = 123
    bw._require_real_sim = True
    bw._load_benchmark_intents.return_value = intents
    bw._build_score_fn = AsyncMock(return_value=object())
    bw._enrich_intents_with_manifests.side_effect = lambda x: x
    bw._load_historical_scenarios.return_value = []
    bw._build_reference_quotes = AsyncMock(return_value=reference_quotes)
    bw._compute_avg_score.return_value = 0.52
    bw._results_to_details.return_value = {
        "scorecard": {"app_onchain": {"dex": [5200]}},
    }

    async def _benchmark_submission(image_tag, bench_intents, score_fn, *, reference_quotes=None):
        seen["image_tag"] = image_tag
        seen["reference_quotes"] = reference_quotes
        return ["RESULTS"]

    async def _memo_champion_bench(
        *,
        round_id,
        image,
        fork_block,
        intents,
        require_real_sim,
        reference_quotes=None,
        run,
    ):
        seen["memo_reference_quotes"] = reference_quotes
        return await run()

    bw._benchmark_submission = AsyncMock(side_effect=_benchmark_submission)
    bw.memo_champion_bench = AsyncMock(side_effect=_memo_champion_bench)
    mgr._benchmark_worker = bw

    asyncio.run(mgr._refresh_incumbent_score())

    assert mgr._champion.benchmark_score == 0.52
    assert seen["image_tag"] == "champ:1"
    assert seen["reference_quotes"] is reference_quotes
    assert seen["memo_reference_quotes"] is reference_quotes
    bw._build_reference_quotes.assert_awaited_once_with(intents, image_tag="champ:1")


# ── CORRECTED reference-bar shadow (REFQUOTE_SHADOW, observe-only) ──────────────
# The production bar is the reference-anchored score (#353); the shadow ALSO scores
# the incumbent SELF-QUOTED and logs the delta — the measurement the #340 shadow got
# wrong (it reused the memoized champion result for both legs -> false delta=0).

def _shadow_harness(calls, *, self_raises=False):
    from minotaur_subnet.epoch.manager import ChampionInfo
    real = SimpleNamespace(
        submission_id="sub_real", solver_name="x", solver_version="1",
        benchmark_score=0.7, epoch=3, image_tag="champ:1", hotkey="5Real", updated_at=0,
        benchmark_details={"scorecard": {"app_onchain": {"dex": [7000]}}},
    )
    mgr = _mgr(genesis=None)
    mgr._champion = ChampionInfo(
        submission_id="sub_real", hotkey="5Real", benchmark_score=0.7, image_tag="champ:1",
    )
    mgr._sub_store.get.return_value = real
    mgr._round_store.get_current_round.return_value = SimpleNamespace(round_id="round-1")
    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    reference_quotes = {"dex": {"quoted_output": "100"}}
    bw = MagicMock()
    bw._epoch_block_number = 123
    bw._require_real_sim = True
    bw._load_benchmark_intents.return_value = intents
    bw._build_score_fn = AsyncMock(return_value=object())
    bw._enrich_intents_with_manifests.side_effect = lambda x: x
    bw._load_historical_scenarios.return_value = []
    bw._build_reference_quotes = AsyncMock(return_value=reference_quotes)
    bw._results_to_details.return_value = {"scorecard": {}}

    async def _bs(image_tag, bench_intents, score_fn, *, reference_quotes=None):
        calls.append(reference_quotes)
        if reference_quotes is None and self_raises:
            raise RuntimeError("shadow boom")
        return ["REF_RESULTS"] if reference_quotes else ["SELF_RESULTS"]

    async def _memo(*, round_id, image, fork_block, intents, require_real_sim,
                    reference_quotes=None, run):
        return await run()

    bw._benchmark_submission = AsyncMock(side_effect=_bs)
    bw.memo_champion_bench = AsyncMock(side_effect=_memo)
    # production reference-anchored -> 0.52 ; self-quote shadow leg -> 0.72
    bw._compute_avg_score.side_effect = lambda r: 0.52 if r == ["REF_RESULTS"] else 0.72
    mgr._benchmark_worker = bw
    return mgr, reference_quotes


def test_refquote_shadow_logs_delta_without_changing_bar(monkeypatch):
    import asyncio
    monkeypatch.setenv("REFQUOTE_SHADOW", "1")
    calls = []
    mgr, ref = _shadow_harness(calls)
    asyncio.run(mgr._refresh_incumbent_score())
    # production reference-anchored bar is the STORED score; the shadow did NOT change it
    assert mgr._champion.benchmark_score == 0.52
    assert not getattr(mgr, "_incumbent_refresh_failed", False)
    # both legs ran: production (reference quotes) AND the self-quote shadow (None)
    assert ref in calls and None in calls


def test_refquote_shadow_off_skips_self_quote(monkeypatch):
    import asyncio
    monkeypatch.delenv("REFQUOTE_SHADOW", raising=False)
    calls = []
    mgr, ref = _shadow_harness(calls)
    asyncio.run(mgr._refresh_incumbent_score())
    assert None not in calls  # no self-quote shadow leg when the env is off
    assert mgr._champion.benchmark_score == 0.52


def test_refquote_shadow_failure_isolated(monkeypatch):
    import asyncio
    monkeypatch.setenv("REFQUOTE_SHADOW", "1")
    calls = []
    mgr, ref = _shadow_harness(calls, self_raises=True)
    asyncio.run(mgr._refresh_incumbent_score())
    # a shadow-leg failure must NEVER change the bar nor mark it stale
    assert mgr._champion.benchmark_score == 0.52
    assert not getattr(mgr, "_incumbent_refresh_failed", False)
    assert None in calls  # the shadow leg was attempted
