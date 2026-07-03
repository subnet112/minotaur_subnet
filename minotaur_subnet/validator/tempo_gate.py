"""TempoEmitGate — align weight commits with the chain's tempo epochs.

Subnet 112 runs commit-reveal weights (tempo=360 blocks, reveal after the
next epoch step). The chain keeps ONE pending commit per validator per
tempo epoch: committing twice inside the same epoch silently discards the
first commit. The daemon's legacy wall-clock cadence (a commit every
~epoch_seconds, plus one whenever a solver round activated) made 2-3
commits per tempo epoch, so most commits never revealed — and a champion
whose reign fell between the two surviving commits earned ZERO emission
despite a successful ``set_weights`` (observed live 2026-07-03: a 48-min
champion was committed at 00:52Z, overwritten at 01:17Z, never revealed).

This gate replaces the wall clock with the chain clock: commit exactly
once per tempo epoch, inside a short window just before the epoch step,
so our commit is the LAST of its epoch and always reveals — carrying the
freshest possible champion snapshot.

Chain mechanics (verified on finney, netuid 112, blocks 8536990-8537050):

- ``SubtensorModule.Tempo(netuid)`` = 360.
- ``SubtensorModule.BlocksSinceLastStep(netuid)`` counts 0..tempo and
  resets to 0 on the step block → epoch period is ``tempo + 1`` blocks
  and ``blocks_until_step = tempo - blocks_since_last_step``.
- A commit made anywhere in epoch N becomes the revealed weights at the
  step that closes epoch N (reveal_period_epochs=1); a later commit in
  the same epoch replaces it.

The gate is advisory: ``should_emit_now`` returns ``None`` whenever the
chain state can't be established (query failure before any sync, tempo=0
on an odd testnet), and the caller falls back to the legacy wall-clock
cadence. Between successful syncs it extrapolates the block height from
the local clock — finney's 12s block time drifts far less per resync
interval than the emit window is wide.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Start trying to commit this many blocks before the epoch step (~4 min at
# 12s blocks). Wide enough for a ~30s set_weights round-trip plus retries
# and extrapolation drift; narrow enough that no later commit of ours can
# land in the same epoch (the next attempt targets the NEXT boundary).
DEFAULT_LEAD_BLOCKS = 20

# Refresh (block, tempo, blocks_since_last_step) from chain at most this
# often; extrapolate from the local monotonic clock in between.
DEFAULT_RESYNC_SECONDS = 300.0

# After a failed chain sync, wait this long before querying again so a dead
# websocket doesn't add a blocking timeout to every 5s epoch-loop tick.
SYNC_RETRY_BACKOFF_SECONDS = 30.0


class TempoEmitGate:
    """Decides WHEN the epoch loop may commit weights: once per tempo epoch,
    in the ``lead_blocks`` window before the epoch step.

    Args:
        get_subtensor: zero-arg callable returning the live Subtensor client.
            A callable (not the client itself) so the gate follows the
            WeightsEmitter's stale-websocket reconnects instead of pinning
            the original, possibly-dead client.
        netuid: subnet to track.
        block_time: seconds per block (12.0 mainnet, 0.25 local testnet).
        lead_blocks: window size before the step in which to commit.
        resync_seconds: how often to re-anchor the block estimate on chain.
        monotonic: clock override for tests.
    """

    def __init__(
        self,
        get_subtensor: Callable[[], Any],
        netuid: int,
        block_time: float = 12.0,
        lead_blocks: int = DEFAULT_LEAD_BLOCKS,
        resync_seconds: float = DEFAULT_RESYNC_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._get_subtensor = get_subtensor
        self.netuid = netuid
        self.block_time = max(0.01, float(block_time))
        self.lead_blocks = max(1, int(lead_blocks))
        self.resync_seconds = float(resync_seconds)
        self._monotonic = monotonic

        self._tempo: int | None = None
        self._synced_block: int | None = None
        self._synced_at: float | None = None
        self._next_boundary_block: int | None = None
        self._committed_boundary: int | None = None
        self._last_sync_attempt: float | None = None

    # ── chain sync ───────────────────────────────────────────────────────

    def _sync_blocking(self) -> tuple[int, int, int]:
        """Query (current_block, tempo, blocks_since_last_step). Blocking —
        run in an executor."""
        sub = self._get_subtensor()
        block = int(sub.get_current_block())
        tempo = int(
            sub.substrate.query("SubtensorModule", "Tempo", [self.netuid]).value
        )
        since = int(
            sub.substrate.query(
                "SubtensorModule", "BlocksSinceLastStep", [self.netuid]
            ).value
        )
        return block, tempo, since

    async def _maybe_resync(self) -> None:
        now = self._monotonic()
        fresh = (
            self._synced_at is not None
            and (now - self._synced_at) <= self.resync_seconds
        )
        if fresh:
            return
        backed_off = (
            self._last_sync_attempt is not None
            and (now - self._last_sync_attempt) < SYNC_RETRY_BACKOFF_SECONDS
        )
        if backed_off:
            return
        self._last_sync_attempt = now
        try:
            block, tempo, since = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_blocking,
            )
        except Exception as exc:
            # Keep extrapolating from the previous sync (if any); the backoff
            # above keeps a dead RPC from stalling every tick.
            logger.warning(
                "Tempo gate chain sync failed (%s) — %s",
                exc,
                "falling back to wall-clock cadence" if self._tempo is None
                else "extrapolating from previous sync",
            )
            return
        if tempo <= 0:
            # Degenerate tempo (some local testnets) — signal "unknown" so the
            # caller uses the legacy cadence rather than dividing by zero here.
            self._tempo = None
            return
        self._tempo = tempo
        self._synced_block = block
        self._synced_at = self._monotonic()
        self._next_boundary_block = block + max(0, tempo - since)

    # ── window decision ──────────────────────────────────────────────────

    def _estimated_block(self) -> int:
        assert self._synced_block is not None and self._synced_at is not None
        elapsed = max(0.0, self._monotonic() - self._synced_at)
        return self._synced_block + int(elapsed / self.block_time)

    async def should_emit_now(self) -> bool | None:
        """Gate decision for this tick.

        Returns:
            True — inside the pre-step window and no commit made for this
                boundary yet: emit now.
            False — chain state known, but outside the window (or this
                boundary is already committed): do nothing this tick.
            None — chain state unavailable: caller should fall back to the
                legacy wall-clock cadence.
        """
        await self._maybe_resync()
        if self._tempo is None or self._next_boundary_block is None:
            return None

        est = self._estimated_block()
        period = self._tempo + 1
        while est > self._next_boundary_block:
            self._next_boundary_block += period

        blocks_left = self._next_boundary_block - est
        if blocks_left > self.lead_blocks:
            return False
        # Already committed for this boundary? Compare with tolerance, not
        # equality: a resync re-derives the boundary from two chain queries
        # that may land a block or two apart, and the two get_current_block /
        # BlocksSinceLastStep reads can straddle a block. Distinct boundaries
        # are a full period apart, so half a period is an unambiguous cutoff.
        if (
            self._committed_boundary is not None
            and abs(self._next_boundary_block - self._committed_boundary)
            < period // 2
        ):
            return False
        return True

    def mark_committed(self) -> None:
        """Record a successful commit for the current boundary so the loop
        doesn't double-commit inside one window. Call ONLY after a successful
        emit — a failed emit must stay retryable on the next tick."""
        self._committed_boundary = self._next_boundary_block

    # ── observability ────────────────────────────────────────────────────

    def debug_state(self) -> dict[str, Any]:
        """Schedule state for /health."""
        state: dict[str, Any] = {
            "mode": "tempo",
            "active": self._tempo is not None,
            "tempo": self._tempo,
            "lead_blocks": self.lead_blocks,
            "next_boundary_block": self._next_boundary_block,
            "committed_boundary_block": self._committed_boundary,
        }
        if self._tempo is not None and self._synced_block is not None:
            est = self._estimated_block()
            state["estimated_block"] = est
            if self._next_boundary_block is not None:
                state["blocks_until_boundary"] = self._next_boundary_block - est
        return state
