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
        """Blocking weight submission (runs in executor)."""
        import numpy as np

        metagraph = self.subtensor.metagraph(netuid=self.netuid)

        # Map hotkeys to UIDs
        hotkey_to_uid: dict[str, int] = {}
        for uid in range(metagraph.n.item()):
            hotkey_to_uid[metagraph.hotkeys[uid]] = uid

        uids: list[int] = []
        weights: list[float] = []
        for hotkey, weight in weights_mapping.items():
            uid = hotkey_to_uid.get(hotkey)
            if uid is not None:
                uids.append(uid)
                weights.append(weight)
            else:
                logger.warning("Hotkey %s not found in metagraph, skipping", hotkey[:16])

        if not uids:
            logger.warning("No valid UIDs found for weight emission")
            return False

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
