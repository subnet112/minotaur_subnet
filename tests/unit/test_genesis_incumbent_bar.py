"""Genesis-as-bar (#242, user decision): the FIRST champion must BEAT the genesis.

The leader seeds self._champion from a SCORED genesis (score>0) at decision time so
the adoption rule sees has_champion=True (must beat genesis*(1+margin) + floor + veto).
This must be WEIGHT-SAFE: the seeded hotkey is GENESIS_HOTKEY, which is_real_miner_hotkey
rejects, so _build_weights_mapping still burns 100% to the owner — identical to the
empty-champion case. These tests lock both properties.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
