"""Signed provenance helpers for solver submission artifacts.

The provenance record binds repo/commit/image metadata to a signature so
adoption policies can verify champion artifacts were produced by the local
screening pipeline and not tampered with in the submission store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

PROVENANCE_HMAC_ALG = "hmac-sha256"
PROVENANCE_EIP191_ALG = "eip191-secp256k1"
PROVENANCE_VERSION = 1


def parse_allowed_signers(raw: str) -> set[str]:
    """Parse comma-separated EVM addresses into a normalized set."""
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def validate_provenance_policy(
    *,
    require_signed: bool,
    require_asymmetric: bool,
    hmac_key: str,
    allowed_signers: set[str] | None,
    signing_private_key: str,
    signing_address: str,
    submissions_accepting: bool,
) -> tuple[bool, str]:
    """Validate signer/verifier policy consistency.

    Returns:
        (True, "") if valid; (False, reason) otherwise.
    """
    if require_asymmetric:
        require_signed = True

    allowed = {s.lower() for s in (allowed_signers or set()) if s}
    hmac_key = hmac_key.strip()
    signing_private_key = signing_private_key.strip()
    signing_address = signing_address.strip()

    if signing_address and not signing_private_key:
        return False, "SUBMISSION_PROVENANCE_SIGNING_ADDRESS set without signing private key"

    if signing_private_key and signing_address:
        try:
            derived = _derive_signer_address(signing_private_key)
        except Exception as exc:
            return False, f"failed to derive signer address: {exc}"
        if derived.lower() != signing_address.lower():
            return False, "configured signing private key does not match signing address"

    verifier_ready = bool(allowed) if require_asymmetric else bool(hmac_key or allowed)
    if require_signed and not verifier_ready:
        if require_asymmetric:
            return (
                False,
                "asymmetric provenance required but SUBMISSION_PROVENANCE_ALLOWED_SIGNERS is unset",
            )
        return (
            False,
            "signed provenance required but no verifier configured "
            "(set SUBMISSION_PROVENANCE_HMAC_KEY or SUBMISSION_PROVENANCE_ALLOWED_SIGNERS)",
        )

    if submissions_accepting and require_signed:
        signer_ready = bool(signing_private_key) if require_asymmetric else bool(
            signing_private_key or hmac_key
        )
        if not signer_ready:
            if require_asymmetric:
                return (
                    False,
                    "submissions are enabled with asymmetric provenance required "
                    "but SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY is unset",
                )
            return (
                False,
                "submissions are enabled with signed provenance required "
                "but no signing key is configured",
            )

    return True, ""


def validate_runtime_security_profile(
    *,
    enforce: bool,
    enable_source_submissions: bool,
    allow_subprocess_benchmark: bool,
    require_signed: bool,
    require_asymmetric: bool,
    hmac_key: str,
    allowed_signers: set[str] | None,
    submissions_accepting: bool,
    submissions_api_key: str,
    submissions_rate_limit_per_minute: int,
) -> tuple[bool, list[str]]:
    """Validate strict runtime security invariants for production profiles."""
    if not enforce:
        return True, []

    violations: list[str] = []
    allowed = {s.lower() for s in (allowed_signers or set()) if s}

    if enable_source_submissions:
        violations.append("ENABLE_SOURCE_SUBMISSIONS must be disabled")
    if allow_subprocess_benchmark:
        violations.append("ALLOW_SUBPROCESS_BENCHMARK must be disabled")
    if not require_asymmetric:
        violations.append("REQUIRE_ASYMMETRIC_PROVENANCE must be enabled")
    if not require_signed and not require_asymmetric:
        violations.append("REQUIRE_SIGNED_PROVENANCE must be enabled")
    if not allowed:
        violations.append("SUBMISSION_PROVENANCE_ALLOWED_SIGNERS must be configured")
    if require_asymmetric and hmac_key.strip():
        violations.append("SUBMISSION_PROVENANCE_HMAC_KEY must be unset in asymmetric-only mode")
    if submissions_accepting and not submissions_api_key.strip():
        violations.append("SUBMISSIONS_API_KEY must be set when submissions are accepting")
    if submissions_rate_limit_per_minute <= 0:
        violations.append("SUBMISSIONS_RATE_LIMIT_PER_MINUTE must be greater than zero")

    return len(violations) == 0, violations


def create_signed_provenance(
    *,
    submission_id: str,
    repo_url: str,
    commit_hash: str,
    image_id: str,
    image_tag: str | None,
    signing_key: str = "",
    signing_private_key: str = "",
    signer_address: str = "",
) -> dict[str, Any]:
    """Create a signed provenance record for a screened solver artifact.

    Signing strategy:
    - if ``signing_private_key`` is set -> EIP-191/secp256k1 signature
    - else if ``signing_key`` is set -> HMAC-SHA256 signature
    - else raises ValueError
    """
    payload: dict[str, Any] = {
        "version": PROVENANCE_VERSION,
        "submission_id": submission_id,
        "repo_url": repo_url,
        "commit_hash": commit_hash,
        "image_id": image_id,
        "image_tag": image_tag or "",
        "issued_at": int(time.time()),
    }
    canonical = _canonical_json(payload)

    if signing_private_key:
        signer = _derive_signer_address(signing_private_key)
        if signer_address and signer.lower() != signer_address.lower():
            raise ValueError("signer_address does not match signing_private_key")
        signature = _sign_eip191(canonical, signing_private_key)
        return {
            "alg": PROVENANCE_EIP191_ALG,
            "payload": payload,
            "signer": signer,
            "signature": signature,
        }

    if signing_key:
        signature = hmac.new(
            signing_key.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "alg": PROVENANCE_HMAC_ALG,
            "payload": payload,
            "signature": signature,
        }

    raise ValueError("No provenance signing material configured")


def verify_signed_provenance(
    record: dict[str, Any] | None,
    *,
    signing_key: str = "",
    allowed_signers: set[str] | None = None,
    allow_hmac: bool = True,
    expected_submission_id: str,
    expected_repo_url: str,
    expected_commit_hash: str,
    expected_image_id: str,
) -> tuple[bool, str]:
    """Verify a provenance record signature and bound identity fields."""
    if not isinstance(record, dict):
        return False, "missing provenance record"

    alg = str(record.get("alg", "")).strip().lower()

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False, "missing provenance payload"

    signature = str(record.get("signature", "")).strip().lower()
    if not signature:
        return False, "missing provenance signature"

    canonical = _canonical_json(payload)
    if alg == PROVENANCE_HMAC_ALG:
        if not allow_hmac:
            return False, "hmac provenance is disallowed by policy"
        if not signing_key:
            return False, "missing signing key for hmac provenance"
        expected_sig = hmac.new(
            signing_key.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return False, "signature mismatch"
    elif alg == PROVENANCE_EIP191_ALG:
        signer = str(record.get("signer", "")).strip().lower()
        if not signer:
            return False, "missing signer for eip191 provenance"
        recovered = _recover_eip191_signer(canonical, signature)
        if recovered.lower() != signer:
            return False, "signature mismatch"
        if allowed_signers:
            normalized = {s.lower() for s in allowed_signers if s}
            if signer not in normalized:
                return False, f"unauthorized signer: {signer}"
    else:
        return False, f"unsupported provenance alg: {alg or 'missing'}"

    checks = [
        ("submission_id", expected_submission_id),
        ("repo_url", expected_repo_url),
        ("commit_hash", expected_commit_hash),
        ("image_id", expected_image_id),
    ]
    for key, expected in checks:
        actual = str(payload.get(key, ""))
        if actual != expected:
            return False, f"payload mismatch for {key}"

    version = payload.get("version")
    if version != PROVENANCE_VERSION:
        return False, f"unsupported provenance version: {version}"

    return True, ""


def _canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON string for signing/verification."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign_eip191(message: str, private_key: str) -> str:
    """Sign message using EIP-191 personal_sign semantics."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception as exc:
        raise RuntimeError("eth_account is required for eip191 provenance signing") from exc
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    return signed.signature.hex()


def _recover_eip191_signer(message: str, signature_hex: str) -> str:
    """Recover signer address from EIP-191 signature."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception as exc:
        raise RuntimeError("eth_account is required for eip191 provenance verification") from exc
    return Account.recover_message(encode_defunct(text=message), signature=signature_hex)


def _derive_signer_address(private_key: str) -> str:
    """Derive EVM address for a secp256k1 private key."""
    try:
        from eth_account import Account
    except Exception as exc:
        raise RuntimeError("eth_account is required for eip191 provenance signing") from exc
    return Account.from_key(private_key).address
