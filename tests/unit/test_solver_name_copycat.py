"""First-to-coin copycat labeling for solver names.

The solver display name is self-declared in the miner's ``metadata().name``
(free text, no uniqueness or identity binding), so multiple hotkeys submit the
same name. The store coins each distinct, non-boilerplate name to the first
hotkey that submits it and flags a later DIFFERENT hotkey reusing it. Cosmetic
only — no scoring effect. See minotaur_subnet/harness/submission_store.py.
"""
from __future__ import annotations

import json

from minotaur_subnet.harness.submission_store import (
    SubmissionStore,
    _exempt_names,
    _normalize_solver_name,
)


def _submit(store: SubmissionStore, hotkey: str, name: str, rid: str = "r1"):
    sub = store.create(
        repo_url="src://x",
        commit_hash="c-" + hotkey + "-" + name,
        epoch=1,
        hotkey=hotkey,
        round_id=rid,
        max_per_round=0,
        max_total_per_round=0,
    )
    store.set_solver_info(sub.submission_id, name=name, version="1.0")
    return store.get(sub.submission_id)


# ── normalization ───────────────────────────────────────────────────────────

def test_normalize_folds_case_whitespace_and_zero_width():
    assert _normalize_solver_name("King ") == "king"
    assert _normalize_solver_name("  King\tSolver ") == "king solver"
    # zero-width joiner embedded mid-word folds away
    assert _normalize_solver_name("k​ing") == _normalize_solver_name("king")
    assert _normalize_solver_name("") == ""
    assert _normalize_solver_name(None) == ""


def test_boilerplate_names_are_exempt():
    exempt = _exempt_names()
    assert _normalize_solver_name("baseline-swap-solver") in exempt
    assert _normalize_solver_name("my-swap-solver") in exempt
    # a distinctive name is coinable
    assert _normalize_solver_name("king") not in exempt


# ── coinage / copycat ───────────────────────────────────────────────────────

def test_first_hotkey_coins_second_is_copycat(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    a = _submit(store, "5AAA", "king")
    b = _submit(store, "5BBB", "king")
    assert a.is_copycat is False and a.coined_by_hotkey is None
    assert a.display_name == "king"
    assert b.is_copycat is True and b.coined_by_hotkey == "5AAA"
    assert b.display_name == "king-copycat"


def test_same_hotkey_reusing_own_name_is_not_copycat(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    _submit(store, "5AAA", "king")
    # case + trailing space variant, same owner → still not a copycat
    again = _submit(store, "5AAA", "King ", rid="r2")
    assert again.is_copycat is False


def test_zero_width_evasion_still_flagged(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    _submit(store, "5AAA", "king")
    evader = _submit(store, "5CCC", "k​ing")
    assert evader.is_copycat is True and evader.coined_by_hotkey == "5AAA"


def test_boilerplate_never_flags_anyone(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    x = _submit(store, "5DDD", "baseline-swap-solver")
    y = _submit(store, "5EEE", "baseline-swap-solver")
    assert x.is_copycat is False and y.is_copycat is False


def test_env_override_extends_exempt_list(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_NAME_UNCOINABLE", "acme,  Foo Bar ")
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    x = _submit(store, "5DDD", "acme")
    y = _submit(store, "5EEE", "foo bar")
    assert x.is_copycat is False and y.is_copycat is False


# ── serialization ───────────────────────────────────────────────────────────

def test_fields_round_trip_through_to_dict_and_status_dict(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "submissions.json")
    _submit(store, "5AAA", "king")
    b = _submit(store, "5BBB", "king")
    d = b.to_dict()
    assert d["is_copycat"] is True and d["coined_by_hotkey"] == "5AAA"
    sd = b.status_dict()
    assert sd["is_copycat"] is True and sd["display_name"] == "king-copycat"


# ── persistence: sidecar, restart, pruning-independence ──────────────────────

def test_registry_persists_in_sidecar_and_survives_restart(tmp_path):
    path = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=path)
    a = _submit(store, "5AAA", "king")
    b = _submit(store, "5BBB", "king")

    sidecar = path.with_name(path.name + ".names")
    assert sidecar.exists()
    reg = json.loads(sidecar.read_text())
    assert reg["king"]["owner_hotkey"] == "5AAA"

    # New store from the same path (simulated restart) reloads the registry and
    # the persisted copycat flags, and keeps coining against the same owner.
    store2 = SubmissionStore(persist_path=path)
    assert store2.get(b.submission_id).is_copycat is True
    z = _submit(store2, "5ZZZ", "KING", rid="r3")  # case variant, new hotkey
    assert z.is_copycat is True and z.coined_by_hotkey == "5AAA"


def test_backfill_seeds_earliest_coiner_when_sidecar_absent(tmp_path):
    path = tmp_path / "submissions.json"
    store = SubmissionStore(persist_path=path)
    _submit(store, "5AAA", "king")
    _submit(store, "5BBB", "king")
    _submit(store, "5DDD", "baseline-swap-solver")

    # Drop the sidecar and reconstruct: backfill must credit the earliest coiner
    # and must not coin boilerplate.
    path.with_name(path.name + ".names").unlink()
    store2 = SubmissionStore(persist_path=path)
    reg = json.loads(path.with_name(path.name + ".names").read_text())
    assert reg["king"]["owner_hotkey"] == "5AAA"
    assert "baseline-swap-solver" not in reg
    # And a fresh reuse by a third hotkey is flagged against the backfilled owner.
    c = _submit(store2, "5CCC", "king", rid="r9")
    assert c.is_copycat is True and c.coined_by_hotkey == "5AAA"
