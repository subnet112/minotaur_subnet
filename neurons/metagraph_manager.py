"""Metagraph manager for validator operations.

Synchronizes subnet state, validates validator permit, and exposes
hotkey→uid mappings for active miners/validators.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import bittensor as bt


@dataclass
class MetagraphSnapshot:
    uid_for_hotkey: Dict[str, int]
    size: int
    validator_permit: bool
    validator_uid: Optional[int]


class MetagraphManager:
    def __init__(self, subtensor: bt.Subtensor, wallet: bt.Wallet, netuid: int, logger):
        self.subtensor = subtensor
        self.wallet = wallet
        self.netuid = int(netuid)
        self.logger = logger
        self._metagraph: Optional[bt.Metagraph] = None
        self._last_block: Optional[int] = None

    def refresh(self, force: bool = False) -> Optional[MetagraphSnapshot]:
        try:
            block = self.subtensor.get_current_block()
        except Exception:
            block = None

        if not force and self._last_block is not None and block is not None and block - self._last_block < 5:
            # Skip frequent refreshes; return cached snapshot if available
            if self._metagraph is not None:
                return self._build_snapshot(self._metagraph)

        try:
            if self._metagraph is None:
                self._metagraph = bt.Metagraph(netuid=self.netuid, network=self.subtensor.network, sync=False)
            self._metagraph.sync(subtensor=self.subtensor, lite=True)
            self._last_block = block
            return self._build_snapshot(self._metagraph)
        except Exception as e:
            self.logger.error(f"Failed to sync metagraph: {e}")
            return None

    async def sync_metagraph(self) -> Optional[MetagraphSnapshot]:
        """Async-friendly metagraph refresh."""
        return self.refresh(force=True)

    async def get_current_metagraph(self) -> Optional[MetagraphSnapshot]:
        """Async-friendly accessor for the latest metagraph snapshot."""
        return self.refresh(force=False)

    def _build_snapshot(self, m: bt.metagraph) -> Optional[MetagraphSnapshot]:
        if not hasattr(m, "hotkeys") or not hasattr(m, "uids"):
            self.logger.error("Metagraph missing hotkeys/uids attributes")
            return None

        uid_for_hotkey: Dict[str, int] = {}
        try:
            for hotkey, uid in zip(m.hotkeys, m.uids):
                uid_for_hotkey[str(hotkey)] = int(uid)
        except Exception as e:
            self.logger.error(f"Failed to build hotkey→UID map: {e}")
            return None

        validator_hotkey = getattr(self.wallet, "hotkey", None)
        validator_ss58 = getattr(validator_hotkey, "ss58_address", None)
        validator_uid = None
        validator_permit = False

        if validator_ss58 is not None:
            try:
                validator_uid = self.subtensor.get_uid_for_hotkey_on_subnet(validator_ss58, self.netuid)
                validator_permit = bool(m.validator_permit[validator_uid]) if validator_uid is not None else False
            except Exception:
                validator_permit = False

        if not validator_permit:
            self.logger.error("Validator does not have permit or UID is missing – skipping weight emission")

        return MetagraphSnapshot(
            uid_for_hotkey=uid_for_hotkey,
            size=len(uid_for_hotkey),
            validator_permit=validator_permit,
            validator_uid=validator_uid,
        )


