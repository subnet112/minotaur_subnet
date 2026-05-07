"""Validator sync — keeps on-chain validator sets in sync with the metagraph.

Periodically checks the Bittensor metagraph for validator changes and
updates the AppIntentBase contract on each supported chain via the relayer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .chain_config import ChainDeployment

logger = logging.getLogger(__name__)


class ValidatorSync:
    """Syncs validator set from metagraph to on-chain contracts.

    Args:
        chains: Supported chain deployments.
        relayer: EvmRelayer instance for on-chain updates.
        subtensor_url: WebSocket URL for the subtensor node.
        netuid: Bittensor subnet UID.
        quorum_bps: Quorum basis points for the contract.
        max_validators: Maximum number of validators to sync.
        poll_interval: Seconds between sync checks.
    """

    def __init__(
        self,
        chains: dict[int, ChainDeployment] | None = None,
        relayer: Any = None,
        subtensor_url: str = "",
        netuid: int = 112,
        max_validators: int = 32,
        poll_interval: float = 300.0,
    ) -> None:
        self.chains = chains or {}
        self.relayer = relayer
        self.subtensor_url = subtensor_url
        self.netuid = netuid
        self.max_validators = max_validators
        self.poll_interval = poll_interval
        self._running = False
        self._last_validators: list[str] = []

    async def sync_loop(self) -> None:
        """Background loop that checks for validator changes."""
        self._running = True
        logger.info(
            "ValidatorSync started (netuid=%d, poll=%ds)",
            self.netuid, self.poll_interval,
        )
        while self._running:
            try:
                await self._sync_once()
            except Exception as exc:
                logger.error("ValidatorSync error: %s", exc, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _sync_once(self) -> None:
        """Check metagraph and update chains if validators changed."""
        validators = await self._get_metagraph_validators()
        if not validators:
            return

        if set(validators) == set(self._last_validators):
            return

        logger.info(
            "Validator set changed: %d → %d validators",
            len(self._last_validators), len(validators),
        )
        self._last_validators = validators
        await self._update_all_chains(validators)

    async def _get_metagraph_validators(self) -> list[str]:
        """Get validator EVM addresses from the Bittensor metagraph.

        Queries the subtensor for top validators by stake and maps
        hotkeys to Ethereum addresses via a registry or derivation.
        Falls back to the last known validator set if query fails.
        """
        if not self.subtensor_url:
            return self._last_validators

        try:
            import bittensor as bt
            sub = bt.Subtensor(network=self.subtensor_url)
            metagraph = sub.metagraph(netuid=self.netuid)

            validators = []
            neurons = sorted(
                metagraph.neurons,
                key=lambda n: n.stake,
                reverse=True,
            )
            for neuron in neurons[:self.max_validators]:
                if neuron.stake.tao <= 0:
                    continue
                eth_addr = self._hotkey_to_eth_address(neuron.hotkey)
                if eth_addr:
                    validators.append(eth_addr)

            return validators
        except Exception as exc:
            logger.warning(
                "Failed to query metagraph validators: %s", exc,
            )
            return self._last_validators

    def _hotkey_to_eth_address(self, hotkey: str) -> str | None:
        """Derive or look up an Ethereum address from a Bittensor hotkey.

        For MVP: uses keccak256(hotkey_bytes)[:20] as the derived address.
        Production should use a proper registry contract.
        """
        try:
            from eth_hash.auto import keccak
            hotkey_bytes = hotkey.encode() if isinstance(hotkey, str) else hotkey
            addr_bytes = keccak(hotkey_bytes)[-20:]
            return "0x" + addr_bytes.hex()
        except Exception:
            return None

    async def _update_all_chains(self, validators: list[str]) -> None:
        """Update validator set on all supported chains."""
        for chain_id, config in self.chains.items():
            if not config.validator_registry_address:
                continue
            try:
                await self._update_chain(chain_id, config, validators)
                logger.info(
                    "Updated validators on chain %d (%s)", chain_id, config.name,
                )
            except Exception as exc:
                logger.error(
                    "Failed to update validators on chain %d: %s",
                    chain_id, exc,
                )

    async def _update_chain(
        self,
        chain_id: int,
        config: ChainDeployment,
        validators: list[str],
    ) -> None:
        """Call updateValidators() on a single chain's ValidatorRegistry."""
        if self.relayer is not None:
            await self.relayer.sync_validators(
                chain_id, validators,
            )
        else:
            logger.info(
                "[mock] Would update validators on %s (%d): %s",
                config.name, chain_id, validators[:3],
            )

    def set_validators(self, validators: list[str]) -> None:
        """Manually set the validator list (for testing)."""
        self._last_validators = list(validators)

    def stop(self) -> None:
        self._running = False
