"""Intent OrderBook — core order management for the Minotaur v2 architecture.

The OrderBook is the universal entry point for all intent execution. Users submit
signed orders (one-shot or perpetual). Each tick, the block loop drains OPEN orders,
generates plans, scores them, and routes approved plans to the relayer.

Rate limiting: 10 orders per user per minute.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderStatus(str, Enum):
    """Lifecycle status of an order in the book."""
    OPEN = "open"
    ASSIGNED = "assigned"        # Picked up by block loop tick
    SOLVED = "solved"            # Plan generated
    SCORED = "scored"            # JS scoring complete
    APPROVED = "approved"        # Passed consensus
    SUBMITTED = "submitted"      # Sent to relayer
    FILLED = "filled"            # On-chain execution confirmed
    REJECTED = "rejected"        # Failed scoring/validation
    EXPIRED = "expired"          # Past deadline
    CANCELLED = "cancelled"      # User cancelled
    UNSTAKING = "unstaking"      # Substrate unstake in progress (alpha → TAO)
    BRIDGING = "bridging"        # Source leg done, awaiting bridge completion
    BRIDGE_FAILED = "bridge_failed"  # Bridge transfer failed
    EXECUTING_LEG = "executing_leg"  # Multi-leg: processing a forward leg
    ROLLING_BACK = "rolling_back"    # Multi-leg: forward failed, executing rollback
    ROLLED_BACK = "rolled_back"      # Multi-leg: rollback completed
    PARTIAL_ROLLBACK = "partial_rollback"  # Multi-leg: rollback partially completed


@dataclass
class Order:
    """A single order in the Intent OrderBook."""
    order_id: str
    app_id: str
    intent_function: str         # Intent function name on the app contract
    params: dict[str, Any]       # User-provided parameters
    submitted_by: str            # User wallet address
    status: OrderStatus = OrderStatus.OPEN
    chain_id: int = 1
    deadline: float = 0.0        # Unix timestamp; 0 = no deadline
    perpetual: bool = False
    max_executions: int = 1      # For perpetual orders
    cooldown: float = 0.0        # Seconds between perpetual fills
    execution_count: int = 0
    last_filled_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    plan: dict[str, Any] | None = None
    score: float | None = None
    tx_hash: str | None = None
    error: str | None = None
    block_number: int | None = None
    consensus_result: dict[str, Any] | None = None
    user_signature: str = ""             # EIP-712 user signature (hex)
    on_chain_score: int | None = None    # BPS (0-10000) from contract scoreIntent()
    best_score: float | None = None      # Best JS score seen (even if below threshold)
    policy_tier: str = "hybrid"
    automation_id: str = ""
    plan_assessment: dict[str, Any] | None = None
    # Fee model (skeleton — accounting only for MVP)
    fee_amount_wei: int = 0          # wTAO fee in wei (0 = no fee)
    fee_paid: bool = False           # True after successful fill

    def to_dict(self) -> dict[str, Any]:
        result = {
            "order_id": self.order_id,
            "app_id": self.app_id,
            "intent_function": self.intent_function,
            "params": self.params,
            "submitted_by": self.submitted_by,
            "status": self.status.value,
            "chain_id": self.chain_id,
            "deadline": self.deadline,
            "perpetual": self.perpetual,
            "max_executions": self.max_executions,
            "cooldown": self.cooldown,
            "execution_count": self.execution_count,
            "last_filled_at": self.last_filled_at,
            "created_at": self.created_at,
            "plan": self.plan,
            "score": self.score,
            "tx_hash": self.tx_hash,
            "error": self.error,
            "block_number": self.block_number,
            "consensus_result": self.consensus_result,
            "user_signature": self.user_signature,
            "on_chain_score": self.on_chain_score,
            "best_score": self.best_score,
            "policy_tier": self.policy_tier,
            "automation_id": self.automation_id,
            "plan_assessment": self.plan_assessment,
            "fee_amount_wei": self.fee_amount_wei,
            "fee_paid": self.fee_paid,
        }
        if self.submitted_by and self.chain_id:
            result["interop_address"] = f"eip155:{self.chain_id}:{self.submitted_by}"
        return result


# Rate limit: 10 orders per user per minute
_RATE_LIMIT = 10
_RATE_WINDOW = 60.0


class IntentOrderBook:
    """In-memory order book with rate limiting and atomic snapshots.

    Thread-safe via a simple lock. The block loop calls snapshot_open()
    each tick to atomically claim OPEN orders for processing.
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._lock = threading.Lock()
        self._rate_limiter: dict[str, deque[float]] = {}

    def submit(
        self,
        app_id: str,
        intent_function: str,
        params: dict[str, Any],
        submitted_by: str,
        chain_id: int = 0,
        deadline: float = 0.0,
        perpetual: bool = False,
        max_executions: int = 1,
        cooldown: float = 0.0,
        user_signature: str = "",
        policy_tier: str = "hybrid",
        automation_id: str = "",
    ) -> Order:
        """Submit a new order to the book.

        Raises ValueError on rate limit or invalid params.
        """
        if not app_id:
            raise ValueError("app_id is required")
        if not submitted_by:
            raise ValueError("submitted_by is required")

        with self._lock:
            self._check_rate_limit(submitted_by)

            order = Order(
                order_id=f"ord_{uuid.uuid4().hex[:16]}",
                app_id=app_id,
                intent_function=intent_function or "execute",
                params=params or {},
                submitted_by=submitted_by,
                chain_id=chain_id,
                deadline=deadline,
                perpetual=perpetual,
                max_executions=max_executions if perpetual else 1,
                cooldown=cooldown,
                user_signature=user_signature,
                policy_tier=policy_tier,
                automation_id=automation_id,
            )
            self._orders[order.order_id] = order
            return order

    def cancel(self, order_id: str, submitted_by: str) -> bool:
        """Cancel an open or assigned order. Returns True if cancelled.

        Only the original submitter can cancel their own order (OB-4).
        Raises PermissionError if the caller is not the owner.
        """
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            if order.submitted_by.lower() != submitted_by.lower():
                raise PermissionError(
                    f"Order {order_id} belongs to {order.submitted_by}, "
                    f"not {submitted_by}"
                )
            if order.status not in (OrderStatus.OPEN, OrderStatus.ASSIGNED):
                return False
            order.status = OrderStatus.CANCELLED
            return True

    def snapshot_open(self, max_count: int = 50) -> list[Order]:
        """Atomically take up to max_count OPEN orders, marking them ASSIGNED.

        Perpetual orders are skipped when any of these hold:
          - they have their own cooldown and haven't cleared it yet, OR
          - the global PERPETUAL_MIN_INTERVAL_SECONDS floor (default 60s)
            hasn't elapsed since their last fill. The floor protects against
            orders submitted with cooldown=0 that would otherwise refill on
            every tick and risk nonce-race failures against the pending relay.
        """
        import os as _os
        try:
            global_min_interval = float(
                _os.environ.get("PERPETUAL_MIN_INTERVAL_SECONDS", "60").strip() or 60
            )
        except ValueError:
            global_min_interval = 60.0
        now = time.time()
        with self._lock:
            result: list[Order] = []
            for order in self._orders.values():
                if order.status == OrderStatus.OPEN:
                    # Skip perpetual orders still in cooldown (OB-6, VAL-8).
                    # Effective cooldown is max(order.cooldown, global floor)
                    # so even an order submitted with cooldown=0 can't refill
                    # on every tick.
                    if order.perpetual and order.last_filled_at > 0:
                        effective_cd = max(float(order.cooldown or 0.0), global_min_interval)
                        if now - order.last_filled_at < effective_cd:
                            continue
                    order.status = OrderStatus.ASSIGNED
                    result.append(order)
                    if len(result) >= max_count:
                        break
            return result

    def update_order(self, order_id: str, **kwargs: Any) -> bool:
        """Update fields on an existing order. Returns True if found."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            for key, value in kwargs.items():
                if key == "status" and isinstance(value, str):
                    value = OrderStatus(value)
                if hasattr(order, key):
                    setattr(order, key, value)
            return True

    def get(self, order_id: str) -> Order | None:
        """Get an order by ID."""
        return self._orders.get(order_id)

    def list_orders(
        self,
        app_id: str | None = None,
        status: str | None = None,
    ) -> list[Order]:
        """List orders with optional filters."""
        result = list(self._orders.values())
        if app_id:
            result = [o for o in result if o.app_id == app_id]
        if status:
            try:
                status_enum = OrderStatus(status)
                result = [o for o in result if o.status == status_enum]
            except ValueError:
                pass
        return result

    def expire_stale(self, now: float | None = None) -> int:
        """Expire orders past their deadline. Returns count expired."""
        now = now or time.time()
        count = 0
        with self._lock:
            for order in self._orders.values():
                if (
                    order.deadline > 0
                    and now > order.deadline
                    and order.status in (OrderStatus.OPEN, OrderStatus.ASSIGNED)
                ):
                    order.status = OrderStatus.EXPIRED
                    count += 1
        return count

    def _check_rate_limit(self, user: str) -> None:
        """Enforce rate limiting. Must be called with lock held."""
        now = time.time()
        if user not in self._rate_limiter:
            self._rate_limiter[user] = deque()

        timestamps = self._rate_limiter[user]
        # Purge old entries
        while timestamps and timestamps[0] < now - _RATE_WINDOW:
            timestamps.popleft()

        if len(timestamps) >= _RATE_LIMIT:
            raise ValueError(
                f"Rate limit exceeded: {_RATE_LIMIT} orders per {_RATE_WINDOW}s"
            )
        timestamps.append(now)

    @property
    def count(self) -> int:
        """Total number of orders in the book."""
        return len(self._orders)

    def stats(self) -> dict[str, int]:
        """Return count by status."""
        counts: dict[str, int] = {}
        for order in self._orders.values():
            key = order.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts
