"""Tests for limit/offset pagination on GET /v1/submissions.

The endpoint historically returned the FULL submission corpus (~20k rows,
~44 MB) on every unfiltered call, which drove the bulk of validator egress —
even though many clients already sent ``?limit=N`` (silently ignored). These
tests pin the behaviour: honour limit/offset (newest-first), **default to the
50 newest rows** (subnet #990 — an omitted ``limit`` no longer dumps the whole
corpus; pass ``limit=0`` to opt back in), and report the unpaginated ``total``.
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

from minotaur_subnet.api.routes.submissions.routes import list_submissions

CTX_PATH = "minotaur_subnet.api.server_context.ctx"
_NO_SYNC = SimpleNamespace(solver_round_metagraph_sync=None)


class _FakeSub:
    def __init__(self, sid: str, created_at: float) -> None:
        self.submission_id = sid
        self.hotkey = "5F" + sid
        self.created_at = created_at
        self.round_id = None

    def to_dict(self) -> dict:
        return {"submission_id": self.submission_id, "hotkey": self.hotkey}


class _FakeStore:
    def __init__(self, subs) -> None:
        self._submissions = {s.submission_id: s for s in subs}


def _run(store, **kwargs):
    with patch(
        "minotaur_subnet.api.routes.submissions.routes.get_store", return_value=store
    ), patch(CTX_PATH, _NO_SYNC):
        return asyncio.run(list_submissions(**kwargs))


def _store_n(n: int) -> _FakeStore:
    # created_at ascending with index; newest (highest created_at) is sub-<n-1>.
    return _FakeStore([_FakeSub(f"sub-{i}", created_at=float(i)) for i in range(n)])


def test_default_caps_at_50():
    # Omitting ``limit`` returns the 50 NEWEST rows, not the full corpus
    # (subnet #990 — the unbounded no-param poll drove the bulk of egress).
    out = _run(_store_n(60))
    assert out["count"] == 50
    assert out["total"] == 60
    assert out["limit"] == 50
    assert out["offset"] == 0
    assert len(out["submissions"]) == 50
    # Newest-first: the 50 returned are sub-59 .. sub-10.
    assert out["submissions"][0]["submission_id"] == "sub-59"
    assert out["submissions"][-1]["submission_id"] == "sub-10"


def test_limit_zero_opts_into_full_corpus():
    # Explicit ``limit=0`` still returns everything — the opt-in escape hatch for
    # the rare caller that genuinely needs the whole corpus.
    out = _run(_store_n(60), limit=0)
    assert out["count"] == 60
    assert out["total"] == 60
    assert out["limit"] == 0


def test_limit_returns_newest_n():
    out = _run(_store_n(10), limit=3)
    assert out["count"] == 3
    assert out["total"] == 10
    assert out["limit"] == 3
    assert [s["submission_id"] for s in out["submissions"]] == ["sub-9", "sub-8", "sub-7"]


def test_offset_then_limit_paginates():
    out = _run(_store_n(10), limit=2, offset=3)
    assert out["total"] == 10
    assert out["offset"] == 3
    assert [s["submission_id"] for s in out["submissions"]] == ["sub-6", "sub-5"]


def test_limit_larger_than_total_returns_all():
    out = _run(_store_n(4), limit=100)
    assert out["count"] == 4
    assert out["total"] == 4


def test_negative_and_zero_clamp_safely():
    out = _run(_store_n(3), limit=-1, offset=-5)
    assert out["count"] == 3
    assert out["offset"] == 0
    assert out["limit"] == 0


def test_offset_past_end_is_empty_not_error():
    out = _run(_store_n(3), offset=10)
    assert out["count"] == 0
    assert out["total"] == 3
    assert out["submissions"] == []
