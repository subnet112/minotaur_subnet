"""Tests for quote demand tracking in AppIntentStore."""

import pytest
import tempfile
from pathlib import Path

from minotaur_subnet.store.app_intent_store import AppIntentStore


@pytest.fixture
def store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "test_store.json")


class TestRecordQuoteAttempt:
    def test_successful_quote(self, store):
        store.record_quote_attempt("app-001", success=True)
        stats = store.get_quote_stats("app-001")
        assert stats["total_quotes"] == 1
        assert stats["failed_quotes"] == 0
        assert stats["success_rate"] == 1.0

    def test_failed_quote_with_error(self, store):
        store.record_quote_attempt("app-001", success=False, error="zero_output")
        stats = store.get_quote_stats("app-001")
        assert stats["total_quotes"] == 1
        assert stats["failed_quotes"] == 1
        assert stats["success_rate"] == 0.0
        assert "zero_output" in stats["recent_errors"]

    def test_mixed_quotes(self, store):
        store.record_quote_attempt("app-001", success=True)
        store.record_quote_attempt("app-001", success=True)
        store.record_quote_attempt("app-001", success=False, error="error: timeout")
        stats = store.get_quote_stats("app-001")
        assert stats["total_quotes"] == 3
        assert stats["failed_quotes"] == 1
        assert abs(stats["success_rate"] - 2 / 3) < 0.001

    def test_recent_errors_capped_at_20(self, store):
        for i in range(25):
            store.record_quote_attempt("app-001", success=False, error=f"err_{i}")
        stats = store.get_quote_stats("app-001")
        assert len(stats["recent_errors"]) == 20
        # Most recent should be kept
        assert stats["recent_errors"][-1] == "err_24"

    def test_nonexistent_app_returns_defaults(self, store):
        stats = store.get_quote_stats("nonexistent")
        assert stats["total_quotes"] == 0
        assert stats["failed_quotes"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["recent_errors"] == []

    def test_persists_across_reload(self, tmp_path):
        path = tmp_path / "persist_test.json"
        store1 = AppIntentStore(store_path=path)
        store1.record_quote_attempt("app-001", success=False, error="test_err")
        store1.record_quote_attempt("app-001", success=True)

        # Reload from disk
        store2 = AppIntentStore(store_path=path)
        stats = store2.get_quote_stats("app-001")
        assert stats["total_quotes"] == 2
        assert stats["failed_quotes"] == 1
        assert "test_err" in stats["recent_errors"]

    def test_multiple_apps_isolated(self, store):
        store.record_quote_attempt("app-001", success=True)
        store.record_quote_attempt("app-002", success=False, error="fail")
        s1 = store.get_quote_stats("app-001")
        s2 = store.get_quote_stats("app-002")
        assert s1["total_quotes"] == 1
        assert s1["failed_quotes"] == 0
        assert s2["total_quotes"] == 1
        assert s2["failed_quotes"] == 1
