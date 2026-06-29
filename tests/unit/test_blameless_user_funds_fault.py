"""Blameless miner on a USER-funds settlement revert (#229).

The scoring fork fabricates the user's input-token balance, so a balance-less /
impossible order still passes scoring + quorum and reaches settlement, where
executeIntent's transferFrom-from-user reverts. That is a USER fault, not the
solver's — so it must NOT debit the (blameless) champion's execution stats.
Otherwise an attacker could spam impossible orders to tank an honest miner.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.blockloop.order_processor import (
    OrderProcessor,
    _is_user_fund_fault,
    _is_user_fault,
    _is_user_signature_fault,
)


# ── classification ────────────────────────────────────────────────────────────

def test_fund_fault_matches_oz_v4_and_v5():
    assert _is_user_fund_fault("execution reverted: ERC20: transfer amount exceeds balance")
    assert _is_user_fund_fault("ERC20: transfer amount exceeds allowance")
    assert _is_user_fund_fault("ERC20: insufficient allowance")
    assert _is_user_fund_fault("reverted: ERC20InsufficientBalance(0xabc, 0, 100)")
    assert _is_user_fund_fault("ERC20InsufficientAllowance(...)")


def test_fund_fault_does_not_match_solver_reverts():
    # Slippage / router faults are the SOLVER's fault — must still be debited.
    assert not _is_user_fund_fault("Too little received")
    assert not _is_user_fund_fault("execution reverted: STF")
    assert not _is_user_fund_fault("execution reverted")
    assert not _is_user_fund_fault(None)


def test_is_user_fault_covers_signature_and_funds_not_solver():
    assert _is_user_fault("ECDSAInvalidSignature")          # signature fault
    assert _is_user_fault("ERC20: transfer amount exceeds balance")  # funds fault
    assert not _is_user_fault("Too little received")        # solver fault
    # sanity: the signature classifier is unchanged
    assert _is_user_signature_fault("0xf645eedf")
    assert not _is_user_signature_fault("ERC20: transfer amount exceeds balance")


# ── blameless stats behaviour ─────────────────────────────────────────────────

def _op():
    op = OrderProcessor.__new__(OrderProcessor)  # only uses self.app_store + logger
    op.app_store = MagicMock()
    return op


_ORDER = SimpleNamespace(order_id="ord_x", app_id="app_1")


def test_user_funds_revert_does_not_debit_solver():
    op = _op()
    op._record_settlement_stats(
        _ORDER, 0.5,
        SimpleNamespace(success=False, error="execution reverted: ERC20: transfer amount exceeds balance"),
    )
    op.app_store.record_execution.assert_not_called()


def test_user_signature_revert_does_not_debit_solver():
    op = _op()
    op._record_settlement_stats(
        _ORDER, 0.5, SimpleNamespace(success=False, error="ECDSAInvalidSignature"),
    )
    op.app_store.record_execution.assert_not_called()


def test_solver_revert_debits_solver():
    op = _op()
    op._record_settlement_stats(
        _ORDER, 0.5, SimpleNamespace(success=False, error="Too little received"),
    )
    op.app_store.record_execution.assert_called_once()
    assert op.app_store.record_execution.call_args.kwargs.get("success") is False


def test_success_records_success():
    op = _op()
    op._record_settlement_stats(
        _ORDER, 0.5, SimpleNamespace(success=True, error=None),
    )
    op.app_store.record_execution.assert_called_once()
    assert op.app_store.record_execution.call_args.kwargs.get("success") is True
