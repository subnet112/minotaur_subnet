"""Private-repo tokens survive an api restart — encrypted at rest.

The per-submission PAT used to live only in the store's in-memory side map, so
ANY api restart between a private submission and champion finalize voided the
dethrone fail-closed ("relayer-finalize: ... has no token", round-e29716440-n1,
2026-07-02). The store now mirrors the token into an encrypted sidecar file
(NaCl SecretBox, key derived from VALIDATOR_PRIVATE_KEY) next to the main
JSON: a fresh store instance — a restarted process or a sibling worker — can
still produce the token, while the plaintext never touches disk. With no
signing key in the environment the sidecar is disabled and the historical
in-memory-only behaviour (miner re-submits) is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur_subnet.harness.submission_store import SubmissionStore

VALIDATOR_KEY = "0x" + "11" * 32
TOKEN = "ghp_secret_pat_1234567890"


def _make_private_submission(store: SubmissionStore, token: str | None = TOKEN):
    return store.create(
        repo_url="https://github.com/miner/private-solver",
        commit_hash="abc1234",
        epoch=42,
        hotkey="5Gxyz",
        round_id="round-e42-n1",
        is_private=True,
        private_repo_full="miner/private-solver",
        repo_token=token,
    )


def test_token_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"

    store = SubmissionStore(persist_path=p)
    sub = _make_private_submission(store)
    assert store.get_repo_token(sub.submission_id) == TOKEN

    # Simulated restart: a brand-new store instance recovers the token.
    reloaded = SubmissionStore(persist_path=p)
    assert reloaded.get_repo_token(sub.submission_id) == TOKEN


def test_plaintext_never_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"

    store = SubmissionStore(persist_path=p)
    _make_private_submission(store)

    for f in tmp_path.iterdir():
        if f.is_file():
            assert TOKEN not in f.read_text(errors="ignore"), f.name

    sidecar = tmp_path / "submissions.json.tokens"
    assert sidecar.exists()
    # Sidecar is ciphertext (base64 blobs), valid JSON, and 0600.
    assert json.loads(sidecar.read_text())
    assert (sidecar.stat().st_mode & 0o777) == 0o600


def test_cross_process_visibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A sibling worker (separate instance, same files) sees the token."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"

    writer = SubmissionStore(persist_path=p)
    reader = SubmissionStore(persist_path=p)  # opened BEFORE the submission
    sub = _make_private_submission(writer)

    reader._maybe_reload()  # what _write_guard does on the next mutation
    assert reader.get_repo_token(sub.submission_id) == TOKEN


def test_purge_removes_from_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"

    store = SubmissionStore(persist_path=p)
    sub = _make_private_submission(store)
    store.purge_token(sub.submission_id)

    assert store.get_repo_token(sub.submission_id) is None
    assert json.loads((tmp_path / "submissions.json.tokens").read_text()) == {}
    # And a restart cannot resurrect it.
    assert SubmissionStore(persist_path=p).get_repo_token(sub.submission_id) is None


def test_terminal_states_purge_the_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """reject() and adopt() both end the credential's life on disk."""
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"
    sidecar = tmp_path / "submissions.json.tokens"

    store = SubmissionStore(persist_path=p)
    rejected = _make_private_submission(store)
    store.reject(rejected.submission_id, "screening failed")
    assert rejected.submission_id not in json.loads(sidecar.read_text())

    adopted = store.create(
        repo_url="https://github.com/miner/private-solver",
        commit_hash="def5678",
        epoch=43,
        hotkey="5Gother",
        round_id="round-e43-n1",
        is_private=True,
        private_repo_full="miner/private-solver",
        repo_token=TOKEN,
    )
    store.adopt(adopted.submission_id)
    assert json.loads(sidecar.read_text()) == {}


def test_no_signing_key_keeps_in_memory_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("VALIDATOR_PRIVATE_KEY", raising=False)
    p = tmp_path / "submissions.json"

    store = SubmissionStore(persist_path=p)
    sub = _make_private_submission(store)

    assert store.get_repo_token(sub.submission_id) == TOKEN  # in-process OK
    assert not (tmp_path / "submissions.json.tokens").exists()
    # Restart loses it — the historical fail-closed behaviour.
    assert SubmissionStore(persist_path=p).get_repo_token(sub.submission_id) is None


def test_kill_switch_disables_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    monkeypatch.setenv("SUBMISSION_TOKEN_PERSIST", "0")
    p = tmp_path / "submissions.json"

    store = SubmissionStore(persist_path=p)
    sub = _make_private_submission(store)

    assert store.get_repo_token(sub.submission_id) == TOKEN
    assert not (tmp_path / "submissions.json.tokens").exists()


def test_rotated_key_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", VALIDATOR_KEY)
    p = tmp_path / "submissions.json"
    sub = _make_private_submission(SubmissionStore(persist_path=p))

    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0x" + "22" * 32)
    rotated = SubmissionStore(persist_path=p)
    assert rotated.get_repo_token(sub.submission_id) is None
