"""Tests for ``verify_hotkey_signature`` / ``build_submission_message`` — the
crypto half of the signed-miner submission gate.

The path under test lives in
``minotaur_subnet.api.routes.submissions.routes``. Existing suites
(``test_submissions.py``, ``test_signed_miner_gate.py``) only ever *mock*
``verify_hotkey_signature`` (``return_value=True/False``) or test a different
gate (``_require_admin_or_signed_miner`` in ``routes/apps.py``). None of them
exercise the REAL signature roundtrip, the canonical message format
``"{pr_number}:{head_sha}:{round_id}"``, or the base64/exception handling.

These tests produce a genuine sr25519 signature with a real bittensor
``Keypair`` (no network — pure local crypto) and assert that:
  * a valid signature over the canonical message verifies True,
  * a tampered message / wrong key / wrong round_id / malformed signature
    return False (never raise),
  * ``build_submission_message`` emits the exact wire format and rejects an
    empty ``round_id``.

If bittensor's ``Keypair`` is unavailable in the venv we fall back to mocking
it and assert the code's branching + canonical-message construction instead.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions import routes as sub_routes

# Real local crypto if available; otherwise the mocked-branching tests still run.
try:
    from bittensor import Keypair as _RealKeypair

    _HAVE_KEYPAIR = True
except Exception:  # pragma: no cover - depends on venv
    _RealKeypair = None
    _HAVE_KEYPAIR = False

_real_keypair = pytest.mark.skipif(
    not _HAVE_KEYPAIR, reason="bittensor Keypair unavailable in venv"
)

# Canonical-message fixture inputs.
_PR = 7
_SHA = "a" * 40
_ROUND = "round-2026"


# ── build_submission_message: the canonical wire format ──────────────────


def test_build_message_exact_format():
    """Message is exactly ``{pr_number}:{head_sha}:{round_id}`` (no extras)."""
    msg = sub_routes.build_submission_message(_PR, _SHA, round_id=_ROUND)
    assert msg == f"{_PR}:{_SHA}:{_ROUND}"


def test_build_message_requires_round_id():
    """An empty round_id is rejected — it's a required consensus anchor."""
    with pytest.raises(ValueError):
        sub_routes.build_submission_message(_PR, _SHA, round_id="")


# ── verify_hotkey_signature: REAL sr25519 roundtrip ──────────────────────


def _sign(keypair, pr: int = _PR, sha: str = _SHA, round_id: str = _ROUND) -> str:
    """Sign the canonical message and return base64, the way a miner client does."""
    message = sub_routes.build_submission_message(pr, sha, round_id=round_id)
    sig = keypair.sign(message.encode("utf-8"))
    return base64.b64encode(sig).decode("ascii")


@_real_keypair
def test_valid_signature_verifies_true():
    """A signature produced by the hotkey over the canonical message → True."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp)
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, _SHA, sig_b64, _ROUND
    ) is True


@_real_keypair
def test_tampered_pr_number_fails():
    """Same signature, but verifier is told a different pr_number → False
    (the canonical message no longer matches what was signed)."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp, pr=_PR)
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR + 1, _SHA, sig_b64, _ROUND
    ) is False


@_real_keypair
def test_tampered_head_sha_fails():
    """A different head_sha than was signed → False."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp, sha=_SHA)
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, "b" * 40, sig_b64, _ROUND
    ) is False


@_real_keypair
def test_wrong_round_id_fails():
    """A signature for one round must not verify against another round."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp, round_id=_ROUND)
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, _SHA, sig_b64, "round-OTHER"
    ) is False


@_real_keypair
def test_wrong_hotkey_fails():
    """Signature made by Alice, but verified against Bob's ss58 → False."""
    alice = _RealKeypair.create_from_uri("//Alice")
    bob = _RealKeypair.create_from_uri("//Bob")
    sig_b64 = _sign(alice)
    assert sub_routes.verify_hotkey_signature(
        bob.ss58_address, _PR, _SHA, sig_b64, _ROUND
    ) is False


@_real_keypair
def test_malformed_base64_signature_returns_false_not_raise():
    """A non-base64 / garbage signature must be swallowed → False (no 500)."""
    kp = _RealKeypair.create_from_uri("//Alice")
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, _SHA, "!!!not-base64!!!", _ROUND
    ) is False


@_real_keypair
def test_wrong_length_signature_returns_false_not_raise():
    """Valid base64 but wrong byte length → swallowed → False."""
    short_sig_b64 = base64.b64encode(b"\x00\x01\x02").decode("ascii")
    kp = _RealKeypair.create_from_uri("//Alice")
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, _SHA, short_sig_b64, _ROUND
    ) is False


@_real_keypair
def test_invalid_ss58_address_returns_false_not_raise():
    """A bogus ss58 hotkey (Keypair construction raises) → False, not 500."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp)
    assert sub_routes.verify_hotkey_signature(
        "not-a-valid-ss58", _PR, _SHA, sig_b64, _ROUND
    ) is False


@_real_keypair
def test_empty_round_id_returns_false_not_raise():
    """build_submission_message raises ValueError on empty round_id; the broad
    try/except in verify_hotkey_signature must convert that to False, not 500."""
    kp = _RealKeypair.create_from_uri("//Alice")
    sig_b64 = _sign(kp)
    assert sub_routes.verify_hotkey_signature(
        kp.ss58_address, _PR, _SHA, sig_b64, ""
    ) is False


# ── mocked-Keypair fallback: assert canonical-message construction ───────
# These run regardless of whether real bittensor crypto is importable; they
# pin the EXACT bytes passed to Keypair.verify and the ss58 used to build it.


def test_verify_passes_canonical_message_and_ss58_to_keypair():
    """The verifier must build Keypair(ss58_address=hotkey) and call
    .verify(<canonical-message-bytes>, <decoded-signature>)."""
    raw_sig = b"\x11" * 64
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    mock_kp = MagicMock()
    mock_kp.verify.return_value = True
    mock_keypair_cls = MagicMock(return_value=mock_kp)

    with patch.object(sub_routes, "Keypair", mock_keypair_cls, create=True):
        # Import inside verify_hotkey_signature is `from bittensor import Keypair`,
        # so patch the symbol where it is looked up.
        with patch.dict(
            "sys.modules",
            {"bittensor": MagicMock(Keypair=mock_keypair_cls)},
        ):
            result = sub_routes.verify_hotkey_signature(
                "5HotKey", _PR, _SHA, sig_b64, _ROUND
            )

    assert result is True
    mock_keypair_cls.assert_called_once_with(ss58_address="5HotKey")
    expected_msg = f"{_PR}:{_SHA}:{_ROUND}".encode("utf-8")
    mock_kp.verify.assert_called_once_with(expected_msg, raw_sig)


def test_verify_returns_false_when_keypair_verify_false():
    """Keypair.verify() False propagates as False (no inversion bug)."""
    sig_b64 = base64.b64encode(b"\x22" * 64).decode("ascii")
    mock_kp = MagicMock()
    mock_kp.verify.return_value = False
    mock_keypair_cls = MagicMock(return_value=mock_kp)

    with patch.dict(
        "sys.modules",
        {"bittensor": MagicMock(Keypair=mock_keypair_cls)},
    ):
        result = sub_routes.verify_hotkey_signature(
            "5HotKey", _PR, _SHA, sig_b64, _ROUND
        )
    assert result is False
