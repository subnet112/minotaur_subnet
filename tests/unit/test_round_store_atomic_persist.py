"""RoundStore persists round/champion state ATOMICALLY.

``_persist`` used to ``write_text`` directly over the target file, so a crash
(or a concurrent ``_load`` reader) mid-write could observe a truncated /
half-written ``solver_rounds.json`` — losing or corrupting the leader's round
and champion state on restart. The fix writes a temp file in the same
directory, fsyncs it, and ``os.replace``s over the target (atomic on POSIX): a
reader always sees either the whole old file or the whole new one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from minotaur_subnet.harness import round_store as rs_mod
from minotaur_subnet.harness.round_store import RoundStatus, RoundStore


def test_persist_round_trips_and_leaves_no_temp_file(tmp_path: Path):
    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)
    store.ensure_open_round(opened_epoch=7)

    # The target exists and is COMPLETE, valid JSON.
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["current_round_id"] == "round-e7-n1"

    # No partial temp file lingers after a successful persist.
    assert list(tmp_path.glob(".solver_rounds.json.tmp")) == []
    assert list(tmp_path.glob("*.tmp")) == []

    # A fresh store reloads the same state from disk.
    reloaded = RoundStore(persist_path=p)
    current = reloaded.get_current_round()
    assert current is not None
    assert current.round_id == "round-e7-n1"
    assert current.status == RoundStatus.OPEN


def test_failed_replace_leaves_prior_file_intact(tmp_path: Path):
    """If the rename fails mid-persist, the previously-persisted file must remain
    whole and readable — never truncated to empty/partial."""
    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)
    store.ensure_open_round(opened_epoch=3)  # first good persist
    good_bytes = p.read_bytes()
    assert json.loads(good_bytes)["current_round_id"] == "round-e3-n1"

    # Make the atomic swap blow up on the NEXT persist, then mutate.
    with patch.object(rs_mod.os, "replace", side_effect=OSError("disk full")):
        store.ensure_open_round(opened_epoch=3)  # triggers another _persist
        store.close_current_round(close_epoch=4)  # and another

    # The on-disk file is exactly the last fully-written version — untouched,
    # not a half-written ruin.
    assert p.read_bytes() == good_bytes
    assert json.loads(p.read_text())["current_round_id"] == "round-e3-n1"
    # And the temp file was cleaned up, not left behind.
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_write_cleans_up_temp(tmp_path: Path):
    """An error during the temp write must not leave a stray temp file. fsync is
    the write-phase op (the temp is created by mkstemp, then written + fsync'd);
    a failure there must still hit the finally that unlinks the temp."""
    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)

    with patch.object(rs_mod.os, "fsync", side_effect=OSError("no space")):
        store.ensure_open_round(opened_epoch=5)  # _persist fails on the temp fsync

    assert list(tmp_path.glob("*.tmp")) == []


def test_persist_preserves_file_mode(tmp_path: Path):
    """os.replace swaps the inode, so the temp's mode would otherwise win. mkstemp
    creates the temp 0600; the persist must copy the target's existing mode so an
    operator-set permission is not silently narrowed on every write."""
    import stat as _stat

    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)
    store.ensure_open_round(opened_epoch=7)        # first persist creates the file
    os.chmod(p, 0o640)                              # operator sets a restrictive mode
    store.close_current_round(close_epoch=8)        # next persist replaces via mkstemp

    assert _stat.S_IMODE(p.stat().st_mode) == 0o640  # preserved, not reset to 0600


def test_first_write_falls_back_to_0644(tmp_path: Path):
    """The FIRST persist has no target to copy a mode from; it must fall back to
    0644 (the umask-default the old write_text produced), NOT leave mkstemp's
    0600 — else a co-located reader (or a shared store-data mount) can't open it."""
    import stat as _stat

    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)
    store.ensure_open_round(opened_epoch=7)  # first-ever persist

    assert _stat.S_IMODE(p.stat().st_mode) == 0o644


def test_orphan_temp_swept_on_load(tmp_path: Path):
    """A crash between mkstemp and os.replace leaves a unique .tmp orphan. Because
    temp names are unique they'd accumulate; a fresh store must sweep them on load."""
    p = tmp_path / "solver_rounds.json"
    RoundStore(persist_path=p).ensure_open_round(opened_epoch=3)  # real file now exists

    orphan = tmp_path / ".solver_rounds.json.deadbeef.tmp"
    orphan.write_text("half-written-garbage")
    assert orphan.exists()

    reloaded = RoundStore(persist_path=p)  # __init__ -> _load -> _sweep_orphan_temps

    assert not orphan.exists()                       # orphan swept
    assert p.exists()                                # real store untouched
    assert reloaded.get_current_round() is not None  # and still loads
