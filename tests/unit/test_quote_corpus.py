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

    def test_quote_draw_uses_distinct_salt_from_order_draw(self):
        # Feed the SAME id pool to BOTH samplers and prove the "{round_id}:quotes"
        # salt makes the quote draw select a DIFFERENT subset than the plain
        # round_id order draw — a regression dropping the salt would make these
        # equal and fail. Each row is both an order (status=filled) and a quote with
        # order_id == quote_id, so the only difference is the seed salt.
        from minotaur_subnet.harness.order_sampler import sample_historical_orders

        rows = []
        for i in range(60):
            rows.append({
                "order_id": f"x_{i:03d}", "quote_id": f"x_{i:03d}",
                "app_id": "app_test", "chain_id": 8453, "status": "filled",
                "intent_function": "swap",
                "params": {"input_token": "0xWETH", "output_token": f"0xOUT_{i}",
                           "input_amount": "1000000000000000000"},
            })

        class _Dual:
            def list_orders(self): return list(rows)
            def list_quotes(self): return list(rows)
            def list_apps(self): return []

        store = _Dual()
        order_ids = [o["order_id"] for o in sample_historical_orders(store, "round-1", n_per_chain=10)]
        quote_ids = [q["quote_id"] for q in sample_historical_quotes(store, "round-1", n_per_chain=10)]
        assert order_ids != quote_ids          # the salt changes the selection
        assert len(order_ids) == len(quote_ids) == 10
        # and the quote draw is still internally deterministic
        again = [q["quote_id"] for q in sample_historical_quotes(store, "round-1", n_per_chain=10)]
        assert again == quote_ids


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

    def test_prune_keeps_newest(self, tmp_path):
        s = self._store(tmp_path)
        for i in range(10):
            q = _make_quote(quote_id=f"q_{i:02d}")
            q["created_at"] = 1_700_000_000.0 + i  # ascending; higher = newer
            s.save_quote(q)
        pruned = s.prune_quotes(4)
        assert pruned == 6
        kept = {q["quote_id"] for q in s.list_quotes()}
        assert kept == {"q_06", "q_07", "q_08", "q_09"}
        assert s.prune_quotes(4) == 0  # idempotent at/under cap

    def test_list_ids_and_delete(self, tmp_path):
        s = self._store(tmp_path)
        for i in range(5):
            s.save_quote(_make_quote(quote_id=f"q_{i}"))
        assert s.list_quote_ids() == {f"q_{i}" for i in range(5)}
        removed = s.delete_quotes({"q_1", "q_3", "absent"})
        assert removed == 2
        assert s.list_quote_ids() == {"q_0", "q_2", "q_4"}


class TestCaptureStrip:
    """The capture-time strip must drop identity/derived params before storage,
    because quote cases are served on the PUBLIC /v1/quotes and replicated."""

    def test_strip_set_covers_identity_and_volatile(self):
        from minotaur_subnet.harness.order_sampler import QUOTE_PARAM_STRIP_FIELDS
        for k in ("receiver", "recipient", "to", "user_address", "intent_params_hex",
                  "app_address", "permit", "submitted_by", "user_signature",
                  "quoted_output", "platform_fee_wei"):
            assert k in QUOTE_PARAM_STRIP_FIELDS, k
        # trade-defining keys must SURVIVE
        for k in ("input_token", "output_token", "input_amount", "min_output_amount"):
            assert k not in QUOTE_PARAM_STRIP_FIELDS, k

    def test_stripped_params_have_no_identity(self):
        from minotaur_subnet.harness.order_sampler import QUOTE_PARAM_STRIP_FIELDS
        raw = {
            "input_token": "0xA", "output_token": "0xB", "input_amount": "10",
            "receiver": "0xUSERWALLET", "intent_params_hex": "0xowneraddr",
            "user_signature": "0xsig",
        }
        clean = {k: v for k, v in raw.items() if k not in QUOTE_PARAM_STRIP_FIELDS}
        assert clean == {"input_token": "0xA", "output_token": "0xB", "input_amount": "10"}
        assert "0xUSERWALLET" not in str(clean)


class _MemQuoteStore:
    """In-memory quote store for QuoteSync reconcile tests."""

    def __init__(self, existing=None):
        self.quotes = dict(existing or {})

    def save_quote(self, q):
        self.quotes[q["quote_id"]] = q

    def list_quote_ids(self):
        return set(self.quotes)

    def delete_quotes(self, ids):
        n = 0
        for i in list(ids):
            if i in self.quotes:
                del self.quotes[i]
                n += 1
        return n


