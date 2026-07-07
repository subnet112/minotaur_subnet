"""Normalized content fingerprint — "same code, same quota" vs nonce rotation.

Pins the identity properties the cross-hotkey resubmit quota depends on:
cosmetic edits (comments / whitespace / docstrings / .git noise) do NOT mint a
new identity; semantic edits (logic, string constants — replay calldata lives
in strings — data files, file moves) DO.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.code_fingerprint import (
    repo_fingerprint,
    source_fingerprint,
)
from minotaur_subnet.harness.submission_store import (
    SubmissionStatus,
    SubmissionStore,
)

BASE = '''
"""Module docstring."""
import os

TABLE = {"0xabc": "0xdeadbeef"}

def solve(x):
    """Doc."""
    a = x + 1
    return a * 2
'''


def _repo(tmp_path, name, solver_src, data='{"k": "v"}'):
    d = tmp_path / name
    d.mkdir()
    (d / "solver.py").write_text(solver_src)
    (d / "replay.json").write_text(data)
    (d / ".git").mkdir()
    (d / ".git" / "pack").write_text(name)  # clone-unique transport noise
    return str(d)


# ── source-level identity ─────────────────────────────────────────────────


def test_nonce_comment_does_not_rotate_identity():
    assert source_fingerprint(BASE) == source_fingerprint(
        BASE + "\n# putty-nonce 0.87.5-edge 1783394779453439103-1117347\n"
    )


def test_whitespace_and_formatting_do_not_rotate():
    reformatted = BASE.replace("a = x + 1", "a   =   x + 1").replace("\n\n", "\n\n\n")
    assert source_fingerprint(BASE) == source_fingerprint(reformatted)


def test_docstring_edit_does_not_rotate():
    assert source_fingerprint(BASE) == source_fingerprint(
        BASE.replace('"""Module docstring."""', '"""Totally new docs v99."""')
        .replace('"""Doc."""', '"""Other."""')
    )


def test_string_constant_change_rotates():
    # Semantic: replay calldata lives in string constants.
    assert source_fingerprint(BASE) != source_fingerprint(
        BASE.replace("0xdeadbeef", "0xdeadbee0")
    )


def test_logic_change_rotates():
    assert source_fingerprint(BASE) != source_fingerprint(
        BASE.replace("a * 2", "a * 3")
    )


def test_unparseable_source_falls_back_to_bytes():
    bad = "def broken(:\n    pass\n"
    assert source_fingerprint(bad) == source_fingerprint(bad)
    assert source_fingerprint(bad) != source_fingerprint(bad + "# x\n")  # raw mode: bytes count


# ── tree-level identity ───────────────────────────────────────────────────


def test_git_noise_is_excluded(tmp_path):
    a = _repo(tmp_path, "a", BASE)
    b = _repo(tmp_path, "b", BASE)  # different .git pack contents
    assert repo_fingerprint(a) == repo_fingerprint(b)


def test_data_file_change_rotates(tmp_path):
    a = _repo(tmp_path, "a", BASE, data='{"k": "v1"}')
    b = _repo(tmp_path, "b", BASE, data='{"k": "v2"}')
    assert repo_fingerprint(a) != repo_fingerprint(b)


def test_file_rename_rotates(tmp_path):
    a = _repo(tmp_path, "a", BASE)
    b = _repo(tmp_path, "b", BASE)
    (Path(b) / "solver.py").rename(Path(b) / "solver2.py")
    assert repo_fingerprint(a) != repo_fingerprint(b)


def test_comment_edit_in_tree_does_not_rotate(tmp_path):
    a = _repo(tmp_path, "a", BASE)
    b = _repo(tmp_path, "b", BASE + "\n# nonce 12345\n")
    assert repo_fingerprint(a) == repo_fingerprint(b)


# ── store: cross-hotkey benched-round accounting ──────────────────────────


def _seed(store, sid, hotkey, round_id, status, fp):
    sub = store.create(
        repo_url="https://example.com/r.git", commit_hash=f"c_{sid}",
        epoch=1, hotkey=hotkey, round_id=round_id,
    )
    store.set_content_fingerprint(sub.submission_id, fp)
    store.update_status(sub.submission_id, status)
    return sub.submission_id


def test_count_benched_rounds_is_cross_hotkey(tmp_path):
    store = SubmissionStore(persist_path=tmp_path / "subs.json")
    fp = "f" * 64
    _seed(store, 1, "hk_A", "r1", SubmissionStatus.SCORED, fp)
    _seed(store, 2, "hk_B", "r2", SubmissionStatus.ADOPTED, fp)       # other hotkey COUNTS
    _seed(store, 3, "hk_C", "r2", SubmissionStatus.SCORED, fp)        # same round: distinct-round count
    _seed(store, 4, "hk_D", "r3", SubmissionStatus.REJECTED, fp)      # not benched: free
    _seed(store, 5, "hk_E", "r4", SubmissionStatus.SCORED, "0" * 64)  # other code: free
    assert store.count_benched_rounds_for_fingerprint(fp) == 2  # r1, r2

    excl = _seed(store, 6, "hk_F", "r5", SubmissionStatus.SCORED, fp)
    assert store.count_benched_rounds_for_fingerprint(fp) == 3
    assert store.count_benched_rounds_for_fingerprint(fp, exclude_submission_id=excl) == 2


def test_fingerprint_survives_store_reload(tmp_path):
    path = tmp_path / "subs.json"
    store = SubmissionStore(persist_path=path)
    sid = _seed(store, 1, "hk_A", "r1", SubmissionStatus.SCORED, "a" * 64)
    reloaded = SubmissionStore(persist_path=path)
    assert reloaded.get(sid).content_fingerprint == "a" * 64
