"""Champion weight tracking and emission.

Moved from validator/main.py — tracks per-epoch champion weights and
routes emissions to the active champion miner or burns to subnet owner.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minotaur_subnet.weight_policy import build_bootstrap_or_champion_weights

logger = logging.getLogger("minotaur_subnet.validator.weight_policy")


class ChampionWeights:
    """Champion/burn weight emission.

    Before a real miner-backed champion exists, 100% of emissions are routed to
    the subnet owner hotkey (burn behavior). Once a real miner champion exists,
    100% goes to that champion.
    """

    def __init__(
        self,
        epoch_seconds: int = 60,
        owner_hotkey: str | None = None,
    ) -> None:
        self.epoch_seconds = epoch_seconds
        self.owner_hotkey = (owner_hotkey or "").strip()
        self._epoch_start = time.time()
        self._history: list[dict[str, Any]] = []

    def seed_epoch_clock_from_last_emit(self, seconds_since_last_emit: float) -> None:
        """Backdate the local epoch clock so the next ``maybe_emit`` call
        reflects the AUTHORITATIVE on-chain last_update, not the process
        start time.

        Without this, every container restart silently delays the next
        weight emission by a full ``epoch_seconds`` window — even when
        the chain-side rate limit would have allowed an immediate emit.
        That made today's 4-restart cascade on third-party validators
        (Rizzo + others) silently skip their first post-fix emit window.

        Caller passes ``current_time - (current_block - last_update_block)
        * block_time``. We just set ``_epoch_start`` to that point in the
        past — if the difference is already >= epoch_seconds, the next
        ``maybe_emit`` call returns weights on the very first tick.

        Capped at ``max(epoch_seconds * 2, seconds_since_last_emit)`` from
        ``time.time()`` to avoid arithmetic underflow from absurd inputs.
        """
        # Negative values (clock skew) → treat as "just emitted, wait normally"
        seconds_since_last_emit = max(0.0, float(seconds_since_last_emit))
        self._epoch_start = time.time() - seconds_since_last_emit
        logger.info(
            "Seeded epoch clock: last on-chain emission %.1fs ago "
            "(epoch_seconds=%ds → next emit %s)",
            seconds_since_last_emit,
            self.epoch_seconds,
            "on first tick" if seconds_since_last_emit >= self.epoch_seconds
            else f"in ~{self.epoch_seconds - seconds_since_last_emit:.0f}s",
        )

    def maybe_emit(self, champion_miner_id: str | None) -> dict[str, float] | None:
        """Return champion weights if epoch has elapsed, else None."""
        if time.time() - self._epoch_start < self.epoch_seconds:
            return None

        weights = build_bootstrap_or_champion_weights(
            champion_miner_id,
            owner_hotkey=self.owner_hotkey,
        )

        self._history.append({
            "epoch_start": self._epoch_start,
            "epoch_end": time.time(),
            "champion": champion_miner_id,
            "weights": weights,
        })
        logger.info(
            "Epoch closed: champion=%s, weights=%s",
            champion_miner_id or "none",
            list(weights.keys()) or "none",
        )

        self._epoch_start = time.time()
        return weights

    def get_weights(self, champion_miner_id: str | None) -> dict[str, float]:
        """Current weights using bootstrap burn before a real miner champion."""
        return build_bootstrap_or_champion_weights(
            champion_miner_id,
            owner_hotkey=self.owner_hotkey,
        )

    def get_history(self) -> list[dict[str, Any]]:
        return list(self._history)
