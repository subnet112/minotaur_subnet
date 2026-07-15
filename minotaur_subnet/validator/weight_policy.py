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
        # Throttle the "empty weights" warning so a misconfigured operator's
        # log isn't spammed every 5 sec of the epoch_loop.
        self._last_empty_warn_at: float = 0.0

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
        return self.close_epoch_now(champion_miner_id)

    def close_epoch_now(self, champion_miner_id: str | None) -> dict[str, float] | None:
        """``maybe_emit`` without the wall-clock gate.

        Used by the tempo-aligned scheduler (``TempoEmitGate``), where the
        CHAIN clock — the pre-boundary commit window — decides timing and this
        local ``epoch_seconds`` clock must not add a second veto. Still resets
        ``_epoch_start`` so a later fallback to the wall-clock cadence (chain
        state unavailable) spaces itself from the last tempo-aligned emit
        instead of firing immediately.
        """
        weights = build_bootstrap_or_champion_weights(
            champion_miner_id,
            owner_hotkey=self.owner_hotkey,
        )

        # Empty weights = no champion AND no resolvable owner_hotkey. Returning
        # here WITHOUT advancing _epoch_start is the load-bearing piece:
        # previously the clock was reset to now even on empty, so an operator
        # whose owner_hotkey hadn't been resolved at startup would wait
        # another full epoch_seconds (default 20 min) for the next attempt —
        # if the chain query later succeeded, the fix wouldn't take effect
        # for another 20 min. Now the loop retries every tick until weights
        # are non-empty, which is what an operator self-healing from a
        # misconfig actually wants. (Bug surfaced 2026-05-26 when third-party
        # validators on canonical compose lacked SUBNET_OWNER_HOTKEY env and
        # entered a permanent silent-no-emit loop.)
        if not weights:
            if time.time() - self._last_empty_warn_at > 300:
                logger.warning(
                    "maybe_emit returned empty weights: no real champion AND "
                    "no resolvable owner_hotkey. Validator will NOT emit until "
                    "either is set. Check that SUBNET_OWNER_HOTKEY is set OR "
                    "that the daemon's bittensor subtensor connection can "
                    "reach SubnetOwnerHotkey storage at startup."
                )
                self._last_empty_warn_at = time.time()
            return None

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
