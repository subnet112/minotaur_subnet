"""Tests for the M3+M4 order-ownership signature gate.

Three order-modifying endpoints now require a signed proof of ownership:
- DELETE /orders/{id}       (M3)
- PATCH /orders/{id}/tx-confirmed   (M4)
- PATCH /orders/{id}/signature      (M4)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.order_owner_sig import (
    ACTION_ATTACH_SIG,
    ACTION_CANCEL,
    ACTION_CONFIRM_TX,
    MAX_DEADLINE_FUTURE_SECONDS,
    content_hash_of,
    sign_order_action,
    verify_order_action,
)


# ── content_hash_of ────────────────────────────────────────────────────


def test_content_hash_empty_is_zeroes():
    assert content_hash_of("") == "0x" + ("0" * 64)


def test_content_hash_deterministic():
    h1 = content_hash_of("0xabc")
    h2 = content_hash_of("0xabc")
    assert h1 == h2 != content_hash_of("0xabcd")


# ── sign + verify round-trip ──────────────────────────────────────────


@pytest.fixture
def owner():
    """Fresh account whose key tests use to sign action payloads."""
    return Account.create()


def test_signed_action_verifies_for_correct_owner(owner):
    deadline = int(time.time()) + 300
    sig = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL,
        order_id="order-123",
        deadline=deadline,
        chain_id=8453,
    )
    ok, err = verify_order_action(
        expected_owner=owner.address,
        action=ACTION_CANCEL,
        order_id="order-123",
        content_hash="",
        deadline=deadline,
        chain_id=8453,
        signature_hex=sig,
    )
    assert ok, err


def test_signed_action_rejects_wrong_owner(owner):
    """Sig from one account, expected_owner is a different account."""
    deadline = int(time.time()) + 300
    sig = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL,
        order_id="order-123",
        deadline=deadline,
        chain_id=8453,
    )
    other = Account.create()
    ok, err = verify_order_action(
        expected_owner=other.address,
        action=ACTION_CANCEL,
        order_id="order-123",
        content_hash="",
        deadline=deadline,
        chain_id=8453,
        signature_hex=sig,
    )
    assert not ok
    assert "does not match" in err


def test_signed_action_rejects_wrong_order(owner):
    """A sig that covers order A can't cancel order B (replay protection)."""
    deadline = int(time.time()) + 300
    sig_for_a = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="order-A",
        deadline=deadline, chain_id=8453,
    )
    ok, _ = verify_order_action(
        expected_owner=owner.address, action=ACTION_CANCEL, order_id="order-B",
        content_hash="", deadline=deadline, chain_id=8453,
        signature_hex=sig_for_a,
    )
    assert not ok


def test_signed_action_rejects_wrong_action(owner):
    """A Cancel sig can't be used for ConfirmTx (action separator)."""
    deadline = int(time.time()) + 300
    sig_cancel = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="order-X",
        deadline=deadline, chain_id=8453,
    )
    ok, _ = verify_order_action(
        expected_owner=owner.address, action=ACTION_CONFIRM_TX, order_id="order-X",
        content_hash="", deadline=deadline, chain_id=8453,
        signature_hex=sig_cancel,
    )
    assert not ok


def test_signed_action_rejects_wrong_chain(owner):
    """Chain separator: same order_id on Base can't be replayed to BT EVM."""
    deadline = int(time.time()) + 300
    sig_base = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="order-X",
        deadline=deadline, chain_id=8453,
    )
    ok, _ = verify_order_action(
        expected_owner=owner.address, action=ACTION_CANCEL, order_id="order-X",
        content_hash="", deadline=deadline, chain_id=964,
        signature_hex=sig_base,
    )
    assert not ok


def test_signed_action_rejects_expired_deadline(owner):
    """A sig with a deadline in the past is rejected."""
    deadline = int(time.time()) - 60
    sig = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="order-X",
        deadline=deadline, chain_id=8453,
    )
    ok, err = verify_order_action(
        expected_owner=owner.address, action=ACTION_CANCEL, order_id="order-X",
        content_hash="", deadline=deadline, chain_id=8453,
        signature_hex=sig,
    )
    assert not ok
    assert "deadline expired" in err


def test_signed_action_rejects_far_future_deadline(owner):
    """Deadlines further than MAX_DEADLINE_FUTURE_SECONDS ahead are rejected —
    bounds replay window even if the signed payload leaks."""
    deadline = int(time.time()) + MAX_DEADLINE_FUTURE_SECONDS + 60
    sig = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="order-X",
        deadline=deadline, chain_id=8453,
    )
    ok, err = verify_order_action(
        expected_owner=owner.address, action=ACTION_CANCEL, order_id="order-X",
        content_hash="", deadline=deadline, chain_id=8453,
        signature_hex=sig,
    )
    assert not ok
    assert "too far in the future" in err


