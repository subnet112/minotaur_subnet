"""Relayer pre-submit safeguards.

A bundle of cheap checks the relayer service runs BEFORE handing off to
``EvmRelayer.submit_plan``, all designed to either (a) reject invalid
requests without spending gas, or (b) bound the gas damage a malicious
caller (including a malicious validator-of-record) can do.

Layers:

1. **Deadline check** — reject plans whose ``deadline`` has passed.
   Catches replay of expired plans cheaply.

2. **Plan-hash dedup** — in-memory ``set[plan_hash]`` with TTL bounded
   by the plan's deadline + grace. Each unique plan submittable exactly
   once. Survives RPC failure & retry-noise of the calling code.
   Restart-vulnerable; the on-chain nonce burn is the authoritative
   replay defense underneath this.

3. **Per-caller rate limit** — sliding-window counter keyed by the
   wrapper's signer address. Bounds how many submissions any single
   validator (including the current leader, even if malicious) can
   make in a window. Default: 60/hour, configurable.

4. **Gas wallet daily cap** — hard ETH-per-day ceiling. If submission
   gas usage hits the cap, refuse new submissions until the next
   UTC day. Bounds worst-case griefing damage even if every other
   layer fails.

Pre-simulation (rejecting txs that would revert on-chain) is NOT
implemented here because ``EvmRelayer.submit_plan`` already calls
``estimate_gas`` with a 1.5x multiplier — if the tx would revert,
``estimate_gas`` returns an RPC error and ``submit_plan`` returns
``SubmitResult(success=False, ...)`` without broadcasting. No gas
spent. This module's checks are layered on top of that built-in
safeguard.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Tunables — all overrideable via env so we can adjust without restart-the-world.
DEFAULT_DEDUP_GRACE_SECONDS = 600  # how long after deadline to keep a plan_hash
DEFAULT_PER_CALLER_LIMIT = 60       # submissions
DEFAULT_PER_CALLER_WINDOW_SECONDS = 3600  # 60/hour
DEFAULT_DAILY_GAS_CAP_ETH = 0.5     # halt submissions after 0.5 ETH of gas in 24h


@dataclass
class _DailyGasState:
    day_start_utc: int = 0
    gas_used_wei: int = 0


@dataclass
class Safeguards:
    """Pre-submit safeguards. Instantiate once at startup, share across
    requests. Thread-safe via a single lock (request rate is far below
    the lock's overhead)."""

    per_caller_limit: int = DEFAULT_PER_CALLER_LIMIT
    per_caller_window_seconds: int = DEFAULT_PER_CALLER_WINDOW_SECONDS
    daily_gas_cap_wei: int = field(default_factory=lambda: int(DEFAULT_DAILY_GAS_CAP_ETH * 10**18))
    dedup_grace_seconds: int = DEFAULT_DEDUP_GRACE_SECONDS

    # state
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # plan_hash -> evict_at (unix seconds)
    _seen_plan_hashes: dict[str, int] = field(default_factory=dict)
    # caller_addr_lower -> list[unix_seconds] of recent successful submissions
    _caller_history: dict[str, list[int]] = field(default_factory=dict)
    # signer_addr_lower -> last submission_nonce we accepted
    _nonce_high_water: dict[str, int] = field(default_factory=dict)
    # per-day gas accumulator
    _daily_gas: _DailyGasState = field(default_factory=_DailyGasState)

    @classmethod
    def from_env(cls) -> "Safeguards":
        """Construct with env-overridable tunables."""
        def _int_env(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default

        def _float_env(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default

        cap_eth = _float_env("RELAYER_DAILY_GAS_CAP_ETH", DEFAULT_DAILY_GAS_CAP_ETH)
        return cls(
            per_caller_limit=_int_env("RELAYER_PER_CALLER_LIMIT", DEFAULT_PER_CALLER_LIMIT),
            per_caller_window_seconds=_int_env("RELAYER_PER_CALLER_WINDOW_SECONDS", DEFAULT_PER_CALLER_WINDOW_SECONDS),
            daily_gas_cap_wei=int(cap_eth * 10**18),
            dedup_grace_seconds=_int_env("RELAYER_DEDUP_GRACE_SECONDS", DEFAULT_DEDUP_GRACE_SECONDS),
        )

    # ── Public checks. Each returns (ok: bool, error: str) ─────────────
    # On reject the relayer returns 400/409 with the error text. None
    # of these change state on rejection.

    def check_deadline(self, plan_deadline: int, now: int | None = None) -> tuple[bool, str]:
        now = int(now if now is not None else time.time())
        if int(plan_deadline) <= now:
            return False, f"plan deadline expired ({plan_deadline} <= now {now})"
        return True, ""

    def check_plan_hash_unseen(self, plan_hash: str, plan_deadline: int) -> tuple[bool, str]:
        """Check + reserve the plan_hash. Idempotent at the hash level:
        same hash twice returns False the second time, and the cache
        evicts when (deadline + grace) elapses.
        """
        now = int(time.time())
        with self._lock:
            self._evict_expired(now)
            existing = self._seen_plan_hashes.get(plan_hash)
            if existing is not None and existing > now:
                return False, f"plan_hash already submitted (re-submittable after {existing - now}s)"
            self._seen_plan_hashes[plan_hash] = int(plan_deadline) + self.dedup_grace_seconds
            return True, ""

    def check_caller_rate(self, caller_addr: str) -> tuple[bool, str]:
        """Check the caller hasn't exceeded the per-window limit.
        Counts on a sliding window — older entries auto-eviction.
        """
        now = int(time.time())
        cutoff = now - self.per_caller_window_seconds
        key = caller_addr.lower()
        with self._lock:
            history = self._caller_history.get(key, [])
            history = [t for t in history if t > cutoff]
            if len(history) >= self.per_caller_limit:
                self._caller_history[key] = history  # write back the pruned list
                return False, (
                    f"caller {caller_addr[:10]} exceeded per-window limit: "
                    f"{len(history)}/{self.per_caller_limit} in last {self.per_caller_window_seconds}s"
                )
            history.append(now)
            self._caller_history[key] = history
            return True, ""

    def check_signer_nonce(self, signer_addr: str, claimed_nonce: int) -> tuple[bool, str]:
        """Enforce strict monotonic nonces per signer. Updates state on
        accept — call only after wrapper sig verification succeeds.
        """
        key = signer_addr.lower()
        with self._lock:
            last = self._nonce_high_water.get(key, 0)
            if claimed_nonce <= last:
                return False, (
                    f"non-monotonic nonce from {signer_addr[:10]}: "
                    f"claimed {claimed_nonce}, last accepted {last}"
                )
            self._nonce_high_water[key] = int(claimed_nonce)
            return True, ""

    def check_daily_gas_room(self) -> tuple[bool, str]:
        """Reject if today's submitted-gas total is at or above the cap.
        Called BEFORE submit. Doesn't change state — call ``record_gas_used``
        after a successful submission to charge against the budget.
        """
        now = int(time.time())
        today_start = now - (now % 86400)
        with self._lock:
            if self._daily_gas.day_start_utc != today_start:
                self._daily_gas = _DailyGasState(day_start_utc=today_start, gas_used_wei=0)
            if self._daily_gas.gas_used_wei >= self.daily_gas_cap_wei:
                return False, (
                    f"daily gas cap hit: {self._daily_gas.gas_used_wei / 10**18:.4f} / "
                    f"{self.daily_gas_cap_wei / 10**18:.4f} ETH used today; "
                    "halting submissions until next UTC day"
                )
            return True, ""

    def record_gas_used(self, gas_used: int, gas_price_wei: int) -> None:
        """Charge a mined submission against the daily budget — on success OR an
        on-chain revert (both burn real gas). Caller passes gas_used=0 for
        pre-broadcast failures so nothing is charged for gas that was not spent."""
        cost_wei = int(gas_used) * int(gas_price_wei)
        with self._lock:
            self._daily_gas.gas_used_wei += cost_wei

    # ── Internals ──────────────────────────────────────────────────────

    def _evict_expired(self, now: int) -> None:
        """Drop plan_hashes whose expiration has passed. Called under
        ``self._lock``."""
        expired = [h for h, exp in self._seen_plan_hashes.items() if exp <= now]
        for h in expired:
            del self._seen_plan_hashes[h]
        # Don't aggressively prune caller_history — the check_caller_rate
        # path already prunes on every call. Same for nonce_high_water
        # which we keep indefinitely for monotonic safety.

    # ── Observability ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Snapshot the current state — used by /health and the alarm script."""
        with self._lock:
            return {
                "plan_hashes_tracked": len(self._seen_plan_hashes),
                "unique_callers_in_window": len(self._caller_history),
                "signers_with_nonce": len(self._nonce_high_water),
                "daily_gas_used_wei": self._daily_gas.gas_used_wei,
                "daily_gas_used_eth": round(self._daily_gas.gas_used_wei / 10**18, 6),
                "daily_gas_cap_eth": round(self.daily_gas_cap_wei / 10**18, 6),
            }
