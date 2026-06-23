"""#229 fix 4: a USER signature fault at settlement must not debit the solver.

A plan that passed JS scoring + on-chain sim scoring + the validator quorum, then
reverts at executeIntent because the USER's order signature was invalid, is NOT
the solver's fault (scoreIntent never verifies the user sig). The order is still
REJECTED, but the blameless miner must not be recorded a failed execution.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.blockloop.order_processor import (
    OrderProcessor,
    _is_user_signature_fault,
)
from minotaur_subnet.relayer.base import SubmitResult


# ── classifier ────────────────────────────────────────────────────────────────

def test_classifier_flags_user_signature_reverts():
    assert _is_user_signature_fault("Transaction reverted: ('0xf645eedf', '0xf645eedf')")  # ECDSAInvalidSignature
    assert _is_user_signature_fault("execution reverted: Invalid user signature")
    assert _is_user_signature_fault("0xfce698f7")  # ECDSAInvalidSignatureLength
    assert _is_user_signature_fault("0xd78bce0c")  # ECDSAInvalidSignatureS
    assert _is_user_signature_fault("REVERT 0xF645EEDF")  # case-insensitive


def test_classifier_does_not_flag_solver_faults():
    assert not _is_user_signature_fault("Too little received")
    assert not _is_user_signature_fault("execution reverted: insufficient output amount")
    assert not _is_user_signature_fault("STF")          # SafeTransferFrom (router)
    assert not _is_user_signature_fault("nonce too low")
    assert not _is_user_signature_fault(None)
    assert not _is_user_signature_fault("")


# ── settlement-stats decision ─────────────────────────────────────────────────

def _proc():
    p = OrderProcessor.__new__(OrderProcessor)  # bypass the heavy ctor
    p.app_store = MagicMock()
    return p


def _order():
    return SimpleNamespace(order_id="ord_1", app_id="app_x")


def test_fill_records_success():
    p = _proc()
    p._record_settlement_stats(_order(), 0.7, SubmitResult(success=True))
    p.app_store.record_execution.assert_called_once_with("app_x", 0.7, success=True)


def test_user_sig_fault_does_not_debit_solver():
    # The exact prod revert (#229): 0xf645eedf -> order rejected, solver NOT debited.
    p = _proc()
    p._record_settlement_stats(
        _order(), 0.52,
        SubmitResult(success=False, error="Transaction reverted: ('0xf645eedf', '0xf645eedf')"),
    )
    p.app_store.record_execution.assert_not_called()  # blameless miner


def test_solver_fault_still_debits():
    # A router/plan revert IS the solver's fault -> recorded as a failed execution.
    p = _proc()
    p._record_settlement_stats(
        _order(), 0.4,
        SubmitResult(success=False, error="execution reverted: Too little received"),
    )
    p.app_store.record_execution.assert_called_once_with("app_x", 0.4, success=False)
