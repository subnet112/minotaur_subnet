"""Epoch-aligned window planner for events-based validator operation.

Computes [from_ts, to_ts) for the previous on-chain epoch using Subtensor
parameters (Tempo) and block timestamps from the chain, so all validators
slice the same window deterministically.
"""
from __future__ import annotations

import os
import datetime as dt
from typing import Tuple, Optional

from async_substrate_interface.sync_substrate import SubstrateInterface

from .exceptions import WindowPlannerError


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class WindowPlanner:
    def __init__(self, substrate: SubstrateInterface, netuid: int):
        self.substrate = substrate
        self.netuid = int(netuid)
        self.max_timestamp_retries = int(os.getenv("WINDOW_PLANNER_MAX_RETRIES", "3"))

    def _get_tempo(self) -> int:
        try:
            q = self.substrate.query("SubtensorModule", "Tempo", [self.netuid])
            tempo = int(q.value) if q is not None else 360
            return max(1, tempo)
        except Exception:
            # Sensible default if query fails
            return 360

    def get_current_tempo(self) -> int:
        """Public accessor for the network tempo."""
        return self._get_tempo()

    def _get_current_block(self) -> int:
        try:
            header = self.substrate.get_block_header(block_hash=None)
            # header.number can be hex string or int depending on library version
            num = header.get("number") if isinstance(header, dict) else getattr(header, "number", None)
            if isinstance(num, str):
                return int(num, 16)
            return int(num)
        except Exception:
            # Fallback to 0
            return 0

    def _block_hash(self, block_number: int) -> Optional[str]:
        try:
            return self.substrate.get_block_hash(block_number)
        except Exception:
            return None

    def _block_timestamp_iso(self, block_hash: Optional[str]) -> Optional[str]:
        if not block_hash:
            return None
        try:
            tsq = self.substrate.query("Timestamp", "Now", block_hash=block_hash)
            # Timestamp pallet stores milliseconds since epoch
            millis = int(tsq.value) if tsq is not None else None
            if millis is None:
                return None
            dt_ = dt.datetime.fromtimestamp(millis / 1000.0, tz=dt.timezone.utc)
            return _to_iso(dt_)
        except Exception:
            return None

    def _resolve_block_timestamp(self, block_number: int) -> Optional[str]:
        attempts = max(1, self.max_timestamp_retries)
        for _ in range(attempts):
            block_hash = self._block_hash(block_number)
            ts = self._block_timestamp_iso(block_hash)
            if ts:
                return ts
        return None

    def previous_epoch_window(
        self,
        last_processed_epoch: Optional[int],
        finalization_buffer_blocks: int = 5,
    ) -> Optional[Tuple[int, str, str]]:
        """Return (epoch_index, from_ts_iso, to_ts_iso) for the previous epoch.

        Epoch index is derived as floor(current_block / tempo). The previous
        epoch spans blocks [start, end] = [(epoch-1)*tempo, epoch*tempo - 1].
        Timestamps are taken from the Timestamp pallet at the start and end blocks.
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

        # Fallbacks: if timestamps unavailable, use now-based approximations
        if not from_ts or not to_ts:
            raise WindowPlannerError(
                f"Failed to resolve timestamps for epoch {prev_epoch} blocks {start_block}-{end_block_inclusive}"
            )

        return int(prev_epoch), str(from_ts), str(to_ts)



