"""Bittensor-specific validator wrapper around the core ValidationEngine.

Handles Bittensor blockchain operations, metagraph management, and wallet operations
while delegating core validation logic to the ValidationEngine.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any, Optional

import bittensor as bt

from .validation_engine import ValidationEngine, EpochResult, WeightCallback
from .aggregator_client import AggregatorClient
from .simulator import OrderSimulator
from .metagraph_manager import MetagraphManager
from .onchain_emitter import OnchainWeightsEmitter


class BittensorWeightCallback:
    """Weight callback that sets weights on Bittensor chain."""

    def __init__(
        self,
        metagraph_manager: MetagraphManager,
        onchain_emitter: OnchainWeightsEmitter,
        logger: Optional[logging.Logger] = None
    ):
        self.metagraph_manager = metagraph_manager
        self.onchain_emitter = onchain_emitter
        self.logger = logger or logging.getLogger(__name__)

    async def __call__(self, weights: Dict[str, float], epoch_result: EpochResult) -> bool:
        """Convert miner weights to UID weights and set on chain."""
        try:
            # Get current metagraph
            metagraph = await self.metagraph_manager.get_current_metagraph()

            # Convert miner IDs to UIDs
            uid_weights = {}
            for miner_id, weight in weights.items():
                # For now, use a simple mapping - in production you'd need proper miner_id -> UID mapping
                try:
                    # This is a placeholder - you'll need to implement proper mapping
                    # based on your miner registration system
                    uid = hash(miner_id) % metagraph.n
                    if 0 <= uid < metagraph.n:
                        uid_weights[uid] = weight
                except (ValueError, IndexError):
                    continue

            if uid_weights:
                success = await self.onchain_emitter.set_weights(uid_weights)
                if success:
                    self.logger.info(f"✅ Set weights for {len(uid_weights)} UIDs on Bittensor chain")
                    return True
                else:
                    self.logger.error("❌ Failed to set weights on Bittensor chain")
                    return False
            else:
                self.logger.warning("⚠️ No valid UID mappings found for weights")
                return False

        except Exception as e:
            self.logger.error(f"Error setting weights on Bittensor: {e}")
            return False


class BittensorValidator:
    """Bittensor validator that wraps the core ValidationEngine.

    This class handles all Bittensor-specific operations:
    - Wallet and hotkey management
    - Metagraph operations
    - On-chain weight emission
    - Subtensor connectivity

    While delegating validation logic to ValidationEngine.
    """

    def __init__(
        self,
        config,
        subtensor: Optional[bt.Subtensor] = None,
        wallet: Optional[bt.Wallet] = None,
        logger: Optional[logging.Logger] = None
    ):
        self.config = config
        self.subtensor = subtensor or bt.Subtensor(
            network=getattr(config.subtensor, "network", "finney"),
            config=config,
        )
        self.wallet = wallet or bt.Wallet(
            name=getattr(config.wallet, "name", "default"),
            hotkey=getattr(config.wallet, "hotkey", "default"),
            path=getattr(config.wallet, "path", "~/.bittensor/wallets"),
        )
        self.logger = logger or logging.getLogger(__name__)

        # Initialize Bittensor-specific components
        self._metagraph_manager = MetagraphManager(
            subtensor=self.subtensor,
            wallet=self.wallet,
            netuid=int(self.config.netuid),
            logger=self.logger
        )

        # Initialize on-chain emitter
        self._onchain_emitter = self._create_onchain_emitter()

        # Get creator hotkey for burn allocation
        self._creator_hotkey = self._get_creator_hotkey()

        # Initialize core validation engine
        self._validation_engine = self._create_validation_engine()

        # Register Bittensor weight callback
        weight_callback = BittensorWeightCallback(
            metagraph_manager=self._metagraph_manager,
            onchain_emitter=self._onchain_emitter,
            logger=self.logger
        )
        self._validation_engine.add_weight_callback(weight_callback)

    def _create_onchain_emitter(self):
        """Create the on-chain weights emitter."""
        netuid = int(self.config.netuid)
        wallet_name = getattr(self.wallet, 'name', 'validator')
        hotkey_name = getattr(self.wallet, 'hotkey_str', 'default')
        subtensor_network = getattr(self.subtensor, 'network', 'local')
        subtensor_address = getattr(self.subtensor, 'chain_endpoint', 'ws://127.0.0.1:9944')

        return OnchainWeightsEmitter(
            netuid=netuid,
            wallet_name=wallet_name,
            hotkey_name=hotkey_name,
            subtensor_network=subtensor_network,
            subtensor_address=subtensor_address,
            logger=self.logger
        )

    def _get_creator_hotkey(self) -> Optional[str]:
        """Get the creator hotkey for burn allocation."""
        try:
            # Do a lightweight metagraph sync to get UID 0 hotkey
            metagraph = bt.metagraph(netuid=self.config.netuid, subtensor=self.subtensor, lite=True)
            metagraph.sync(subtensor=self.subtensor, lite=True)

            if len(metagraph.hotkeys) > 0:
                creator_hotkey = str(metagraph.hotkeys[0])  # UID 0 is creator
                self.logger.info(f"Creator hotkey: {creator_hotkey[:8]}...", prefix="CONFIG")
                return creator_hotkey
        except Exception as e:
            self.logger.error(f"Failed to get creator hotkey: {e}", prefix="CONFIG")

        self.logger.warning("Creator hotkey lookup failed - burn allocation disabled", prefix="CONFIG")
        return None

    def _create_validation_engine(self) -> ValidationEngine:
        """Create the core validation engine with Bittensor-specific config."""
        # Initialize events client
        # Use VALIDATOR_API_KEY for validator-specific endpoints (required)
        validator_api_key = getattr(self.config, "validator_api_key", None)
        if not validator_api_key:
            raise ValueError("VALIDATOR_API_KEY is required for validator endpoints. Set it via --validator.api_key or VALIDATOR_API_KEY environment variable.")
        events_client = AggregatorClient(
            base_url=self.config.aggregator_url,
            api_key=validator_api_key,
            timeout=self.config.aggregator_timeout,
            logger=self.logger,
            verify_ssl=bool(self.config.aggregator_verify_ssl),
            max_retries=int(self.config.aggregator_max_retries),
            backoff_seconds=float(self.config.aggregator_backoff_seconds),
            page_limit=int(self.config.aggregator_page_limit),
        )

        # Generate validator ID
        validator_id = self.config.validator_id or self.wallet.hotkey.ss58_address

        # Get max concurrent simulations from config
        max_concurrent = getattr(self.config, "simulator_max_concurrent", 5)
        
        # Create simulator with container pool matching concurrency limit
        simulator = OrderSimulator(
            rpc_url=self.config.simulator_rpc_url,
            simulator_image=self.config.simulator_docker_image,
            logger=self.logger,
            container_pool_size=max_concurrent,  # One container per concurrent simulation
        )
        
        # Create validation engine
        # Pass wallet hotkey keypair for signing weights
        return ValidationEngine(
            events_client=events_client,
            validator_id=validator_id,
            simulator=simulator,
            logger=self.logger,
            validation_interval_seconds=int(self.config.validator_poll_seconds),
            burn_percentage=float(self.config.burn_percentage),
            creator_miner_id=self._creator_hotkey,
            max_concurrent_simulations=max_concurrent,
            signing_keypair=self.wallet.hotkey,  # Bittensor keypair for signing
            submit_weights_to_aggregator=True,
        )

    @property
    def validation_engine(self) -> ValidationEngine:
        """Access to the core validation engine."""
        return self._validation_engine

    async def run_continuous_epochs(self, epoch_minutes: int = 5):
        """Run continuous epochs with Bittensor weight emission."""
        self.logger.info("Starting Bittensor validator with continuous epochs", prefix="BITTENSOR")
        await self._validation_engine.run_continuous_epochs(epoch_minutes)

    async def run_single_epoch(self, epoch_minutes: int = 5) -> EpochResult:
        """Run a single epoch."""
        epoch_key = f"single-{asyncio.get_event_loop().time()}"
        return await self._validation_engine.run_epoch(epoch_key, epoch_minutes)

    async def start(self):
        """Start the validator."""
        self.logger.info("Bittensor validator starting up", prefix="BITTENSOR")

        # Sync metagraph
        await self._metagraph_manager.sync_metagraph()

        # Check wallet and registration
        if not await self._check_wallet_registration():
            raise RuntimeError("Wallet not properly registered on subnet")

    async def _check_wallet_registration(self) -> bool:
        """Check if wallet is registered and has stake."""
        try:
            metagraph = await self._metagraph_manager.get_current_metagraph()

            # Check if hotkey is registered
            hotkey = self.wallet.hotkey.ss58_address
            if hotkey not in metagraph.hotkeys:
                self.logger.error(f"Hotkey {hotkey[:8]}... not registered on subnet {self.config.netuid}")
                return False

            # Get UID
            uid = metagraph.hotkeys.index(hotkey)
            stake = metagraph.stake[uid]

            self.logger.info(
                f"Validator registered: UID={uid}, stake={stake:.4f}TAO",
                prefix="BITTENSOR"
            )

            return True

        except Exception as e:
            self.logger.error(f"Wallet registration check failed: {e}")
            return False

    async def stop(self):
        """Stop the validator."""
        self.logger.info("Bittensor validator shutting down", prefix="BITTENSOR")
        await self._validation_engine.stop_continuous_validation()
