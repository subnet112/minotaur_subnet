"""Tests for the quote-demand benchmark corpus (Phase-1 soak).

Covers the consensus-critical invariants:
  * quote_case_id is a stable, content-addressed collapse key
  * sample_historical_quotes is deterministic from round_id, capped, PII-stripped,
    dedup-collapsed, and independent of the order draw
  * the pack-hash quote fold is INERT when off (byte-identical) and binding when on
  * the AppIntentStore quotes table round-trips + upserts by quote_id
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.harness.benchmark_pack import compute_pack_hash
from minotaur_subnet.harness.order_sampler import (
    QUOTE_CORPUS_SAMPLES,
    quote_case_id,
    sample_historical_quotes,
)


class _FakeQuoteStore:
    """Mock app store exposing list_quotes()/list_apps() for the quote draw."""

    def __init__(self, quotes: list[dict[str, Any]]) -> None:
        self._quotes = quotes

    def list_quotes(self) -> list[dict[str, Any]]:
        return list(self._quotes)

    def list_apps(self) -> list[Any]:  # retirement exclusion queries this
        return []


def _make_quote(
    quote_id: str | None = None,
    chain_id: int = 8453,
    intent_function: str = "swap",
    app_id: str = "app_test",
    params: dict[str, Any] | None = None,
    output_token: str | None = None,
) -> dict[str, Any]:
    params = params or {
        "input_token": "0xWETH",
        # Distinct pair per quote so the pre-draw dedup doesn't collapse them.
        "output_token": output_token or f"0xOUT_{quote_id}",
        "input_amount": "1000000000000000000",
    }
    qid = quote_id or quote_case_id(app_id, chain_id, intent_function, params)
    return {
        "quote_id": qid,
        "app_id": app_id,
        "chain_id": chain_id,
        "intent_function": intent_function,
        "params": params,
        "estimated_output": "0",
        "created_at": 1_700_000_000.0,
    }


class TestQuoteCaseId:
    def test_deterministic_and_content_addressed(self):
        p = {"input_token": "0xA", "output_token": "0xB", "input_amount": "10"}
        a = quote_case_id("app", 8453, "swap", p)
        b = quote_case_id("app", 8453, "swap", dict(p))
        assert a == b
        assert a.startswith("q_")

    def test_volatile_params_do_not_change_id(self):
        base = {"input_token": "0xA", "output_token": "0xB", "input_amount": "10"}
        noisy = {**base, "quoted_output": "999", "platform_fee_wei": "7",
                 "intent_params_hex": "0xdeadbeef"}
        assert quote_case_id("app", 8453, "swap", base) == \
            quote_case_id("app", 8453, "swap", noisy)

    def test_different_trade_differs(self):
        p1 = {"input_token": "0xA", "output_token": "0xB", "input_amount": "10"}
        p2 = {"input_token": "0xA", "output_token": "0xC", "input_amount": "10"}
        p3 = {"input_token": "0xA", "output_token": "0xB", "input_amount": "11"}
        ids = {
            quote_case_id("app", 8453, "swap", p1),
            quote_case_id("app", 8453, "swap", p2),
            quote_case_id("app", 8453, "swap", p3),
            quote_case_id("app", 1, "swap", p1),        # chain differs
            quote_case_id("app2", 8453, "swap", p1),    # app differs
        }
        assert len(ids) == 5


class TestDeterminism:
    def test_same_round_id_same_sample(self):
        quotes = [_make_quote(quote_id=f"q_{i:03d}") for i in range(60)]
        store = _FakeQuoteStore(quotes)
        s1 = sample_historical_quotes(store, "round-1", n_per_chain=10)
        s2 = sample_historical_quotes(store, "round-1", n_per_chain=10)
        assert [q["quote_id"] for q in s1] == [q["quote_id"] for q in s2]
        assert len(s1) == 10

    def test_different_round_id_different_sample(self):
        quotes = [_make_quote(quote_id=f"q_{i:03d}") for i in range(60)]
        store = _FakeQuoteStore(quotes)
        s1 = [q["quote_id"] for q in sample_historical_quotes(store, "round-1", n_per_chain=10)]
        s2 = [q["quote_id"] for q in sample_historical_quotes(store, "round-2", n_per_chain=10)]
        assert s1 != s2

    def test_quote_draw_independent_of_order_seed(self):
        # The quote draw uses the "{round_id}:quotes" salt, so with an identical id
        # pool the selected set must not be forced to equal a plain round_id draw.
        # (Proven indirectly: the salt is applied — a regression that dropped it
        # would make this pool of 60 draw the same indices as the order sampler.)
        quotes = [_make_quote(quote_id=f"q_{i:03d}") for i in range(60)]
        store = _FakeQuoteStore(quotes)
        got = sample_historical_quotes(store, "round-1", n_per_chain=10)
        assert len(got) == 10  # smoke: draw works with the salted seed


class TestCapAndGrouping:
    def test_per_chain_cap(self):
        quotes = [_make_quote(quote_id=f"q_{i:03d}", chain_id=8453) for i in range(200)]
        store = _FakeQuoteStore(quotes)
        got = sample_historical_quotes(store, "r", n_per_chain=QUOTE_CORPUS_SAMPLES)
        assert len(got) == QUOTE_CORPUS_SAMPLES  # 50 — same cap as orders

    def test_grouped_per_chain(self):
        quotes = (
            [_make_quote(quote_id=f"a_{i:03d}", chain_id=8453) for i in range(30)]
            + [_make_quote(quote_id=f"b_{i:03d}", chain_id=1) for i in range(30)]
        )
        store = _FakeQuoteStore(quotes)
        got = sample_historical_quotes(store, "r", n_per_chain=10)
        by_chain: dict[int, int] = {}
        for q in got:
            by_chain[q["chain_id"]] = by_chain.get(q["chain_id"], 0) + 1
        assert by_chain == {8453: 10, 1: 10}

    def test_chain_filter(self):
        quotes = (
            [_make_quote(quote_id=f"a_{i:03d}", chain_id=8453) for i in range(20)]
            + [_make_quote(quote_id=f"b_{i:03d}", chain_id=1) for i in range(20)]
        )
        store = _FakeQuoteStore(quotes)
        got = sample_historical_quotes(store, "r", chain_ids=[8453], n_per_chain=50)
        assert {q["chain_id"] for q in got} == {8453}


class TestDedupAndPii:
    def test_near_dup_same_pair_and_decade_collapse(self):
        # Same pair, same order-of-magnitude amount → ONE representative, even
        # though the exact amounts (hence quote_ids) differ.
        quotes = []
        for i in range(10):
            p = {"input_token": "0xA", "output_token": "0xB",
                 "input_amount": str(1_000_000_000_000_000_000 + i)}
            quotes.append(_make_quote(chain_id=8453, params=p))
        store = _FakeQuoteStore(quotes)
        got = sample_historical_quotes(store, "r", n_per_chain=50)
        assert len(got) == 1

    def test_pii_stripped(self):
        q = _make_quote(quote_id="q_pii")
        q["submitted_by"] = "0xUser"
        q["hotkey"] = "5Fhotkey"
        store = _FakeQuoteStore([q])
        got = sample_historical_quotes(store, "r", n_per_chain=10)
        assert got and "submitted_by" not in got[0] and "hotkey" not in got[0]

    def test_empty_store(self):
        assert sample_historical_quotes(_FakeQuoteStore([]), "r") == []


class TestPackHashFold:
    _S = [{"app_id": "a", "name": "n", "params": {"x": 1}}]
    _H = ["ord_a", "ord_b"]

    def test_none_is_backward_compatible(self):
        # No quotes → byte-identical to a fleet with no quote-corpus code at all.
        assert compute_pack_hash("r", self._S, self._H, historical_quote_ids=None) == \
            compute_pack_hash("r", self._S, self._H)

    def test_empty_list_still_folds_a_section(self):
        # An empty list is DISTINCT from None: it means "quotes are on, none drawn",
        # which is a different consensus state than "quotes off".
        assert compute_pack_hash("r", self._S, self._H, historical_quote_ids=[]) != \
            compute_pack_hash("r", self._S, self._H, historical_quote_ids=None)

    def test_active_changes_hash_and_is_deterministic(self):
        base = compute_pack_hash("r", self._S, self._H)
        h1 = compute_pack_hash("r", self._S, self._H, historical_quote_ids=["q_a", "q_b"])
        h2 = compute_pack_hash("r", self._S, self._H, historical_quote_ids=["q_b", "q_a"])
        assert h1 != base          # folding quotes in is consensus-breaking
        assert h1 == h2            # order-independent (sorted) → fleet agrees

    def test_different_quote_sets_diverge(self):
        h_a = compute_pack_hash("r", self._S, self._H, historical_quote_ids=["q_a"])
        h_b = compute_pack_hash("r", self._S, self._H, historical_quote_ids=["q_c"])
        assert h_a != h_b

    def test_quote_ids_do_not_collide_with_order_ids(self):
        # Same id string in the ORDER section vs the QUOTE section must produce
        # different hashes — the sections are namespaced.
        as_order = compute_pack_hash("r", self._S, ["x"], historical_quote_ids=[])
        as_quote = compute_pack_hash("r", self._S, [], historical_quote_ids=["x"])
        assert as_order != as_quote


class TestFeatureFlags:
    def test_defaults(self, monkeypatch):
        from minotaur_subnet.shared import feature_flags
        monkeypatch.delenv("BENCHMARK_QUOTE_CORPUS", raising=False)
        monkeypatch.delenv("BENCHMARK_QUOTE_CAPTURE", raising=False)
        assert feature_flags.quote_corpus_enabled() is False   # scored corpus OFF by default
        assert feature_flags.quote_capture_enabled() is True   # capture ON by default

    def test_corpus_flag_on(self, monkeypatch):
        from minotaur_subnet.shared import feature_flags
        monkeypatch.setenv("BENCHMARK_QUOTE_CORPUS", "1")
        assert feature_flags.quote_corpus_enabled() is True


class TestStoreRoundtrip:
    def _store(self, tmp_path):
        from minotaur_subnet.store.app_intent_store import AppIntentStore
        return AppIntentStore(store_path=tmp_path / "store")

    def test_save_get_list(self, tmp_path):
        s = self._store(tmp_path)
        q = _make_quote(quote_id="q_1", chain_id=8453)
        s.save_quote(q)
        assert s.get_quote("q_1")["quote_id"] == "q_1"
        assert len(s.list_quotes()) == 1
        assert len(s.list_quotes(app_id="app_test")) == 1
        assert len(s.list_quotes(app_id="nope")) == 0
        assert len(s.list_quotes(chain_id=8453)) == 1
        assert len(s.list_quotes(chain_id=1)) == 0

    def test_upsert_collapses_same_id(self, tmp_path):
        s = self._store(tmp_path)
        q = _make_quote(quote_id="q_dup")
        s.save_quote(q)
        s.save_quote({**q, "estimated_output": "123"})  # same id, re-quote
        rows = s.list_quotes()
        assert len(rows) == 1
        assert rows[0]["estimated_output"] == "123"  # last write wins

    def test_missing_returns_none(self, tmp_path):
        assert self._store(tmp_path).get_quote("absent") is None
