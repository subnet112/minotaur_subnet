"""Relayer pre-submit safeguards.

A bundle of cheap checks the relayer service runs BEFORE handing off to
``EvmRelayer.submit_plan``, all designed to either (a) reject invalid
requests without spending gas, or (b) bound the gas damage a malicious
caller (including a malicious validator-of-record) can do.

Layers:

1. **Deadline check** — reject plans whose ``deadline`` has passed.
   Catches replay of expired plans cheaply.

2. **Submission dedup** — in-memory reservation keyed by
   ``(order_id, execution_count, plan_hash)`` with a TTL clamped to a
   broadcast-window scale regardless of the plan's claimed deadline.
   Each logical submission (one fill round of one order) submittable
   exactly once while in flight — this is what stops two duplicate
   submissions racing past the pre-broadcast dry-run and double-executing
   a perpetual order. Restart-vulnerable; the on-chain nonce burn /
   cooldown is the authoritative replay defense underneath this.
   Cross-order replay needs no dedup at all: validator approvals sign
   ``PlanApproval(orderId, planHash, score)``, so a plan approved for
   one order can never be submitted under another.

   The key/TTL shape matters — three production incidents from the
   original ``plan_hash``-only version (2026-07-15): a submission that
   failed on the gas-balance floor permanently burned its hash; the
   champion's sentinel plan deadline (~year 2286) made every entry
   permanent; and a second order with byte-identical champion calldata
   (deterministic solver, recurring DCA orders) collided with the
   first. Reservations are therefore released on pre-broadcast failure
   (nothing happened on-chain), the TTL is clamped, and the key is
   scoped per fill-round.

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
DEFAULT_DEDUP_GRACE_SECONDS = 600  # how long after deadline to keep a reservation
# Hard ceiling on how far into the future a dedup reservation may live,
# no matter what deadline the plan claims. Miner-authored plans carry
# sentinel deadlines (~10^10); trusting them made reservations permanent.
# The dedup only needs to cover the in-flight broadcast/confirmation
# window — after that, on-chain nonce burn / cooldown takes over.
DEFAULT_DEDUP_MAX_TTL_SECONDS = 900  # 15 min
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
    dedup_max_ttl_seconds: int = DEFAULT_DEDUP_MAX_TTL_SECONDS

    # state
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # dedup_key ("order_id:execution_count:plan_hash") -> evict_at (unix seconds)
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
            dedup_max_ttl_seconds=_int_env("RELAYER_DEDUP_MAX_TTL_SECONDS", DEFAULT_DEDUP_MAX_TTL_SECONDS),
        )

    # ── Public checks. Each returns (ok: bool, error: str) ─────────────
    # On reject the relayer returns 400/409 with the error text. None
    # of these change state on rejection.

    def check_deadline(self, plan_deadline: int, now: int | None = None) -> tuple[bool, str]:
        now = int(now if now is not None else time.time())
        if int(plan_deadline) <= now:
            return False, f"plan deadline expired ({plan_deadline} <= now {now})"
        return True, ""

    def check_plan_hash_unseen(self, dedup_key: str, plan_deadline: int) -> tuple[bool, str]:
        """Check + reserve a submission slot. Idempotent at the key level:
        same key twice returns False the second time, and the cache evicts
        when min(deadline, now + max_ttl) + grace elapses.

        ``dedup_key`` is the caller's identity for one logical submission —
        ``handle_submit_plan`` uses ``order_id:execution_count:plan_hash``
        so distinct orders (and successive perpetual fill rounds) never
        collide even when the champion emits byte-identical plans. The
        deadline is CLAMPED to ``dedup_max_ttl_seconds`` because plans
        carry miner-authored sentinel deadlines; the reservation only
        needs to outlive the broadcast/confirmation window.

        A reservation made here must be released via ``release_plan_hash``
        if the submission later fails before broadcast — otherwise a
        transient failure (gas floor, dry-run revert) blocks the retry.
        """
        now = int(time.time())
        evict_at = min(int(plan_deadline), now + self.dedup_max_ttl_seconds) + self.dedup_grace_seconds
        with self._lock:
            self._evict_expired(now)
            existing = self._seen_plan_hashes.get(dedup_key)
            if existing is not None and existing > now:
                return False, f"plan_hash already submitted (re-submittable after {existing - now}s)"
            self._seen_plan_hashes[dedup_key] = evict_at
            return True, ""

    def release_plan_hash(self, dedup_key: str) -> None:
        """Release a reservation made by ``check_plan_hash_unseen``.

        Called when the submission failed WITHOUT broadcasting a tx
        (gas-balance floor, pre-broadcast dry-run revert, estimate_gas
        error) — nothing happened on-chain, so there is nothing for the
        dedup to protect and the caller must be free to retry. Do NOT
        call this when a tx was broadcast (success or mined-revert), and
        do NOT call it on ambiguous crashes — the clamped TTL bounds the
        damage of a stale reservation to minutes either way. No-op for
        unknown keys.
        """
        with self._lock:
            self._seen_plan_hashes.pop(dedup_key, None)

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
