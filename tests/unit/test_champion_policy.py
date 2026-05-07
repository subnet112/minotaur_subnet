"""Unit tests for shared champion eligibility policy."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.harness.champion_policy import is_submission_champion_eligible
from minotaur_subnet.harness.provenance import create_signed_provenance
from minotaur_subnet.harness.submission_store import SubmissionStore


def _make_submission(
    *,
    hotkey: str = "miner-key",
    solver_path: str | None = None,
    image_id: str | None = "sha256:" + "a" * 64,
):
    store = SubmissionStore()
    sub = store.create(
        repo_url="https://github.com/test/solver",
        commit_hash="abc1234",
        epoch=1,
        hotkey=hotkey,
    )
    if solver_path is not None:
        store.set_solver_path(sub.submission_id, solver_path)
    store.set_image_tag(sub.submission_id, "solver:test")
    if image_id is not None:
        store.set_image_id(sub.submission_id, image_id)
    return store.get(sub.submission_id)


def test_source_submission_not_champion_eligible():
    submission = _make_submission(solver_path="/tmp/solver.py")

    ok, reason = is_submission_champion_eligible(submission)

    assert ok is False
    assert "source/subprocess" in str(reason)


def test_inline_repo_url_not_champion_eligible():
    """Defense-in-depth: even if solver_path isn't set, a source:// repo_url
    marks the submission as inline and must be rejected."""
    store = SubmissionStore()
    sub = store.create(
        repo_url="source://inline",
        commit_hash="abc1234",
        epoch=1,
        hotkey="miner-key",
    )
    store.set_image_tag(sub.submission_id, "solver:test")
    store.set_image_id(sub.submission_id, "sha256:" + "c" * 64)
    submission = store.get(sub.submission_id)

    ok, reason = is_submission_champion_eligible(submission)
    assert ok is False
    assert "source/subprocess" in str(reason)


def test_missing_image_id_not_champion_eligible():
    submission = _make_submission(image_id=None)

    ok, reason = is_submission_champion_eligible(submission)

    assert ok is False
    assert "missing immutable image_id" in str(reason)


def test_image_submission_is_eligible_without_provenance_requirement():
    submission = _make_submission()

    with patch.dict(
        "os.environ",
        {"REQUIRE_SIGNED_PROVENANCE": "0", "REQUIRE_ASYMMETRIC_PROVENANCE": "0"},
        clear=False,
    ):
        ok, reason = is_submission_champion_eligible(submission)

    assert ok is True
    assert reason is None


def test_requires_signed_provenance_when_policy_enabled():
    submission = _make_submission()

    with patch.dict(
        "os.environ",
        {
            "REQUIRE_SIGNED_PROVENANCE": "1",
            "REQUIRE_ASYMMETRIC_PROVENANCE": "0",
            "SUBMISSION_PROVENANCE_HMAC_KEY": "unit-test-key",
        },
        clear=False,
    ):
        ok, reason = is_submission_champion_eligible(submission)

    assert ok is False
    assert "invalid signed provenance" in str(reason)


def test_valid_hmac_provenance_allows_champion_eligibility():
    submission = _make_submission()
    key = "unit-test-key"
    submission.provenance = create_signed_provenance(
        submission_id=submission.submission_id,
        repo_url=submission.repo_url,
        commit_hash=submission.commit_hash,
        image_id=submission.image_id or "",
        image_tag=submission.image_tag,
        signing_key=key,
    )

    with patch.dict(
        "os.environ",
        {
            "REQUIRE_SIGNED_PROVENANCE": "1",
            "REQUIRE_ASYMMETRIC_PROVENANCE": "0",
            "SUBMISSION_PROVENANCE_HMAC_KEY": key,
        },
        clear=False,
    ):
        ok, reason = is_submission_champion_eligible(submission)

    assert ok is True
    assert reason is None


def test_asymmetric_only_rejects_hmac_provenance():
    submission = _make_submission()
    key = "unit-test-key"
    submission.provenance = create_signed_provenance(
        submission_id=submission.submission_id,
        repo_url=submission.repo_url,
        commit_hash=submission.commit_hash,
        image_id=submission.image_id or "",
        image_tag=submission.image_tag,
        signing_key=key,
    )

    with patch.dict(
        "os.environ",
        {
            "REQUIRE_SIGNED_PROVENANCE": "1",
            "REQUIRE_ASYMMETRIC_PROVENANCE": "1",
            "SUBMISSION_PROVENANCE_ALLOWED_SIGNERS": "0xabc",
            "SUBMISSION_PROVENANCE_HMAC_KEY": key,
        },
        clear=False,
    ):
        ok, reason = is_submission_champion_eligible(submission)

    assert ok is False
    assert "invalid signed provenance" in str(reason)


def test_valid_asymmetric_provenance_is_eligible():
    from eth_account import Account

    submission = _make_submission()
    signing_key = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    signer_address = Account.from_key(signing_key).address
    submission.provenance = create_signed_provenance(
        submission_id=submission.submission_id,
        repo_url=submission.repo_url,
        commit_hash=submission.commit_hash,
        image_id=submission.image_id or "",
        image_tag=submission.image_tag,
        signing_private_key=signing_key,
    )

    with patch.dict(
        "os.environ",
        {
            "REQUIRE_SIGNED_PROVENANCE": "1",
            "REQUIRE_ASYMMETRIC_PROVENANCE": "1",
            "SUBMISSION_PROVENANCE_ALLOWED_SIGNERS": signer_address,
            "SUBMISSION_PROVENANCE_HMAC_KEY": "",
        },
        clear=False,
    ):
        ok, reason = is_submission_champion_eligible(submission)

    assert ok is True
    assert reason is None
