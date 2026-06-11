"""Solver round epoch clock helpers.

Provides a stable, monotonic epoch source for the API-side solver round
coordinator. The clock prefers native subnet epoch indices derived from
metagraph/subtensor tempo data when available, otherwise it falls back to
``SOLVER_ROUND_EPOCH_BLOCKS`` or wall-clock epochs derived from the fixed
``EPOCH_SECONDS`` protocol constant.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

# Consensus-critical protocol constant — NOT operator-configurable.
#
# The round-anchored fork pin derives its anchor timestamp as
# ``anchor_epoch * EPOCH_SECONDS`` (see consensus.round_anchor), and the round
# coordinator buckets wall-clock time into epochs of this width. Every
# validator MUST use the identical value or the per-round benchmark pack hashes
# diverge → PACK_HASH_MISMATCH. This was previously the ``SOLVER_ROUND_EPOCH_SECONDS``
# env var, but that was a debug/test knob: letting operators pick it silently
# breaks fleet consensus parity. Fixed at 60s and removed from config
# (2026-06-11). Tests may still override via the constructor.
EPOCH_SECONDS = 60


def _parse_positive_int(raw: str | None, *, default: int | None) -> int | None:
    """Parse a positive integer from config, falling back on invalid input."""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


@dataclass(frozen=True)
class SolverRoundEpochClock:
    """Resolve the current solver-round epoch from blocks or wall-clock time."""

    epoch_seconds: int = EPOCH_SECONDS
    epoch_blocks: int | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "SolverRoundEpochClock":
        """Build an epoch clock from environment variables.

        ``epoch_seconds`` is intentionally NOT read from the environment: it is
        the fixed ``EPOCH_SECONDS`` protocol constant (consensus-critical, see
        above). Only the block-mode override remains operator-tunable.
        """
        environ = env or os.environ
        epoch_blocks = _parse_positive_int(
            environ.get("SOLVER_ROUND_EPOCH_BLOCKS"),
            default=None,
        )
        return cls(epoch_seconds=EPOCH_SECONDS, epoch_blocks=epoch_blocks)

    def uses_block_mode(self, *, block_number: int | None = None) -> bool:
        """Return whether the clock can derive epochs from the chain block."""
        return (
            self.epoch_blocks is not None
            and self.epoch_blocks > 0
            and block_number is not None
            and block_number >= 0
        )

    def resolved_epoch_blocks(
        self,
        *,
        native_epoch_length_blocks: int | None = None,
    ) -> int | None:
        """Return the active block-based epoch length, if any."""
        if native_epoch_length_blocks is not None and native_epoch_length_blocks > 0:
            return int(native_epoch_length_blocks)
        if self.epoch_blocks is not None and self.epoch_blocks > 0:
            return int(self.epoch_blocks)
        return None

    def current_epoch(
        self,
        *,
        block_number: int | None = None,
        native_epoch: int | None = None,
        native_epoch_length_blocks: int | None = None,
        now: float | None = None,
    ) -> int:
        """Return the current epoch as a stable absolute integer."""
        if native_epoch is not None:
            return max(0, int(native_epoch))
        resolved_epoch_blocks = self.resolved_epoch_blocks(
            native_epoch_length_blocks=native_epoch_length_blocks,
        )
        if (
            resolved_epoch_blocks is not None
            and block_number is not None
            and block_number >= 0
        ):
            return max(0, int(block_number) // int(resolved_epoch_blocks))
        ts = time.time() if now is None else now
        return max(0, int(ts // max(1, int(self.epoch_seconds))))

    def health_snapshot(
        self,
        *,
        block_number: int | None = None,
        native_epoch: int | None = None,
        native_epoch_length_blocks: int | None = None,
        native_blocks_since_last_step: int | None = None,
    ) -> dict[str, Any]:
        """Return a small health/debug snapshot for the active clock mode."""
        resolved_epoch_blocks = self.resolved_epoch_blocks(
            native_epoch_length_blocks=native_epoch_length_blocks,
        )
        if native_epoch is not None:
            mode = "native_tempo"
        elif resolved_epoch_blocks is not None and block_number is not None and block_number >= 0:
            mode = "block"
        else:
            mode = "time"
        return {
            "mode": mode,
            "epoch_seconds": self.epoch_seconds,
            "configured_epoch_blocks": self.epoch_blocks,
            "resolved_epoch_blocks": resolved_epoch_blocks,
            "native_epoch": native_epoch,
            "native_blocks_since_last_step": native_blocks_since_last_step,
            "current_epoch": self.current_epoch(
                block_number=block_number,
                native_epoch=native_epoch,
                native_epoch_length_blocks=native_epoch_length_blocks,
            ),
        }
