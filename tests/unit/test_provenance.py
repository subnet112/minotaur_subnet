"""Unit tests for signed provenance helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.harness.provenance import (
    create_signed_provenance,
    parse_allowed_signers,
    validate_provenance_policy,
    validate_runtime_security_profile,
    verify_signed_provenance,
)


def test_verify_signed_provenance_ok():
    key = "test-key"
    record = create_signed_provenance(
        submission_id="sub_abc",
        repo_url="https://github.com/miner/solver",
        commit_hash="abcdef1",
        image_id="sha256:" + "a" * 64,
        image_tag="solver-abcdef:screening",
        signing_key=key,
    )
    ok, reason = verify_signed_provenance(
        record,
        signing_key=key,
        expected_submission_id="sub_abc",
        expected_repo_url="https://github.com/miner/solver",
        expected_commit_hash="abcdef1",
        expected_image_id="sha256:" + "a" * 64,
    )
    assert ok is True
    assert reason == ""


def test_verify_signed_provenance_rejects_tamper():
    key = "test-key"
    record = create_signed_provenance(
        submission_id="sub_abc",
        repo_url="https://github.com/miner/solver",
        commit_hash="abcdef1",
        image_id="sha256:" + "a" * 64,
        image_tag="solver-abcdef:screening",
        signing_key=key,
    )
    record["payload"]["image_id"] = "sha256:" + "b" * 64
    ok, reason = verify_signed_provenance(
        record,
        signing_key=key,
        expected_submission_id="sub_abc",
        expected_repo_url="https://github.com/miner/solver",
        expected_commit_hash="abcdef1",
        expected_image_id="sha256:" + "a" * 64,
    )
    assert ok is False
    assert reason != ""


def test_verify_signed_provenance_rejects_hmac_when_disallowed():
    key = "test-key"
    record = create_signed_provenance(
        submission_id="sub_abc",
        repo_url="https://github.com/miner/solver",
        commit_hash="abcdef1",
        image_id="sha256:" + "a" * 64,
        image_tag="solver-abcdef:screening",
        signing_key=key,
    )
    ok, reason = verify_signed_provenance(
        record,
        signing_key=key,
        allow_hmac=False,
        expected_submission_id="sub_abc",
        expected_repo_url="https://github.com/miner/solver",
        expected_commit_hash="abcdef1",
        expected_image_id="sha256:" + "a" * 64,
    )
    assert ok is False
    assert "disallowed" in reason


def test_verify_signed_provenance_eip191_ok_with_allowed_signer():
    with patch(
        "minotaur_subnet.harness.provenance._derive_signer_address",
        return_value="0xabc",
    ), patch(
        "minotaur_subnet.harness.provenance._sign_eip191",
        return_value="0xsig",
    ), patch(
        "minotaur_subnet.harness.provenance._recover_eip191_signer",
        return_value="0xAbC",
    ):
        record = create_signed_provenance(
            submission_id="sub_abc",
            repo_url="https://github.com/miner/solver",
            commit_hash="abcdef1",
            image_id="sha256:" + "a" * 64,
            image_tag="solver-abcdef:screening",
            signing_private_key="0x123",
        )
        ok, reason = verify_signed_provenance(
            record,
            allowed_signers={"0xabc"},
            expected_submission_id="sub_abc",
            expected_repo_url="https://github.com/miner/solver",
            expected_commit_hash="abcdef1",
            expected_image_id="sha256:" + "a" * 64,
        )
        assert ok is True
        assert reason == ""


def test_verify_signed_provenance_eip191_rejects_unauthorized_signer():
    with patch(
        "minotaur_subnet.harness.provenance._derive_signer_address",
        return_value="0xabc",
    ), patch(
        "minotaur_subnet.harness.provenance._sign_eip191",
        return_value="0xsig",
    ), patch(
        "minotaur_subnet.harness.provenance._recover_eip191_signer",
        return_value="0xabc",
    ):
        record = create_signed_provenance(
            submission_id="sub_abc",
            repo_url="https://github.com/miner/solver",
            commit_hash="abcdef1",
            image_id="sha256:" + "a" * 64,
            image_tag="solver-abcdef:screening",
            signing_private_key="0x123",
        )
        ok, reason = verify_signed_provenance(
            record,
            allowed_signers={"0xdef"},
            expected_submission_id="sub_abc",
            expected_repo_url="https://github.com/miner/solver",
            expected_commit_hash="abcdef1",
            expected_image_id="sha256:" + "a" * 64,
        )
        assert ok is False
        assert "unauthorized signer" in reason


def test_validate_policy_rejects_asymmetric_without_allowed_signers():
    ok, reason = validate_provenance_policy(
        require_signed=True,
        require_asymmetric=True,
        hmac_key="",
        allowed_signers=set(),
        signing_private_key="0x123",
        signing_address="",
        submissions_accepting=True,
    )
    assert ok is False
    assert "allowed_signers" in reason.lower()


def test_validate_policy_rejects_submissions_without_signer_material():
    ok, reason = validate_provenance_policy(
        require_signed=True,
        require_asymmetric=False,
        hmac_key="",
        allowed_signers={"0xabc"},
        signing_private_key="",
        signing_address="",
        submissions_accepting=True,
    )
    assert ok is False
    assert "no signing key" in reason.lower()


def test_validate_policy_accepts_verify_only_when_submissions_disabled():
    ok, reason = validate_provenance_policy(
        require_signed=True,
        require_asymmetric=True,
        hmac_key="",
        allowed_signers={"0xabc"},
        signing_private_key="",
        signing_address="",
        submissions_accepting=False,
    )
    assert ok is True
    assert reason == ""


def test_parse_allowed_signers_normalizes_addresses():
    signers = parse_allowed_signers("0xAbC, 0xDef ,,")
    assert signers == {"0xabc", "0xdef"}


def test_validate_runtime_profile_strict_rejects_insecure_flags():
    ok, violations = validate_runtime_security_profile(
        enforce=True,
        allow_subprocess_benchmark=True,
        require_signed=False,
        require_asymmetric=False,
        hmac_key="hmac-key",
        allowed_signers=set(),
        submissions_accepting=True,
        submissions_api_key="",
        submissions_rate_limit_per_minute=0,
    )
    assert ok is False
    assert len(violations) >= 3


def test_validate_runtime_profile_strict_accepts_safe_config():
    ok, violations = validate_runtime_security_profile(
        enforce=True,
        allow_subprocess_benchmark=False,
        require_signed=True,
        require_asymmetric=True,
        hmac_key="",
        allowed_signers={"0xabc"},
        submissions_accepting=True,
        submissions_api_key="api-key",
        submissions_rate_limit_per_minute=60,
    )
    assert ok is True
    assert violations == []
