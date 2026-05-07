"""Shared champion-eligibility policy helpers.

The benchmark worker may score any submission, but only image-backed,
policy-compliant artifacts are eligible to become the live champion.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from minotaur_subnet.harness.provenance import (
    parse_allowed_signers,
    verify_signed_provenance,
)
from minotaur_subnet.weight_policy import GENESIS_HOTKEY

logger = logging.getLogger(__name__)


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_submission_champion_eligible(submission: Any) -> tuple[bool, str | None]:
    """Return whether a scored submission is eligible for champion activation."""
    if getattr(submission, "hotkey", None) == GENESIS_HOTKEY:
        return True, None

    # Reject inline-source / subprocess submissions. Two independent signals
    # catch this: a non-None solver_path (the local file the source harness
    # ran from) and a repo_url starting with ``source://`` (the marker the
    # /v1/submissions/source endpoint uses). A real Docker-built submission
    # has neither.
    if getattr(submission, "solver_path", None):
        return False, "source/subprocess submission is not champion-eligible by policy"
    repo_url = getattr(submission, "repo_url", "") or ""
    if repo_url.startswith("source://"):
        return False, "source/subprocess submission is not champion-eligible by policy"

    image_tag = getattr(submission, "image_tag", None)
    if not image_tag:
        return False, "missing image_tag"

    image_id = getattr(submission, "image_id", None)
    if not image_id:
        return False, "missing immutable image_id"
    if not str(image_id).startswith("sha256:"):
        return False, f"invalid image_id format: {image_id}"

    # SECURITY: Provenance signing ensures that champion images can be traced
    # back to a specific miner commit with cryptographic proof. Without this,
    # an attacker could inject a malicious image that becomes the live champion
    # without any verifiable link to a known miner identity or source code.
    # Both defaults are True to enforce provenance by default.
    require_provenance = _env_true("REQUIRE_SIGNED_PROVENANCE", default=True)
    require_asymmetric = _env_true("REQUIRE_ASYMMETRIC_PROVENANCE", default=True)
    # Warn operators who explicitly disable provenance checks
    if not require_provenance:
        logger.warning(
            "SECURITY: REQUIRE_SIGNED_PROVENANCE is disabled. "
            "Submissions can become champion without cryptographic proof of origin. "
            "This is unsafe for production — enable provenance signing."
        )
    if not require_asymmetric:
        logger.warning(
            "SECURITY: REQUIRE_ASYMMETRIC_PROVENANCE is disabled. "
            "HMAC-based provenance is weaker than asymmetric signing because "
            "the shared secret can be leaked. Enable asymmetric provenance for production."
        )
    if require_asymmetric:
        require_provenance = True

    key = os.environ.get("SUBMISSION_PROVENANCE_HMAC_KEY", "").strip()
    allowed_signers = parse_allowed_signers(
        os.environ.get("SUBMISSION_PROVENANCE_ALLOWED_SIGNERS", "").strip(),
    )
    verifier_ready = bool(allowed_signers) if require_asymmetric else bool(key or allowed_signers)
    if require_provenance and not verifier_ready:
        if require_asymmetric:
            return (
                False,
                "signed provenance required in asymmetric-only mode but "
                "SUBMISSION_PROVENANCE_ALLOWED_SIGNERS is unset",
            )
        return (
            False,
            "signed provenance required but no verifier configured "
            "(set SUBMISSION_PROVENANCE_HMAC_KEY or SUBMISSION_PROVENANCE_ALLOWED_SIGNERS)",
        )

    if verifier_ready:
        ok, reason = verify_signed_provenance(
            getattr(submission, "provenance", None),
            signing_key=key,
            allowed_signers=allowed_signers,
            allow_hmac=not require_asymmetric,
            expected_submission_id=getattr(submission, "submission_id", ""),
            expected_repo_url=getattr(submission, "repo_url", ""),
            expected_commit_hash=getattr(submission, "commit_hash", ""),
            expected_image_id=image_id,
        )
        if not ok:
            return False, f"invalid signed provenance: {reason}"

    return True, None
