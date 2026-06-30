"""Unit tests for the default store-persistence path (restart consolidation).

A node with no SOLVER_ROUND_STORE_PATH / SUBMISSION_STORE_PATH (e.g. a third-party
follower that only pulls the image, not the leader's compose) used to keep its round +
submission stores IN MEMORY — wiped on every container restart, so it lost the standing
champion's round and fell back to 100% burn. These stores now DEFAULT onto the app
store's persistent volume so they survive restarts. Pure path logic — no real store I/O.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.state import (
    _default_persist_path,
    get_round_store,
    set_round_store,
)


def _data_is_writable() -> bool:
    return Path("/data").is_dir() and os.access("/data", os.W_OK)


class TestDefaultPersistPath:
    def test_uses_app_store_volume(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_INTENTS_STORE_PATH", str(tmp_path / "store.json"))
        assert _default_persist_path("solver_rounds.json") == str(
            tmp_path / "solver_rounds.json"
        )

    def test_none_when_no_writable_volume(self, monkeypatch):
        monkeypatch.delenv("APP_INTENTS_STORE_PATH", raising=False)
        if not _data_is_writable():  # CI sandbox has no /data mount
            assert _default_persist_path("solver_rounds.json") is None

    def test_nonexistent_app_store_dir_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_INTENTS_STORE_PATH", str(tmp_path / "nope" / "store.json"))
        if not _data_is_writable():
            assert _default_persist_path("x.json") is None


class TestGetterDefaultsToVolume:
    def test_round_store_persists_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_INTENTS_STORE_PATH", str(tmp_path / "store.json"))
        monkeypatch.delenv("SOLVER_ROUND_STORE_PATH", raising=False)
        set_round_store(None)
        try:
            rs = get_round_store()
            assert rs._persist_path == tmp_path / "solver_rounds.json"
        finally:
            set_round_store(None)

    def test_explicit_env_takes_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_INTENTS_STORE_PATH", str(tmp_path / "store.json"))
        monkeypatch.setenv("SOLVER_ROUND_STORE_PATH", str(tmp_path / "custom.json"))
        set_round_store(None)
        try:
            rs = get_round_store()
            assert rs._persist_path == tmp_path / "custom.json"
        finally:
            set_round_store(None)


class TestFailLoudDurableState:
    """require_durable_state() makes the getters CRASH instead of silently running
    in-memory — a mis-volumed production node refuses to boot rather than losing state."""

    def test_off_by_default_returns_none(self, monkeypatch):
        import minotaur_subnet.api.routes.submissions.state as st
        monkeypatch.delenv("SUBMISSION_STORE_PATH", raising=False)
        monkeypatch.delenv("APP_INTENTS_STORE_PATH", raising=False)
        if not _data_is_writable():  # default (not required): legacy None, no raise
            assert st._resolve_persist_path("submissions.json", "SUBMISSION_STORE_PATH") is None

    def test_required_raises_when_unresolvable(self, monkeypatch):
        import pytest
        import minotaur_subnet.api.routes.submissions.state as st
        monkeypatch.delenv("SUBMISSION_STORE_PATH", raising=False)
        monkeypatch.delenv("APP_INTENTS_STORE_PATH", raising=False)
        if _data_is_writable():
            pytest.skip("/data writable here; cannot exercise the unresolvable branch")
        monkeypatch.setattr(st, "_REQUIRE_DURABLE_STATE", True)  # auto-reset after test
        with pytest.raises(RuntimeError):
            st._resolve_persist_path("submissions.json", "SUBMISSION_STORE_PATH")

    def test_required_returns_env_path(self, monkeypatch):
        import minotaur_subnet.api.routes.submissions.state as st
        monkeypatch.setattr(st, "_REQUIRE_DURABLE_STATE", True)
        monkeypatch.setenv("SUBMISSION_STORE_PATH", "/srv/data/submissions.json")
        assert (
            st._resolve_persist_path("submissions.json", "SUBMISSION_STORE_PATH")
            == "/srv/data/submissions.json"
        )
