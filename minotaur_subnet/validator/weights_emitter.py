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
    """

    def __init__(
        self,
        wallet: Any,
        subtensor: Any,
        netuid: int = 112,
        version_key: int = 6,
        max_attempts: int = 10,
        block_time: float = 12.0,
        metagraph_sync: Any = None,
    ) -> None:
        self.wallet = wallet
        self.subtensor = subtensor
        self.netuid = netuid
        self.version_key = version_key
        self.max_attempts = max_attempts
        self.block_time = block_time
        self.metagraph_sync = metagraph_sync

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

        # BT-8: Only the leader should emit weights
        if self.metagraph_sync is not None and not self.metagraph_sync.is_leader:
            logger.debug("Skipping weight emission — not leader")
            return False

        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._emit_blocking, weights_mapping,
            )
        except Exception as exc:
            logger.error("Weight emission failed: %s", exc)
            return False

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