class TestQuoteSync:
    import asyncio as _asyncio

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _sync(self, *, is_follower=True, leader_url="http://leader:8080",
              pages=None, store=None):
        from minotaur_subnet.blockloop.quote_sync import QuoteSync
        urls = []

        async def fake_get(url):
            urls.append(url)
            off = int(url.rsplit("offset=", 1)[1])
            return (pages or {}).get(off, [])

        s = QuoteSync(
            app_store=store if store is not None else _MemQuoteStore(),
            leader_api_url=lambda: leader_url,
            is_follower=lambda: is_follower,
            http_get=fake_get,
        )
        return s, urls

    def test_follower_pulls_and_upserts(self):
        store = _MemQuoteStore()
        pages = {0: [{"quote_id": "a"}, {"quote_id": "b"}]}
        s, urls = self._sync(pages=pages, store=store)
        n = self._run(s.sync_once())
        assert n == 2
        assert store.list_quote_ids() == {"a", "b"}
        assert urls[0] == "http://leader:8080/v1/quotes?full=1&limit=500&offset=0"

    def test_noop_on_leader(self):
        store = _MemQuoteStore()
        s, urls = self._sync(is_follower=False, store=store)
        assert self._run(s.sync_once()) == 0
        assert urls == []  # never hit the network

    def test_terminates_against_prepagination_leader(self):
        # A leader that ignores offset and returns the SAME full page every time
        # must NOT loop forever — the seen-set collapses it to one page.
        full = [{"quote_id": f"q{i}"} for i in range(500)]
        urls = []

        async def fake_get(url):
            urls.append(url)
            return full  # ignores offset

        from minotaur_subnet.blockloop.quote_sync import QuoteSync
        store = _MemQuoteStore()
        s = QuoteSync(app_store=store, leader_api_url=lambda: "http://l:8080",
                      is_follower=lambda: True, http_get=fake_get)
        n = self._run(s.sync_once())
        assert n == 500
        assert len(urls) == 2  # page 0 (full), page 500 (all seen -> fresh empty -> break)

    def test_reconcile_mirrors_leader_exactly(self):
        # Follower starts with a leader-absent local quote + a now-deleted leader
        # quote; after sync it must hold EXACTLY the leader's current set.
        store = _MemQuoteStore({
            "leader_kept": {"quote_id": "leader_kept"},
            "leader_deleted": {"quote_id": "leader_deleted"},
            "follower_local": {"quote_id": "follower_local"},  # never on leader
        })
        pages = {0: [{"quote_id": "leader_kept"}, {"quote_id": "leader_new"}]}
        s, _ = self._sync(pages=pages, store=store)
        self._run(s.sync_once())
        assert store.list_quote_ids() == {"leader_kept", "leader_new"}

    def test_empty_pull_never_reconciles(self):
        # A transient empty pull must NOT wipe the follower's mirror.
        store = _MemQuoteStore({"a": {"quote_id": "a"}, "b": {"quote_id": "b"}})
        s, _ = self._sync(pages={0: []}, store=store)
        assert self._run(s.sync_once()) == 0
        assert store.list_quote_ids() == {"a", "b"}  # untouched


class TestQuotesRoute:
    """The GET /v1/quotes route handler (sort, clamp, filters, no-store fallback)."""

    def _route(self):
        import minotaur_subnet.api.routes.orders as m
        return m

    def test_no_store_returns_empty_shape(self):
        m = self._route()
        prev = m._app_store
        try:
            m.set_app_store(None)
            out = m.list_quotes()
            assert out == {"quotes": [], "count": 0, "total": 0, "limit": 100, "offset": 0}
        finally:
            m.set_app_store(prev)

    def test_newest_first_and_clamp(self):
        m = self._route()

        class _S:
            def list_quotes(self, app_id=None, chain_id=None):
                return [
                    {"quote_id": "old", "created_at": 1.0},
                    {"quote_id": "new", "created_at": 9.0},
                ]

        prev = m._app_store
        try:
            m.set_app_store(_S())
            out = m.list_quotes(limit=99999)
            assert [q["quote_id"] for q in out["quotes"]] == ["new", "old"]  # newest first
            assert out["limit"] == m._LIST_MAX_LIMIT                          # clamped to 500
            assert out["total"] == 2
        finally:
            m.set_app_store(prev)
