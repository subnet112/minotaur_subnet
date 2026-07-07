"""PR: champion-run consolidation (CONSOLIDATE_CHAMPION_BENCH).

A contested round benchmarks the champion via TWO independent full-benchmark
paths on a follower: the dethrone re-bench (_refresh_incumbent_score, on the
EpochManager's worker) and the trustless quorum verdict (_independent_adopt_vote,
on a FRESHLY-constructed worker). Both score the SAME round-seeded corpus, so the
champion's result is identical. The memo lets the second path reuse the first's
result — across worker instances (process-wide cache) — but ONLY on an exact key
match (round_id, image, fork_block, real-sim, corpus fingerprint, scoring-JS
fingerprint), so a hit is provably the same
deterministic computation and the verdict never shifts.
"""

import asyncio

import pytest

from minotaur_subnet.harness import benchmark_worker as bw
from minotaur_subnet.harness.benchmark_worker import (
    BenchmarkWorker,
    _clear_champion_bench_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_champion_bench_cache()
    yield
    _clear_champion_bench_cache()


class _State:
    def __init__(self, name=""):
        self._n = name

    def control_view(self):
        return {"_scenario_name": self._n}


class _Intent:
    def __init__(self, app_id):
        self.app_id = app_id


def _intents(*specs):
    """specs: (app_id, scenario_name) tuples → benchmark intent tuples."""
    return [(_Intent(a), _State(n), object()) for a, n in specs]


def _worker():
    return BenchmarkWorker(submission_store=object(), use_docker=False)


def _counting_run(result, counter):
    async def _run():
        counter["n"] += 1
        return result
    return _run


_CORPUS = lambda: _intents(("a", ""), ("b", "swap1"))  # noqa: E731


def _call(w, run, *, round_id="r1", image="img", fork_block=100, intents=None,
          real_sim=True):
    return asyncio.run(w.memo_champion_bench(
        round_id=round_id, image=image, fork_block=fork_block,
        intents=intents if intents is not None else _CORPUS(),
        require_real_sim=real_sim, run=run,
    ))


# ── flag gating ──────────────────────────────────────────────────────────────

def test_flag_off_never_caches(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "0")  # explicit kill switch
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    assert _call(w, run) == ["R"]
    assert _call(w, run) == ["R"]
    assert c["n"] == 2  # ran both times — no cache when flag off


def test_flag_default_on_caches(monkeypatch):
    # The flag was never set in production (leader audit 2026-07-02), so the
    # memo was inert and every round paid the redundant champion run — the
    # default is now ON, with the env var as the kill switch.
    monkeypatch.delenv("CONSOLIDATE_CHAMPION_BENCH", raising=False)
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    assert _call(w, run) == ["R"]
    assert _call(w, run) == ["R"]
    assert c["n"] == 1  # cached on the identical key by default


def test_flag_on_reuses_on_identical_key(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    first = _call(w, run)
    second = _call(w, run)  # identical key → cache hit
    assert first == ["R"] and second == ["R"]
    assert c["n"] == 1  # the champion ran ONCE; the second call reused it


# ── the CRITICAL fix: share across worker INSTANCES ──────────────────────────

def test_cross_worker_instances_share_cache(monkeypatch):
    # The dethrone path uses the manager's worker; the quorum verdict builds a
    # FRESH worker. A per-instance cache would never share → the consolidation
    # would be a no-op. The process-wide cache must let instance B reuse A's run.
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    worker_a, worker_b, c = _worker(), _worker(), {"n": 0}
    assert worker_a is not worker_b
    run = _counting_run(["R"], c)
    _call(worker_a, run)            # instance A caches
    out = _call(worker_b, run)      # instance B hits the shared cache
    assert out == ["R"]
    assert c["n"] == 1


# ── key discrimination: any difference → recompute (safe) ────────────────────

def test_distinct_keys_each_recompute(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    _call(w, run)                                   # baseline
    _call(w, run, image="img2")                     # different champion image
    _call(w, run, fork_block=200)                   # different fork block
    _call(w, run, real_sim=False)                   # different real-sim mode
    _call(w, run, intents=_intents(("a", "OTHER"))) # different corpus
    _call(w, run, round_id="r2")                    # different round
    assert c["n"] == 6  # every distinct key recomputed — never a wrong reuse


def test_js_version_change_invalidates(monkeypatch):
    # A mid-round PUT /scoring hot-reload changes _loaded_js_hashes → the JS
    # fingerprint changes → the memo must NOT reuse the old-JS-scored result.
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    w._loaded_js_hashes = {"a": "v1", "b": "v1"}
    _call(w, run)
    _call(w, run)                       # same JS → hit
    assert c["n"] == 1
    w._loaded_js_hashes = {"a": "v2", "b": "v1"}  # app 'a' scoring updated
    _call(w, run)                       # different JS fingerprint → recompute
    assert c["n"] == 2


def test_missing_round_or_image_skips_cache(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    _call(w, run, round_id=None)
    _call(w, run, round_id=None)  # still not cached
    _call(w, run, image=None)
    assert c["n"] == 3


def test_fork_block_none_skips_cache(monkeypatch):
    # A None pin = live-head (dev). Reuse across live-head blocks is unsafe, so the
    # memo is disabled when fork_block is None.
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    _call(w, run, fork_block=None)
    _call(w, run, fork_block=None)
    assert c["n"] == 2


# ── bounded memo + safety ────────────────────────────────────────────────────

def test_new_round_evicts_prior_round(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}
    run = _counting_run(["R"], c)
    _call(w, run, round_id="r1")
    _call(w, run, round_id="r2")      # inserts r2, evicts r1
    _call(w, run, round_id="r1")      # r1 evicted → recompute
    assert c["n"] == 3
    assert all(k[0] == "r1" for k in bw._CHAMPION_BENCH_CACHE)  # only current round kept


def test_non_list_result_not_cached(monkeypatch):
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w, c = _worker(), {"n": 0}

    async def run():
        c["n"] += 1
        return None  # a failed benchmark must NOT be cached as the champion bar

    asyncio.run(w.memo_champion_bench(round_id="r1", image="img", fork_block=100,
                                      intents=_CORPUS(), require_real_sim=True, run=run))
    asyncio.run(w.memo_champion_bench(round_id="r1", image="img", fork_block=100,
                                      intents=_CORPUS(), require_real_sim=True, run=run))
    assert c["n"] == 2


def test_run_exception_propagates(monkeypatch):
    # RealSimulationUnavailable et al. must surface to the caller (vote REJECT),
    # not be swallowed by the memo.
    monkeypatch.setenv("CONSOLIDATE_CHAMPION_BENCH", "1")
    w = _worker()

    async def run():
        raise RuntimeError("real sim unavailable")

    with pytest.raises(RuntimeError):
        asyncio.run(w.memo_champion_bench(round_id="r1", image="img", fork_block=100,
                                          intents=_CORPUS(), require_real_sim=True, run=run))


# ── fingerprints ─────────────────────────────────────────────────────────────

def test_corpus_fingerprint_stable_and_discriminating():
    w = _worker()
    f1 = w._corpus_fingerprint(_intents(("a", "x"), ("b", "")))
    f2 = w._corpus_fingerprint(_intents(("a", "x"), ("b", "")))
    f3 = w._corpus_fingerprint(_intents(("a", "y"), ("b", "")))  # scenario differs
    f4 = w._corpus_fingerprint(_intents(("b", ""), ("a", "x")))  # order differs
    assert f1 == f2
    assert f1 != f3 and f1 != f4


def test_js_fingerprint_tracks_loaded_hashes():
    w = _worker()
    w._loaded_js_hashes = {"a": "v1", "b": "v1"}
    base = w._loaded_js_fingerprint(_intents(("a", ""), ("b", "")))
    assert base == w._loaded_js_fingerprint(_intents(("a", ""), ("b", "")))
    w._loaded_js_hashes = {"a": "v2", "b": "v1"}
    assert base != w._loaded_js_fingerprint(_intents(("a", ""), ("b", "")))
