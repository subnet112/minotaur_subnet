"""Tests for the additive ``miner_uid`` field on submission responses.

``miner_uid`` is a CURRENT-metagraph lookup of the submitting hotkey's SN112
UID — null whenever the metagraph isn't synced or the hotkey is no longer
registered. The lookup must fail OPEN (null field), never 500 a read endpoint.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.routes import (
    _hotkey_to_uid_map,
    get_submission_status,
    list_submissions,
)

CTX_PATH = "minotaur_subnet.api.server_context.ctx"


def _synced_ctx(peers: list[tuple[str, int]]) -> SimpleNamespace:
    """A ctx whose metagraph sync has a synced state with the given peers."""
    state = SimpleNamespace(
        peers=[SimpleNamespace(hotkey=hk, uid=uid) for hk, uid in peers],
    )
    return SimpleNamespace(
        solver_round_metagraph_sync=SimpleNamespace(state=state),
    )


# ──────────────────────────────────────────────────────────────────────
# _hotkey_to_uid_map
# ──────────────────────────────────────────────────────────────────────


def test_uid_map_from_synced_state():
    ctx = _synced_ctx([("5FChampion", 7), ("5GOther", 42)])
    with patch(CTX_PATH, ctx):
        assert _hotkey_to_uid_map() == {"5FChampion": 7, "5GOther": 42}


def test_uid_map_empty_when_sync_is_none():
    with patch(CTX_PATH, SimpleNamespace(solver_round_metagraph_sync=None)):
        assert _hotkey_to_uid_map() == {}


def test_uid_map_empty_when_state_never_synced():
    ctx = SimpleNamespace(
        solver_round_metagraph_sync=SimpleNamespace(state=None),
    )
    with patch(CTX_PATH, ctx):
        assert _hotkey_to_uid_map() == {}


# ──────────────────────────────────────────────────────────────────────
# GET /submissions — _shape() adds miner_uid
# ──────────────────────────────────────────────────────────────────────


class _FakeSub:
    def __init__(self, submission_id: str, hotkey: str, created_at: float) -> None:
        self.submission_id = submission_id
        self.hotkey = hotkey
        self.created_at = created_at
        self.round_id = None

    def to_dict(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "hotkey": self.hotkey,
            "benchmark_details": {"heavy": True},
        }

    def status_dict(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "status": "benchmarked",
            "screening": {},
        }


class _FakeStore:
    def __init__(self, subs: list[_FakeSub]) -> None:
        self._submissions = {s.submission_id: s for s in subs}

    def get(self, submission_id: str):
        return self._submissions.get(submission_id)


def test_list_submissions_adds_miner_uid_for_registered_hotkey():
    store = _FakeStore([
        _FakeSub("sub-1", "5FChampion", created_at=2.0),
        _FakeSub("sub-2", "5GChurned", created_at=1.0),  # not in metagraph
    ])
    ctx = _synced_ctx([("5FChampion", 7)])
    with patch(
        "minotaur_subnet.api.routes.submissions.routes.get_store",
        return_value=store,
    ), patch(CTX_PATH, ctx):
        out = asyncio.run(list_submissions())
    by_id = {s["submission_id"]: s for s in out["submissions"]}
    assert by_id["sub-1"]["miner_uid"] == 7
    assert by_id["sub-2"]["miner_uid"] is None
    # Additive: hotkey untouched, heavy blob still stripped by default.
    assert by_id["sub-1"]["hotkey"] == "5FChampion"
    assert "benchmark_details" not in by_id["sub-1"]


def test_list_submissions_miner_uid_null_when_sync_unavailable():
    store = _FakeStore([_FakeSub("sub-1", "5FChampion", created_at=1.0)])
    with patch(
        "minotaur_subnet.api.routes.submissions.routes.get_store",
        return_value=store,
    ), patch(CTX_PATH, SimpleNamespace(solver_round_metagraph_sync=None)):
        out = asyncio.run(list_submissions())
    assert out["submissions"][0]["miner_uid"] is None


# ──────────────────────────────────────────────────────────────────────
# GET /submissions/{id}/status — miner_uid on the single-submission view
# ──────────────────────────────────────────────────────────────────────


def test_status_response_carries_miner_uid():
    store = _FakeStore([_FakeSub("sub-1", "5FChampion", created_at=1.0)])
    ctx = _synced_ctx([("5FChampion", 7)])
    with patch(
        "minotaur_subnet.api.routes.submissions.routes.get_store",
        return_value=store,
    ), patch(CTX_PATH, ctx):
        resp = asyncio.run(get_submission_status("sub-1"))
    assert resp.miner_uid == 7


def test_status_response_miner_uid_null_for_unregistered_hotkey():
    store = _FakeStore([_FakeSub("sub-1", "5GChurned", created_at=1.0)])
    ctx = _synced_ctx([("5FChampion", 7)])
    with patch(
        "minotaur_subnet.api.routes.submissions.routes.get_store",
        return_value=store,
    ), patch(CTX_PATH, ctx):
        resp = asyncio.run(get_submission_status("sub-1"))
    assert resp.miner_uid is None
