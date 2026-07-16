"""Unit tests for #225/#226: the solver no-plan order resolution."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from minotaur_subnet.blockloop.order_processor import OrderProcessor
from minotaur_subnet.orderbook.orderbook import OrderStatus


def _processor():
    # All deps are injected + only assigned in __init__, so MagicMocks suffice
    # for exercising _handle_no_plan in isolation.
    p = OrderProcessor.__new__(OrderProcessor)
    p.orderbook = MagicMock()
    p.app_store = MagicMock()
    p.order_persistence = MagicMock()
    return p


def _order(*, perpetual, execution_count=0, max_executions=1, deadline=0):
    return SimpleNamespace(
        order_id="ord_1", app_id="app_1", perpetual=perpetual,
        execution_count=execution_count, max_executions=max_executions,
        deadline=deadline,
    )


def test_one_shot_no_plan_is_rejected_and_debits_miner():
    p = _processor()
    p._handle_no_plan(_order(perpetual=False))
    # REJECTED with a reason
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    assert "no plan" in kwargs["error"].lower()
    # miner debited + persisted
    p.app_store.record_execution.assert_called_once_with("app_1", 0.0, success=False)
    p.order_persistence.sync.assert_called_once_with("ord_1")


def test_perpetual_no_plan_requeues_open_and_does_not_debit():
    p = _processor()
    p._handle_no_plan(_order(perpetual=True, execution_count=0, max_executions=5))
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.OPEN
    assert "last_filled_at" in kwargs  # cooldown backoff
    p.order_persistence.sync.assert_called_once_with("ord_1")
    # retry, not a terminal failure -> NOT debited
    p.app_store.record_execution.assert_not_called()


def test_exhausted_perpetual_no_plan_is_terminal():
    # A perpetual that has used all executions falls through to the terminal path.
    p = _processor()
    p._handle_no_plan(_order(perpetual=True, execution_count=5, max_executions=5))
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    p.app_store.record_execution.assert_called_once_with("app_1", 0.0, success=False)


def test_perpetual_does_not_consume_execution_slot():
    # The requeue must NOT increment execution_count (the cycle never filled).
    p = _processor()
    p._handle_no_plan(_order(perpetual=True, execution_count=2, max_executions=5))
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert "execution_count" not in kwargs


def test_perpetual_past_deadline_no_plan_is_terminal():
    # A live-slot perpetual whose signed deadline has passed can no longer fill
    # (the contract enforces block.timestamp <= deadline), so it is terminal.
    import time as _t
    p = _processor()
    p._handle_no_plan(
        _order(perpetual=True, execution_count=0, max_executions=5,
               deadline=_t.time() - 10),
    )
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.REJECTED
    p.app_store.record_execution.assert_called_once_with("app_1", 0.0, success=False)


def test_try_requeue_perpetual_below_threshold_requeues_without_debit():
    # The score-gate branch is the perpetual's real trigger condition: a live
    # perpetual resting below threshold must requeue OPEN, keep its slot, and
    # NOT debit the miner (a user's price not being hit is not a solver fault).
    import time as _t
    p = _processor()
    order = _order(perpetual=True, execution_count=1, max_executions=10,
                   deadline=_t.time() + 3600)
    assert p._try_requeue_perpetual(order, "on-chain score 4000 BPS < 5000") is True
    kwargs = p.orderbook.update_order.call_args.kwargs
    assert kwargs["status"] == OrderStatus.OPEN
    assert "last_filled_at" in kwargs
    assert "execution_count" not in kwargs
    p.app_store.record_execution.assert_not_called()


def test_try_requeue_perpetual_returns_false_for_one_shot():
    # One-shot orders are left untouched so the caller applies terminal handling.
    p = _processor()
    order = _order(perpetual=False)
    assert p._try_requeue_perpetual(order, "below threshold") is False
    p.orderbook.update_order.assert_not_called()
