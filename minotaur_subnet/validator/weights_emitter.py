"""WeightsEmitter — submit per-miner weights to the Bittensor chain.

Provides the ``emit_async(mapping)`` interface that the epoch loop calls
after closing an epoch. Maps hotkey→weight to UID→weight arrays and
calls ``subtensor.set_weights()``.

Uses bittensor 10.x API with commit-reveal parameters tuned for both
production and local testnet (fast blocks).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WeightsEmitter:
    """Submits normalized weights to the Bittensor chain.

    Args:
        wallet: bittensor Wallet instance for signing.
        subtensor: bittensor Subtensor client.
        netuid: The subnet network UID (default 112 for production).
        version_key: Version key for weight commits.
        max_attempts: Max attempts for commit-reveal set_weights.
        block_time: Expected block time (0.25 for local testnet, 12 for mainnet).
        subtensor_url: Network/URL used to REBUILD the client after a stale-
            websocket failure (self-healing reconnect). Without it the emitter
            reuses one long-lived Subtensor whose ws dies when an operator rotates
            the RPC, and every set_weights then fails on the dead socket until a
            daemon restart. None disables reconnect (keeps the legacy behaviour).
    """

    def __init__(
        self,
        wallet: Any,
        subtensor: Any,
        netuid: int = 112,
        version_key: int = 6,
        max_attempts: int = 10,
        block_time: float = 12.0,
        subtensor_url: str | None = None,
    ) -> None:
        self.wallet = wallet
        self.subtensor = subtensor
        self.netuid = netuid
        self.version_key = version_key
        self.max_attempts = max_attempts
        self.block_time = block_time
        self._subtensor_url = subtensor_url

    async def emit_async(self, weights_mapping: dict[str, float]) -> bool:
        """Submit weights to the chain asynchronously.

        Args:
            weights_mapping: Dict of hotkey_ss58 → normalized weight (0.0-1.0).

        Returns:
            True if weights were set successfully.
        """
        if not weights_mapping:
            logger.info("No weights to emit (empty mapping)")
            return False

        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._emit_blocking, weights_mapping,
            )
        except Exception as exc:
            logger.error("Weight emission failed: %s", exc)
            # A RAISED emit (vs a returned False from a chain-level rejection) is
            # almost always a dead/stale subtensor websocket — most commonly the
            # operator rotated the RPC, leaving this emitter's long-lived client
            # pinned to a closed socket. Rebuild it so the NEXT emit reconnects on
            # its own instead of failing here every epoch until a daemon restart.
            # Run the (blocking) ws handshake off the event loop.
            await asyncio.get_event_loop().run_in_executor(
                None, self._reconnect_subtensor,
            )
            return False

    def _reconnect_subtensor(self) -> None:
        """Rebuild the Subtensor client against ``subtensor_url`` so a stale
        websocket self-heals on the next emit. Best-effort and blocking (called in
        an executor): a still-down RPC just fails here too and we retry next epoch.
        No-op when no URL was configured (legacy behaviour preserved)."""
        if not self._subtensor_url:
            return
        try:
            import bittensor as bt

            self.subtensor = bt.Subtensor(network=self._subtensor_url)
            logger.info(
                "Reconnected emitter subtensor to %s after a failed emit "
                "(stale-websocket recovery)",
                self._subtensor_url,
            )
        except Exception as exc:
            logger.warning(
                "Emitter subtensor reconnect to %s failed (will retry next emit): %s",
                self._subtensor_url, exc,
            )

    def _emit_blocking(self, weights_mapping: dict[str, float]) -> bool:
        """Blocking weight submission (runs in executor).

        Hotkeys absent from the live metagraph (e.g. a champion that
        deregistered between certification and this epoch) have their
        weight rerouted to the subnet owner UID. This keeps the burn-to-
        owner fallback intact under all "champion not currently a miner"
        states — never-registered, deregistered, immunity-expired, hotkey-
        rotated — so the validator never silently emits empty weights.
        """
        import numpy as np

        from minotaur_subnet.weight_policy import get_subnet_owner_hotkey

        metagraph = self.subtensor.metagraph(netuid=self.netuid)

        # Map hotkeys to UIDs
        hotkey_to_uid: dict[str, int] = {}
        for uid in range(metagraph.n.item()):
            hotkey_to_uid[metagraph.hotkeys[uid]] = uid

        owner_hotkey = get_subnet_owner_hotkey()
        owner_uid = hotkey_to_uid.get(owner_hotkey) if owner_hotkey else None

        uid_weight: dict[int, float] = {}
        for hotkey, weight in weights_mapping.items():
            uid = hotkey_to_uid.get(hotkey)
            if uid is not None:
                uid_weight[uid] = uid_weight.get(uid, 0.0) + weight
            elif owner_uid is not None:
                logger.warning(
                    "Hotkey %s not in metagraph — routing %.4f weight to owner UID %d (%s)",
                    hotkey[:16], weight, owner_uid, owner_hotkey[:16],
                )
                uid_weight[owner_uid] = uid_weight.get(owner_uid, 0.0) + weight
            else:
                logger.error(
                    "Hotkey %s not in metagraph and no SUBNET_OWNER_HOTKEY fallback configured — dropping %.4f weight",
                    hotkey[:16], weight,
                )

        if not uid_weight:
            logger.warning("No valid UIDs found for weight emission")
            return False

        uids = list(uid_weight.keys())
        weights = list(uid_weight.values())

        # Normalize weights to sum to 1.0
        total = sum(weights)
        if total > 0:
            weights = [w / total for w in weights]
        else:
            weights = [1.0 / len(weights)] * len(weights)

        uid_array = np.array(uids, dtype=np.int64)
        weight_array = np.array(weights, dtype=np.float32)

        logger.info(
            "Emitting weights: %d UIDs, max=%.4f, min=%.4f",
            len(uids),
            max(weights),
            min(weights),
        )

        result = self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.netuid,
            uids=uid_array,
            weights=weight_array,
            version_key=self.version_key,
            max_attempts=self.max_attempts,
            block_time=self.block_time,
        )

        success = result.success if hasattr(result, "success") else bool(result)
        if success:
            logger.info("Weights emitted successfully for %d UIDs", len(uids))
        else:
            logger.warning("Weight emission returned failure: %s", result)

        return success
