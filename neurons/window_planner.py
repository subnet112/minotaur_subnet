"""Epoch-aligned window planner for events-based validator operation.

Computes [from_ts, to_ts) for the previous on-chain epoch using Subtensor
parameters (Tempo) and block timestamps from the chain, so all validators
slice the same window deterministically.
"""
from __future__ import annotations

import logging
import os
import datetime as dt
from typing import Tuple, Optional

from async_substrate_interface.sync_substrate import SubstrateInterface

from .exceptions import WindowPlannerError

logger = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class WindowPlanner:
    def __init__(self, substrate: SubstrateInterface, netuid: int, *, finney_substrate: SubstrateInterface | None = None):
        self.substrate = substrate
        self.netuid = int(netuid)
        self._finney_substrate = finney_substrate
        self.max_timestamp_retries = int(os.getenv("WINDOW_PLANNER_MAX_RETRIES", "3"))

    def _get_tempo(self) -> int:
        try:
            q = self.substrate.query("SubtensorModule", "Tempo", [self.netuid])
            tempo = int(q.value) if q is not None else 360
            return max(1, tempo)
        except Exception as exc:
            logger.warning("Failed to query Tempo, using default 360: %s", exc)
            return 360

    def get_current_tempo(self) -> int:
        """Public accessor for the network tempo."""
        return self._get_tempo()

    def _get_current_block(self) -> int:
        substrate = self._finney_substrate or self.substrate
        try:
            header = substrate.get_block_header(block_hash=None)
            # header can be nested dict {'header': {...}} or direct dict/object
            if isinstance(header, dict):
                if "header" in header:
                    num = header["header"].get("number")
                else:
                    num = header.get("number")
            else:
                num = getattr(header, "number", None)
            if isinstance(num, str):
                return int(num, 16)
            return int(num)
        except Exception as exc:
            logger.warning("Failed to get current block, falling back to 0: %s", exc)
            return 0

    def _block_hash(self, block_number: int, substrate: SubstrateInterface | None = None) -> Optional[str]:
        sub = substrate or self.substrate
        try:
            return sub.get_block_hash(block_number)
        except Exception as exc:
            logger.warning("Failed to get block hash for block %d: %s", block_number, exc)
            return None

    def _block_timestamp_iso(self, block_hash: Optional[str], substrate: SubstrateInterface | None = None) -> Optional[str]:
        if not block_hash:
            return None
        sub = substrate or self.substrate
        try:
            tsq = sub.query("Timestamp", "Now", block_hash=block_hash)
            # Timestamp pallet stores milliseconds since epoch
            millis = int(tsq.value) if tsq is not None else None
            if millis is None:
                return None
            dt_ = dt.datetime.fromtimestamp(millis / 1000.0, tz=dt.timezone.utc)
            return _to_iso(dt_)
        except Exception as exc:
            logger.warning("Failed to get timestamp for block hash %s: %s", block_hash, exc)
            return None

    def _resolve_block_timestamp(self, block_number: int) -> Optional[str]:
        attempts = max(1, self.max_timestamp_retries)
        # Try archive first (preferred for historical data)
        for _ in range(attempts):
            block_hash = self._block_hash(block_number)
            ts = self._block_timestamp_iso(block_hash)
            if ts:
                return ts
        # Fall back to finney for recent blocks the archive hasn't indexed yet
        if self._finney_substrate:
            for _ in range(attempts):
                block_hash = self._block_hash(block_number, substrate=self._finney_substrate)
                ts = self._block_timestamp_iso(block_hash, substrate=self._finney_substrate)
                if ts:
                    return ts
        return None

    def _estimate_block_timestamp(self, block_number: int, cur_block: int, block_time_seconds: float = 12.0) -> str:
        """Estimate a block's timestamp from the current time and block distance.

        Bittensor produces blocks at a ~12-second cadence.  When neither archive
        nor finney can return an on-chain timestamp (archive too far behind,
        finney state pruned), we estimate using:
            now - (cur_block - block_number) * block_time
        """
        blocks_ago = max(0, cur_block - block_number)
        estimated = _utcnow() - dt.timedelta(seconds=blocks_ago * block_time_seconds)
        return _to_iso(estimated)

    def previous_epoch_window(
        self,
        last_processed_epoch: Optional[int],
        finalization_buffer_blocks: int = 5,
    ) -> Optional[Tuple[int, str, str]]:
        """Return (epoch_index, from_ts_iso, to_ts_iso) for the previous epoch.

        Epoch index is derived as floor(current_block / tempo). The previous
        epoch spans blocks [start, end] = [(epoch-1)*tempo, epoch*tempo - 1].
        Timestamps are taken from the Timestamp pallet at the start and end blocks.
        If on-chain timestamps are unavailable (e.g. archive behind, finney pruned),
        timestamps are estimated from block distance.
        """
        tempo = self._get_tempo()
        cur_block = self._get_current_block()
        cur_epoch = cur_block // tempo
        prev_epoch = max(0, cur_epoch - 1)

        if cur_epoch <= 0:
            return None
        if last_processed_epoch is not None and prev_epoch <= last_processed_epoch:
            return None

        end_block_inclusive = max(0, cur_epoch * tempo - 1)
        buffer_blocks = max(0, int(finalization_buffer_blocks))
        if cur_block - end_block_inclusive < buffer_blocks:
            return None

        start_block = max(0, prev_epoch * tempo)

        from_ts = self._resolve_block_timestamp(start_block)
        to_ts = self._resolve_block_timestamp(end_block_inclusive)

        # Fallback: estimate from block distance when on-chain queries fail
        if not from_ts or not to_ts:
            if not from_ts:
                from_ts = self._estimate_block_timestamp(start_block, cur_block)
            if not to_ts:
                to_ts = self._estimate_block_timestamp(end_block_inclusive, cur_block)
            logger.warning(
                "Using estimated timestamps for epoch %d (blocks %d-%d): archive/finney state unavailable",
                prev_epoch, start_block, end_block_inclusive,
            )

        return int(prev_epoch), str(from_ts), str(to_ts)