def test_signed_action_with_content_hash_binds_content(owner):
    """An attach-sig sig made for user_signature A can't be replayed
    when attaching user_signature B."""
    deadline = int(time.time()) + 300
    sig_for_user_sig_a = sign_order_action(
        owner.key.hex(),
        action=ACTION_ATTACH_SIG, order_id="order-X",
        content_hash=content_hash_of("user_sig_A"),
        deadline=deadline, chain_id=8453,
    )
    # Try to verify it against a different user_signature
    ok, _ = verify_order_action(
        expected_owner=owner.address, action=ACTION_ATTACH_SIG, order_id="order-X",
        content_hash=content_hash_of("user_sig_B"),
        deadline=deadline, chain_id=8453,
        signature_hex=sig_for_user_sig_a,
    )
    assert not ok


def test_verify_rejects_empty_signature(owner):
    ok, err = verify_order_action(
        expected_owner=owner.address, action=ACTION_CANCEL,
        order_id="order-X", content_hash="", deadline=int(time.time()) + 300,
        chain_id=8453, signature_hex="",
    )
    assert not ok
    assert "required" in err


# ── _enforce_order_owner_sig helper integration ────────────────────────


@pytest.fixture(autouse=True)
def _reset_require_env():
    prev = os.environ.pop("REQUIRE_ORDER_OWNER_SIG", None)
    yield
    if prev is None:
        os.environ.pop("REQUIRE_ORDER_OWNER_SIG", None)
    else:
        os.environ["REQUIRE_ORDER_OWNER_SIG"] = prev


def _order(owner_addr: str, order_id: str = "ord-X", chain_id: int = 8453):
    return SimpleNamespace(
        submitted_by=owner_addr,
        order_id=order_id,
        chain_id=chain_id,
        params={},
    )


def test_enforce_rejects_missing_signature(owner):
    from minotaur_subnet.api.routes.orders import _enforce_order_owner_sig
    with pytest.raises(HTTPException) as exc:
        _enforce_order_owner_sig(
            _order(owner.address), action=ACTION_CANCEL,
            content_hash="", deadline=int(time.time()) + 300, signature="",
        )
    assert exc.value.status_code == 403


def test_enforce_accepts_valid_signature(owner):
    from minotaur_subnet.api.routes.orders import _enforce_order_owner_sig
    deadline = int(time.time()) + 300
    sig = sign_order_action(
        owner.key.hex(),
        action=ACTION_CANCEL, order_id="ord-X",
        deadline=deadline, chain_id=8453,
    )
    _enforce_order_owner_sig(  # no raise
        _order(owner.address), action=ACTION_CANCEL,
        content_hash="", deadline=deadline, signature=sig,
    )


def test_enforce_rejects_claimed_owner_mismatch(owner):
    """The cancel route passes submitted_by as a query param; if it
    doesn't match order.submitted_by we 403 before sig verification."""
    from minotaur_subnet.api.routes.orders import _enforce_order_owner_sig
    other = Account.create()
    with pytest.raises(HTTPException) as exc:
        _enforce_order_owner_sig(
            _order(owner.address), action=ACTION_CANCEL,
            content_hash="", deadline=int(time.time()) + 300, signature="",
            claimed_owner=other.address,
        )
    assert exc.value.status_code == 403
    assert "does not match order owner" in exc.value.detail


def test_enforce_bypassed_by_env_override(owner):
    """Operator escape hatch: REQUIRE_ORDER_OWNER_SIG=0 skips the check."""
    from minotaur_subnet.api.routes.orders import _enforce_order_owner_sig
    os.environ["REQUIRE_ORDER_OWNER_SIG"] = "0"
    _enforce_order_owner_sig(  # no raise even without sig
        _order(owner.address), action=ACTION_CANCEL,
        content_hash="", deadline=0, signature="",
    )


def test_enforce_rejects_order_without_owner():
    """If the order has no submitted_by, we can't verify anything → 400."""
    from minotaur_subnet.api.routes.orders import _enforce_order_owner_sig
    order = SimpleNamespace(submitted_by="", order_id="x", chain_id=8453, params={})
    with pytest.raises(HTTPException) as exc:
        _enforce_order_owner_sig(
            order, action=ACTION_CANCEL,
            content_hash="", deadline=int(time.time()) + 300, signature="anything",
        )
    assert exc.value.status_code == 400
