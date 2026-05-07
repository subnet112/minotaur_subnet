"""Unit tests for the Intent OrderBook."""

import sys
import time
from pathlib import Path

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.orderbook.orderbook import IntentOrderBook, Order, OrderStatus


@pytest.fixture
def ob():
    return IntentOrderBook()


class TestSubmit:
    def test_submit_creates_order(self, ob):
        order = ob.submit(
            app_id="app_test",
            intent_function="execute",
            params={"token": "WETH"},
            submitted_by="0xuser1",
        )
        assert order.order_id.startswith("ord_")
        assert order.status == OrderStatus.OPEN
        assert order.app_id == "app_test"
        assert order.params == {"token": "WETH"}
        assert order.submitted_by == "0xuser1"

    def test_submit_requires_app_id(self, ob):
        with pytest.raises(ValueError, match="app_id"):
            ob.submit(app_id="", intent_function="", params={}, submitted_by="0x1")

    def test_submit_requires_submitted_by(self, ob):
        with pytest.raises(ValueError, match="submitted_by"):
            ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="")

    def test_submit_perpetual(self, ob):
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=5,
            cooldown=60.0,
        )
        assert order.perpetual is True
        assert order.max_executions == 5
        assert order.cooldown == 60.0


class TestCancel:
    def test_cancel_open_order(self, ob):
        order = ob.submit(
            app_id="app_1", intent_function="", params={}, submitted_by="0x1"
        )
        assert ob.cancel(order.order_id, submitted_by="0x1") is True
        assert ob.get(order.order_id).status == OrderStatus.CANCELLED

    def test_cancel_nonexistent(self, ob):
        assert ob.cancel("nonexistent", submitted_by="0x1") is False

    def test_cancel_already_filled(self, ob):
        order = ob.submit(
            app_id="app_1", intent_function="", params={}, submitted_by="0x1"
        )
        ob.update_order(order.order_id, status=OrderStatus.FILLED)
        assert ob.cancel(order.order_id, submitted_by="0x1") is False

    def test_cancel_requires_owner(self, ob):
        """OB-4: Only the order's submitter can cancel it."""
        order = ob.submit(
            app_id="app_1", intent_function="", params={}, submitted_by="0xOwner"
        )
        # Wrong user gets PermissionError
        with pytest.raises(PermissionError):
            ob.cancel(order.order_id, submitted_by="0xOther")
        assert ob.get(order.order_id).status == OrderStatus.OPEN
        # Right user can cancel (case-insensitive)
        assert ob.cancel(order.order_id, submitted_by="0xowner") is True
        assert ob.get(order.order_id).status == OrderStatus.CANCELLED


class TestSnapshotOpen:
    def test_snapshot_takes_open_orders(self, ob):
        o1 = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        o2 = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x2")

        orders = ob.snapshot_open()
        assert len(orders) == 2
        assert all(o.status == OrderStatus.ASSIGNED for o in orders)

        # No more open orders
        assert len(ob.snapshot_open()) == 0

    def test_snapshot_respects_max_count(self, ob):
        for i in range(5):
            ob.submit(app_id="app_1", intent_function="", params={}, submitted_by=f"0x{i}")

        orders = ob.snapshot_open(max_count=3)
        assert len(orders) == 3

        # Remaining 2
        remaining = ob.snapshot_open()
        assert len(remaining) == 2


class TestExpire:
    def test_expire_past_deadline(self, ob):
        order = ob.submit(
            app_id="app_1",
            intent_function="",
            params={},
            submitted_by="0x1",
            deadline=time.time() - 100,  # already expired
        )
        count = ob.expire_stale()
        assert count == 1
        assert ob.get(order.order_id).status == OrderStatus.EXPIRED

    def test_no_expire_without_deadline(self, ob):
        ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        count = ob.expire_stale()
        assert count == 0

    def test_no_expire_future_deadline(self, ob):
        ob.submit(
            app_id="app_1",
            intent_function="",
            params={},
            submitted_by="0x1",
            deadline=time.time() + 3600,
        )
        count = ob.expire_stale()
        assert count == 0


class TestRateLimiting:
    def test_rate_limit_enforcement(self, ob):
        for i in range(10):
            ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0xspam")

        with pytest.raises(ValueError, match="Rate limit"):
            ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0xspam")

    def test_rate_limit_per_user(self, ob):
        for i in range(10):
            ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0xuser_a")

        # Different user is not rate limited
        order = ob.submit(
            app_id="app_1", intent_function="", params={}, submitted_by="0xuser_b"
        )
        assert order is not None


class TestListOrders:
    def test_list_all(self, ob):
        ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        ob.submit(app_id="app_2", intent_function="", params={}, submitted_by="0x2")
        assert len(ob.list_orders()) == 2

    def test_filter_by_app_id(self, ob):
        ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        ob.submit(app_id="app_2", intent_function="", params={}, submitted_by="0x2")
        assert len(ob.list_orders(app_id="app_1")) == 1

    def test_filter_by_status(self, ob):
        o1 = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x2")
        ob.update_order(o1.order_id, status=OrderStatus.FILLED)
        assert len(ob.list_orders(status="filled")) == 1
        assert len(ob.list_orders(status="open")) == 1


