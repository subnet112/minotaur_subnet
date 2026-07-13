"""Copycat labels must be SURFACED on the read endpoints the UI consumes.

The store computes is_copycat/display_name correctly (see
test_solver_name_copycat.py) — but the feature looked broken because the
dashboard's ``/submissions`` list and the ``/solver/champion`` view didn't carry
the derived ``display_name`` (the "-copycat"-suffixed name) or the resolved
``coined_by_uid``. These tests pin that the endpoints now expose them.
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

from minotaur_subnet.api.routes.submissions.round_manager import (
    _submission_to_champion_snapshot,
)
from minotaur_subnet.api.routes.submissions.routes import (
    get_solver_champion,
    list_submissions,
)
from minotaur_subnet.harness.submission_store import SubmissionStore

CTX_PATH = "minotaur_subnet.api.server_context.ctx"
ROUTES = "minotaur_subnet.api.routes.submissions.routes"


def _ctx(peers: list[tuple[str, int]]) -> SimpleNamespace:
    state = SimpleNamespace(
        peers=[SimpleNamespace(hotkey=hk, uid=uid) for hk, uid in peers],
    )
    return SimpleNamespace(solver_round_metagraph_sync=SimpleNamespace(state=state))


def _submit(store: SubmissionStore, hotkey: str, name: str, rid: str = "r1"):
    sub = store.create(
        repo_url="src://x", commit_hash="c-" + hotkey + name, epoch=1,
        hotkey=hotkey, round_id=rid, max_per_round=0, max_total_per_round=0,
    )
    store.set_solver_info(sub.submission_id, name=name, version="1.0")
    return store.get(sub.submission_id)


def test_list_submissions_surfaces_copycat_fields(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    _submit(store, "5AAA", "king")   # coiner
    _submit(store, "5BBB", "king")   # copycat
    ctx = _ctx([("5AAA", 1), ("5BBB", 60)])
    with patch(f"{ROUTES}.get_store", return_value=store), patch(CTX_PATH, ctx):
        out = asyncio.run(list_submissions())
    rows = {r["hotkey"]: r for r in out["submissions"]}
    # coiner: not a copycat, plain display name, no coiner UID
    assert rows["5AAA"]["is_copycat"] is False
    assert rows["5AAA"]["display_name"] == "king"
    assert rows["5AAA"]["coined_by_uid"] is None
    # copycat: flagged, suffixed display name, coiner UID resolved from metagraph
    assert rows["5BBB"]["is_copycat"] is True
    assert rows["5BBB"]["display_name"] == "king-copycat"
    assert rows["5BBB"]["coined_by_uid"] == 1


def test_champion_response_surfaces_copycat(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    _submit(store, "5AAA", "king")               # coiner
    champ = _submit(store, "5BBB", "king")        # a copycat that became champion
    snap = _submission_to_champion_snapshot(champ)
    round_store = SimpleNamespace(get_active_champion=lambda: snap)
    ctx = _ctx([("5AAA", 1), ("5BBB", 60)])
    with patch(f"{ROUTES}.get_store", return_value=store), \
         patch(f"{ROUTES}.get_round_store", return_value=round_store), \
         patch(f"{ROUTES}._sync_round_incumbent_from_submission_store",
               lambda *a, **k: None), \
         patch(CTX_PATH, ctx):
        resp = asyncio.run(get_solver_champion())
    assert resp.solver_name == "king"
    assert resp.is_copycat is True
    assert resp.display_name == "king-copycat"
    assert resp.coined_by_uid == 1


def test_champion_response_plain_for_the_coiner(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    champ = _submit(store, "5AAA", "king")        # the coiner is champion
    snap = _submission_to_champion_snapshot(champ)
    round_store = SimpleNamespace(get_active_champion=lambda: snap)
    ctx = _ctx([("5AAA", 1)])
    with patch(f"{ROUTES}.get_store", return_value=store), \
         patch(f"{ROUTES}.get_round_store", return_value=round_store), \
         patch(f"{ROUTES}._sync_round_incumbent_from_submission_store",
               lambda *a, **k: None), \
         patch(CTX_PATH, ctx):
        resp = asyncio.run(get_solver_champion())
    assert resp.is_copycat is False
    assert resp.display_name == "king"
    assert resp.coined_by_uid is None
