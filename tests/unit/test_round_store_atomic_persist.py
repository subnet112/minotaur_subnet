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
    """An error during the temp write must not leave a stray temp file."""
    p = tmp_path / "solver_rounds.json"
    store = RoundStore(persist_path=p)

    real_open = open

    def _boom_open(file, *args, **kwargs):
        if str(file).endswith(".solver_rounds.json.tmp"):
            raise OSError("no space")
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=_boom_open):
        store.ensure_open_round(opened_epoch=5)  # _persist fails on the temp write

    assert list(tmp_path.glob("*.tmp")) == []