class TestUpdateOrder:
    def test_update_existing(self, ob):
        order = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        assert ob.update_order(order.order_id, score=0.85, tx_hash="0xabc")
        updated = ob.get(order.order_id)
        assert updated.score == 0.85
        assert updated.tx_hash == "0xabc"

    def test_update_nonexistent(self, ob):
        assert ob.update_order("nope", score=1.0) is False

    def test_update_status_from_string(self, ob):
        order = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        ob.update_order(order.order_id, status="filled")
        assert ob.get(order.order_id).status == OrderStatus.FILLED

    def test_fill_preserves_block_number_and_params(self, ob):
        """Filled orders must retain block_number, tx_hash, and params for Stage 3 regression replay."""
        params = {"input_token": "0xWETH", "output_token": "0xUSDC", "input_amount": "1000000000000000000"}
        order = ob.submit(app_id="app_1", intent_function="swap", params=params, submitted_by="0xUser")
        ob.update_order(
            order.order_id,
            status=OrderStatus.FILLED,
            tx_hash="0xabcdef",
            block_number=28000123,
        )
        filled = ob.get(order.order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.tx_hash == "0xabcdef"
        assert filled.block_number == 28000123
        assert filled.params == params
        # to_dict must also preserve these for persistence
        d = filled.to_dict()
        assert d["block_number"] == 28000123
        assert d["tx_hash"] == "0xabcdef"
        assert d["params"] == params


class TestCooldown:
    """Tests for perpetual order cooldown enforcement (OB-6, VAL-8)."""

    def test_snapshot_skips_orders_in_cooldown(self, ob):
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=60.0,
        )
        # Simulate a recent fill
        ob.update_order(order.order_id, last_filled_at=time.time(), status=OrderStatus.OPEN)
        # Should be skipped because cooldown hasn't expired
        orders = ob.snapshot_open()
        assert len(orders) == 0

    def test_snapshot_includes_expired_cooldown(self, ob):
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=60.0,
        )
        # Simulate a fill 120 seconds ago (cooldown is 60s, so expired)
        ob.update_order(
            order.order_id,
            last_filled_at=time.time() - 120,
            status=OrderStatus.OPEN,
        )
        orders = ob.snapshot_open()
        assert len(orders) == 1

    def test_snapshot_global_floor_protects_zero_cooldown(self, ob, monkeypatch):
        """PERPETUAL_MIN_INTERVAL_SECONDS (default 60) floors per-order cooldown
        so a perpetual with cooldown=0 still can't refill every tick."""
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=0.0,
        )
        ob.update_order(order.order_id, last_filled_at=time.time(), status=OrderStatus.OPEN)

        # Default floor is 60s — recent fill should block it.
        monkeypatch.delenv("PERPETUAL_MIN_INTERVAL_SECONDS", raising=False)
        assert len(ob.snapshot_open()) == 0

    def test_snapshot_floor_can_be_disabled(self, ob, monkeypatch):
        """Setting PERPETUAL_MIN_INTERVAL_SECONDS=0 removes the floor."""
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=0.0,
        )
        ob.update_order(order.order_id, last_filled_at=time.time(), status=OrderStatus.OPEN)

        monkeypatch.setenv("PERPETUAL_MIN_INTERVAL_SECONDS", "0")
        assert len(ob.snapshot_open()) == 1

    def test_snapshot_floor_yields_to_longer_per_order_cooldown(self, ob, monkeypatch):
        """When per-order cooldown > floor, per-order cooldown wins (effective
        cooldown is max(order.cooldown, floor))."""
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=True,
            max_executions=10,
            cooldown=300.0,  # 5 min, larger than the 60s floor
        )
        # Fill 90s ago — past the 60s floor but still within the 300s per-order.
        ob.update_order(order.order_id, last_filled_at=time.time() - 90, status=OrderStatus.OPEN)
        monkeypatch.delenv("PERPETUAL_MIN_INTERVAL_SECONDS", raising=False)
        assert len(ob.snapshot_open()) == 0

    def test_snapshot_non_perpetual_ignores_floor(self, ob):
        """One-shot orders shouldn't be affected by the perpetual floor."""
        order = ob.submit(
            app_id="app_1",
            intent_function="execute",
            params={},
            submitted_by="0xuser",
            perpetual=False,
        )
        # last_filled_at would only be set after a fill, but simulate anyway.
        ob.update_order(order.order_id, last_filled_at=time.time(), status=OrderStatus.OPEN)
        assert len(ob.snapshot_open()) == 1


class TestStats:
    def test_stats(self, ob):
        ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x1")
        o2 = ob.submit(app_id="app_1", intent_function="", params={}, submitted_by="0x2")
        ob.update_order(o2.order_id, status=OrderStatus.FILLED)

        stats = ob.stats()
        assert stats["open"] == 1
        assert stats["filled"] == 1
        assert ob.count == 2
