"""Tests for the reference-quote pre-pass checkpoint (memory + /data).

The pre-pass ran ~3x per round (once per run_once pass as submissions trickle
in, once per incumbent re-score) and was lost on every api restart. It is fully
determined by (round, champion image, fork block, corpus, scoring-JS), so it is
now memoized process-wide and checkpointed next to the submission store — a
restart resumes the round with the SAME reference set.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from minotaur_subnet.harness import benchmark_worker as bw
from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
from minotaur_subnet.harness.submission_store import SubmissionStore

QUOTES = {"app1": {"quoted_output": "123"}, "app2:swap": {"__reference_quote_failed__": "1"}}


def _worker(tmp_path: Path, *, round_id="round-e1-n1", fork_block=48000000):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    w = BenchmarkWorker(submission_store=store, use_docker=False)
    w._round_store = SimpleNamespace(
        get_current_round=lambda: SimpleNamespace(round_id=round_id) if round_id else None,
    )
    w._epoch_block_number = fork_block
    # deterministic fingerprints (the real ones hash the corpus / loaded JS)
    w._corpus_fingerprint = lambda intents: "corpus-fp"
    w._loaded_js_fingerprint = lambda intents: "js-fp"
    return w


def _stub_build(w, counter, result=QUOTES):
    async def _build(intents, *, image_tag=None):
        counter["n"] += 1
        return result
    w._build_reference_quotes = _build


def _get(w, image_tag="img:champ"):
    return asyncio.run(w._get_or_build_reference_quotes([], image_tag=image_tag))


def test_memoized_within_process(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w, c = _worker(tmp_path), {"n": 0}
    _stub_build(w, c)
    assert _get(w) == QUOTES
    assert _get(w) == QUOTES
    assert c["n"] == 1  # second call served from the memo


def test_checkpoint_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w1, c1 = _worker(tmp_path), {"n": 0}
    _stub_build(w1, c1)
    assert _get(w1) == QUOTES and c1["n"] == 1
    # "restart": fresh process-wide memo + fresh worker over the same /data
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w2, c2 = _worker(tmp_path), {"n": 0}
    _stub_build(w2, c2)
    assert _get(w2) == QUOTES
    assert c2["n"] == 0  # resumed from the /data checkpoint, no champion session


def test_key_sensitivity_rebuilds(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w, c = _worker(tmp_path), {"n": 0}
    _stub_build(w, c)
    _get(w)
    w._epoch_block_number = 48000001  # new fork block -> different state
    _get(w)
    w2, _ = _worker(tmp_path, round_id="round-e2-n1"), None  # new round
    _stub_build(w2, c)
    _get(w2)
    assert c["n"] == 3  # every key component change recomputed


def test_empty_result_not_frozen(tmp_path, monkeypatch):
    # {} = champion session failed to start (transient) — must retry next pass,
    # never checkpoint an empty reference set for the round.
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w, c = _worker(tmp_path), {"n": 0}
    _stub_build(w, c, result={})
    assert _get(w) == {}
    assert _get(w) == {}
    assert c["n"] == 2
    _stub_build(w, c, result=QUOTES)  # champion healthy again
    assert _get(w) == QUOTES and c["n"] == 3
    assert _get(w) == QUOTES and c["n"] == 3  # now cached


def test_kill_switch_disables(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    monkeypatch.setenv("BENCHMARK_REFQUOTE_CHECKPOINT", "0")
    w, c = _worker(tmp_path), {"n": 0}
    _stub_build(w, c)
    _get(w)
    _get(w)
    assert c["n"] == 2  # recompute every pass (legacy behavior)


def test_missing_key_components_fall_through(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w, c = _worker(tmp_path, round_id=None), {"n": 0}  # no current round
    _stub_build(w, c)
    _get(w)
    _get(w)
    assert c["n"] == 2  # uncacheable -> plain build each time


def test_disk_file_bounded_and_well_formed(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_REFERENCE_QUOTES_CACHE", {})
    w, c = _worker(tmp_path), {"n": 0}
    _stub_build(w, c)
    for i in range(bw._REFQUOTE_CHECKPOINT_KEEP + 3):
        w._epoch_block_number = 48000000 + i
        _get(w)
    data = json.loads((tmp_path / "refquote_checkpoints.json").read_text())
    assert len(data["entries"]) == bw._REFQUOTE_CHECKPOINT_KEEP  # trimmed
    assert all("key" in e and "quotes" in e for e in data["entries"])
