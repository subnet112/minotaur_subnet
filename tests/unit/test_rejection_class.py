"""Tests for the structured order-rejection classifier.

``classify_rejection`` folds the free-text ``error`` on a terminal order into a
small, stable enum so the frontend / dashboards can filter deterministically
instead of string-matching prose. The cases below are pinned to the ACTUAL error
strings observed live on the leader (4,023 rejected orders on 2026-07-16), so a
reworded message that would silently drop out of a class is caught here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.orderbook.rejection import (
    RejectionClass,
    classify_rejection,
    is_user_fault,
    is_user_fund_fault,
    is_user_signature_fault,
)


# ── classify_rejection: live error strings → class ─────────────────────

# (error string seen in production, expected class). Keep these verbatim from
# the emitting call sites — order_processor / relayer / safeguards.
_LIVE_CASES = [
    # Duplicate — the trade was already submitted for this order/fill-round.
    ("Relayer submission failed: plan_hash already submitted "
     "(re-submittable after 42s)", RejectionClass.DUPLICATE),
    # User fault — bad order signature (settlement prefix + raw selector).
    ("User signature rejected at settlement: pre-broadcast dry-run reverted: "
     "('0xf645eedf', '0xf645eedf')", RejectionClass.USER),
    # User fault — insufficient balance/allowance (explicit settlement prefix).
    ("User cannot fund order (insufficient input-token balance/allowance) at "
     "settlement: execution reverted", RejectionClass.USER),
    # User fault — funds revert under a GENERIC relayer wrapper (must still be
    # user, not solver — mirrors order_processor's blameless-miner attribution).
    ("Relayer submission failed: Transaction reverted: "
     "(execution reverted: ERC20: transfer amount exceeds balance)",
     RejectionClass.USER),
    # Infra — dual-scoring fail-closed.
    ("On-chain score unavailable — contract returned invalid or unreadable "
     "(dual-scoring fail-closed)", RejectionClass.INFRA),
    # Infra — consensus.
    ("Consensus not reached", RejectionClass.INFRA),
    # Infra — the EIP-55 checksum encode bug (PR #876).
    ("Relayer submission failed: Failed after 2 attempts: "
     "('Address has an invalid EIP-55 checksum. ...')", RejectionClass.INFRA),
    ("Relayer submission failed: Failed after 2 attempts: "
     "('web3.py only accepts checksum addresses. ...')", RejectionClass.INFRA),
    # Infra — per-caller throttle.
    ("Relayer submission failed: caller 0xabc0001 exceeded per-window limit: "
     "60/60 in last 3600s", RejectionClass.INFRA),
    # Infra — relayer gas / key / transport.
    ("Relayer submission failed: Relayer balance too low on chain 8453: "
     "0.001 ETH < 0.01 ETH minimum", RejectionClass.INFRA),
    ("Relayer submission failed: HttpRelayer requires signing_key "
     "(set VALIDATOR_PRIVATE_KEY on the api)", RejectionClass.INFRA),
    ("Relayer submission failed: relayer transport: Cannot connect to host "
     "relayer:8080", RejectionClass.INFRA),
    # Solver — on-chain score gate.
    ("On-chain score 100 BPS < threshold 5000", RejectionClass.SOLVER),
    # Solver — no plan.
    ("Order ord_x: solver produced no plan — REJECTED", RejectionClass.SOLVER),
    # Solver — JS sentinel gate.
    ("Score 0.0000 below threshold 0.5", RejectionClass.SOLVER),
    # Solver — policy / fee.
    ("Policy rejected plan: some reason", RejectionClass.SOLVER),
    ("Fee certification failed: fee too low", RejectionClass.SOLVER),
    # Other — a rejected order with an unrecognized error.
    ("Some brand-new error we haven't taught the classifier", RejectionClass.OTHER),
]


@pytest.mark.parametrize("error,expected", _LIVE_CASES)
def test_classify_live_error_strings(error, expected):
    assert classify_rejection("rejected", error) == expected


def test_duplicate_is_marked_non_failure():
    """The whole point: a ``duplicate`` was already served, so a success-rate
    must be able to exclude it. NON_FAILURE is the canonical set to exclude."""
    assert RejectionClass.DUPLICATE in RejectionClass.NON_FAILURE
    assert RejectionClass.USER not in RejectionClass.NON_FAILURE
    assert RejectionClass.INFRA not in RejectionClass.NON_FAILURE


def test_non_failure_statuses_have_no_class():
    """A class is the 'this terminally failed' signal — filled/cancelled/
    in-flight orders return None."""
    assert classify_rejection("filled", None) is None
    assert classify_rejection("cancelled", "irrelevant") is None
    assert classify_rejection("open", None) is None
    assert classify_rejection("assigned", None) is None


def test_expired_status_classifies_as_expired():
    assert classify_rejection("expired", None) == RejectionClass.EXPIRED


def test_rejected_with_empty_error_is_other():
    assert classify_rejection("rejected", None) == RejectionClass.OTHER
    assert classify_rejection("rejected", "") == RejectionClass.OTHER


def test_status_case_insensitive():
    assert classify_rejection("REJECTED", "Consensus not reached") == RejectionClass.INFRA
    assert classify_rejection("Expired", None) == RejectionClass.EXPIRED


# ── user-fault helpers (shared with order_processor blameless-miner #229) ──


def test_user_fund_fault_markers():
    assert is_user_fund_fault("execution reverted: ERC20: transfer amount exceeds balance")
    assert is_user_fund_fault("ERC20: transfer amount exceeds allowance")
    assert is_user_fund_fault("ERC20InsufficientBalance(0xabc, 0, 100)")
    assert is_user_fund_fault("User cannot fund order (insufficient ...)")
    assert not is_user_fund_fault("Too little received")
    assert not is_user_fund_fault(None)


def test_user_signature_fault_markers():
    assert is_user_signature_fault("0xf645eedf")
    assert is_user_signature_fault("User signature rejected at settlement: ...")
    assert not is_user_signature_fault("execution reverted: STF")


def test_is_user_fault_is_union():
    assert is_user_fault("0xf645eedf")
    assert is_user_fault("transfer amount exceeds balance")
    assert not is_user_fault("Consensus not reached")


# ── Order.to_dict exposes rejection_class ──────────────────────────────


def test_order_to_dict_includes_rejection_class():
    from minotaur_subnet.orderbook.orderbook import IntentOrderBook, OrderStatus

    ob = IntentOrderBook()
    order = ob.submit(
        app_id="app_x",
        intent_function="swap",
        params={},
        submitted_by="0xB763F651776690F7b142e5D40A7C096Aa963f04e",
        chain_id=8453,
    )
    # Fresh order: not a failure → None.
    assert order.to_dict()["rejection_class"] is None

    ob.update_order(
        order.order_id,
        status=OrderStatus.REJECTED,
        error="Relayer submission failed: plan_hash already submitted (re-submittable after 30s)",
    )
    d = ob.get(order.order_id).to_dict()
    assert d["rejection_class"] == RejectionClass.DUPLICATE
