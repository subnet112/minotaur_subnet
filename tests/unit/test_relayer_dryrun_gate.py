"""Pre-broadcast dry-run gate: never broadcast a tx that would revert.

Regression for the balance-less order-spam griefing vector. The scoring fork
fabricates the user's input-token balance, so an impossible (no-funds) order
still scores as doable and reaches the relayer. The relayer's gas estimate
previously SWALLOWED the revert (2M fallback) and broadcast anyway, burning
relayer gas on a guaranteed on-chain revert. The dry run now aborts the
broadcast and fails the order — while a TRANSIENT RPC error still falls back so
a flaky node never falsely fails a legitimate order.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.relayer.evm_relayer import (
    EvmRelayer,
    _DryRunRevert,
    _is_execution_revert,
)

try:
    from web3.exceptions import ContractLogicError
except Exception:  # pragma: no cover - web3 always present in this repo
    ContractLogicError = None


def _w3(side):
    w3 = MagicMock()
    if isinstance(side, BaseException):
        w3.eth.estimate_gas.side_effect = side
    else:
        w3.eth.estimate_gas.return_value = side
    return w3


# ── revert vs transient classification ───────────────────────────────────────

@pytest.mark.skipif(ContractLogicError is None, reason="web3 ContractLogicError unavailable")
def test_contractlogicerror_is_a_revert():
    assert _is_execution_revert(
        ContractLogicError("execution reverted: ERC20: transfer amount exceeds balance")
    )


def test_plain_execution_reverted_string_is_a_revert():
    assert _is_execution_revert(Exception("execution reverted"))


def test_transient_and_gasfunds_errors_are_NOT_reverts():
    # A flaky RPC / disconnect must not be read as a revert (would falsely fail a
    # legit order).
    assert not _is_execution_revert(ConnectionError("RPC down"))
    assert not _is_execution_revert(TimeoutError("read timed out"))
    # The RELAYER's own gas-funds error is a pre-execution check, not an
    # execution revert — must not fail the user's order.
    assert not _is_execution_revert(ValueError("insufficient funds for gas * price + value"))


# ── _dry_run_or_raise ─────────────────────────────────────────────────────────

def _relayer():
    # The method uses only `w3` + module-level helpers, so an uninitialised
    # instance is sufficient (avoids the heavy ctor).
    return EvmRelayer.__new__(EvmRelayer)


def test_dry_run_returns_margined_gas_on_success():
    assert _relayer()._dry_run_or_raise(_w3(100_000), {}) == int(100_000 * 1.5)


def test_dry_run_raises_dryrunrevert_on_execution_revert():
    w3 = _w3(Exception("execution reverted: ERC20: transfer amount exceeds balance"))
    with pytest.raises(_DryRunRevert) as ei:
        _relayer()._dry_run_or_raise(w3, {})
    assert "transfer amount exceeds balance" in ei.value.reason


def test_dry_run_falls_back_to_2M_on_transient_error():
    # Transient → broadcast still proceeds (do not punish a legit order for a
    # flaky node).
    assert _relayer()._dry_run_or_raise(_w3(ConnectionError("RPC timeout")), {}) == 2_000_000


def test_dry_run_does_not_broadcast_on_revert():
    # The estimate path raised; nothing about a broadcast/send happens here — the
    # caller turns _DryRunRevert into a SubmitResult(success=False) without ever
    # calling send_raw_transaction.
    w3 = _w3(Exception("execution reverted"))
    with pytest.raises(_DryRunRevert):
        _relayer()._dry_run_or_raise(w3, {})
    w3.eth.send_raw_transaction.assert_not_called()
