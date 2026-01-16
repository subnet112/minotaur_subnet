"""Simulation validator for testing with real aggregator but without Bittensor operations.

Connects to real aggregator API, fetches real orders, simulates them with Docker,
but skips all Bittensor blockchain operations. Perfect for testing the validation
pipeline with real data without affecting the chain.
"""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable

try:
    import bittensor as bt
except ImportError:
    bt = None

from .validation_engine import ValidationEngine, ValidationResult, EpochResult, WeightCallback
from .simulator import OrderSimulator
from .aggregator_client import AggregatorClient


def _generate_unique_validator_id() -> str:
    """Generate a unique validator ID for simulation mode.
    
    Uses hostname + short UUID to ensure uniqueness across multiple validators.
    """
    hostname = socket.gethostname()
    short_uuid = str(uuid.uuid4())[:8]
    return f"simulation-{hostname}-{short_uuid}"


class MockEventsClient(AggregatorClient):
    """Mock events client that generates fake orders and miner data."""

    def __init__(self, num_miners: int = 10, orders_per_epoch: int = 20):
        # Don't call super().__init__ since we're mocking
        self.num_miners = num_miners
        self.orders_per_epoch = orders_per_epoch
        self.logger = logging.getLogger(__name__)

        # Generate mock miner data
        self._miners_stats = self._generate_mock_miners()

        # Track submitted validations for testing
        self.submitted_validations = []

    def _generate_mock_miners(self) -> List[Dict[str, Any]]:
        """Generate mock miner statistics."""
        miners = []
        for i in range(self.num_miners):
            miner_id = f"miner-{i:02d}"
            # Each miner has 1-3 solvers
            num_solvers = random.randint(1, 3)
            solver_ids = [f"solver-{i}-{j}" for j in range(num_solvers)]

            miners.append({
                "minerId": miner_id,
                "solverIds": solver_ids,
                "performance": random.uniform(0.5, 1.0),  # Mock performance score
            })

        return miners

    def _generate_mock_order(self, order_id: str) -> Dict[str, Any]:
        """Generate a mock order structure."""
        # Pick a random solver
        miner = random.choice(self._miners_stats)
        solver_id = random.choice(miner["solverIds"])

        return {
            "orderId": order_id,
            "quoteDetails": {
                "solverId": solver_id,
                "quoteId": f"quote-{order_id}",
                "settlement": {
                    "interactions": [
                        {
                            "target": "0x" + "".join(random.choices("0123456789abcdef", k=40)),
                            "value": hex(random.randint(0, 10**18)),  # 0-1 ETH
                            "callData": "0x" + "".join(random.choices("0123456789abcdef", k=64)),
                        }
                    ]
                }
            },
            "signature": "0x" + "".join(random.choices("0123456789abcdef", k=130)),
        }

    async def fetch_pending_orders(self, validator_id: str) -> List[Dict[str, Any]]:
        """Return mock pending orders."""
        orders = []
        for i in range(self.orders_per_epoch):
            order_id = f"mock-order-{int(time.time())}-{i}"
            orders.append(self._generate_mock_order(order_id))

        self.logger.info(f"Generated {len(orders)} mock orders")
        return orders

    async def submit_validation(
        self,
        order_id: str,
        validator_id: str,
        success: bool,
        notes: str = ""
    ) -> bool:
        """Record validation submission for testing."""
        validation = {
            "order_id": order_id,
            "validator_id": validator_id,
            "success": success,
            "notes": notes,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.submitted_validations.append(validation)
        self.logger.debug(f"Mock validation submitted: {order_id} -> {success}")
        return True

    async def fetch_miners_stats(self) -> Optional[List[Dict[str, Any]]]:
        """Return mock miners statistics."""
        return self._miners_stats.copy()


class MockWeightCallback:
    """Mock weight callback that logs weights instead of setting them on chain."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.weights_history = []

    async def __call__(self, weights: Dict[str, float], epoch_result: EpochResult) -> bool:
        """Log weights and store in history."""
        self.weights_history.append({
            "epoch_key": epoch_result.epoch_key,
            "weights": weights.copy(),
            "stats": epoch_result.stats.copy(),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        self.logger.info(f"📊 Mock weights set for epoch {epoch_result.epoch_key}:")
        sorted_weights = sorted(weights.items(), key=lambda x: -x[1])
        for i, (miner_id, weight) in enumerate(sorted_weights[:5], 1):  # Top 5
            self.logger.info(f"   {i}. {miner_id}: {weight:.4f} ({weight*100:.1f}%)")

        self.logger.info(f"   Total miners: {len(weights)}")
        self.logger.info(f"   Validations: {epoch_result.stats.get('total_simulations', 0)}")

        return True


class MockValidator:
    """Simulation validator for testing with real aggregator but without Bittensor operations.

    Connects to real aggregator API and simulates real orders, but skips blockchain operations.
    Perfect for testing the validation pipeline with real data without affecting the chain.

    Requires:
    - Real aggregator connection (events_url, api_key)
    - Real simulator (rpc_url, docker_image)
    - But NO Bittensor wallet/metagraph
    """

    def __init__(
        self,
        aggregator_url: str,
        validator_api_key: str,
        simulator_rpc_url: Optional[str] = None,
        simulator_docker_image: str = "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest",
        validator_id: Optional[str] = None,
        burn_percentage: float = 0.0,
        creator_miner_id: Optional[str] = None,
        validation_interval_seconds: int = 5,
        max_concurrent_simulations: int = 5,
        logger: Optional[logging.Logger] = None,
        heartbeat_callback: Optional[Callable[[], None]] = None,
    ):
        self.logger = logger or logging.getLogger(__name__)

        # Generate unique validator ID if not provided
        if validator_id is None:
            validator_id = _generate_unique_validator_id()
            self.logger.info(f"Generated unique validator ID: {validator_id}")
        else:
            self.logger.info(f"Using provided validator ID: {validator_id}")
        
        self.validator_id = validator_id

        # Track submitted validations for statistics
        self.submitted_validations = []

        # Use VALIDATOR_API_KEY for validator-specific endpoints (required)
        if not validator_api_key:
            raise ValueError("VALIDATOR_API_KEY is required for validator endpoints. Set it via validator_api_key parameter or VALIDATOR_API_KEY environment variable.")

        # Create REAL aggregator events client (not mock)
        self.events_client = AggregatorClient(
            base_url=aggregator_url,
            api_key=validator_api_key,
            timeout=10,  # Default timeout
            logger=self.logger,
            verify_ssl=True,  # Default SSL verification
            max_retries=3,  # Default retries
            backoff_seconds=0.5,  # Default backoff
            page_limit=500,  # Default page limit
        )

        # Create mock weight callback (logs instead of setting on chain)
        self.mock_weight_callback = MockWeightCallback(logger=self.logger)

        # Create REAL simulator (uses Docker and RPC)
        # Use container pool size matching max_concurrent_simulations for parallelism
        self.simulator = OrderSimulator(
            rpc_url=simulator_rpc_url,
            simulator_image=simulator_docker_image,
            logger=self.logger,
            container_pool_size=max_concurrent_simulations,  # One container per concurrent simulation
        )

        # Generate a test keypair for signing weights in simulation mode
        # Use deterministic generation from validator_id for consistency
        test_keypair = None
        if bt:
            try:
                # Generate a deterministic test keypair from validator_id
                # Convert validator_id to a hex string seed (Bittensor expects string, not bytes)
                import hashlib
                seed_bytes = hashlib.sha256(validator_id.encode('utf-8')).digest()[:32]
                # Convert bytes to hex string for Bittensor
                seed_hex = seed_bytes.hex()
                # Use create_from_uri with hex seed format
                test_keypair = bt.Keypair.create_from_uri(f"//{seed_hex[:16]}")  # Use first 16 chars as URI seed
                self.logger.info(f"Generated test keypair for simulation mode: {test_keypair.ss58_address}")
            except Exception as e:
                self.logger.warning(f"Failed to generate test keypair for simulation mode: {e}")
                # Try alternative method using mnemonic
                try:
                    # Fallback: create a simple deterministic mnemonic-like string
                    import hashlib
                    seed_hash = hashlib.sha256(validator_id.encode('utf-8')).hexdigest()
                    # Use the hash as a URI seed (Bittensor format)
                    test_keypair = bt.Keypair.create_from_uri(f"//test-validator-{seed_hash[:12]}")
                    self.logger.info(f"Generated test keypair using fallback method: {test_keypair.ss58_address}")
                except Exception as e2:
                    self.logger.warning(f"Fallback keypair generation also failed: {e2}")
        
        # Use the SS58 address from the keypair as the validator_id (required for aggregator)
        # If keypair generation failed, fall back to the provided/generated validator_id
        if test_keypair and hasattr(test_keypair, 'ss58_address'):
            validator_id_ss58 = test_keypair.ss58_address
            self.logger.info(f"Using SS58 address as validator_id: {validator_id_ss58}")
        else:
            validator_id_ss58 = validator_id
            self.logger.warning(f"No keypair available, using original validator_id: {validator_id_ss58} (may not be valid SS58)")
        
        # Create validation engine with real components
        # Enable weight submission in mock mode with test keypair
        self.validation_engine = ValidationEngine(
            events_client=self.events_client,
            validator_id=validator_id_ss58,
            simulator=self.simulator,
            logger=self.logger,
            validation_interval_seconds=validation_interval_seconds,
            burn_percentage=burn_percentage,
            creator_miner_id=creator_miner_id,  # Optional creator address for burn testing
            max_concurrent_simulations=max_concurrent_simulations,
            signing_keypair=test_keypair,  # Test keypair for simulation mode
            submit_weights_to_aggregator=True,  # Enable weight submission in mock mode
            heartbeat_callback=heartbeat_callback,
        )
        
        # Log burn configuration
        if burn_percentage > 0.0:
            if creator_miner_id:
                self.logger.info(
                    f"🔥 Burn configured: {burn_percentage:.1%} will be allocated to creator miner {creator_miner_id[:8]}..."
                )
            else:
                self.logger.warning(
                    f"⚠️  Burn percentage is {burn_percentage:.1%} but creator_miner_id is not set - "
                    f"burn will NOT be applied to weights (only tracked in stats)"
                )

        # Register mock weight callback (logs weights instead of setting on chain)
        self.validation_engine.add_weight_callback(self.mock_weight_callback)

        self.logger.info("🔧 Simulation validator initialized:")
        self.logger.info(f"   Aggregator: {aggregator_url}")
        self.logger.info(f"   Simulator: {simulator_docker_image}")
        self.logger.info(f"   RPC URL: {simulator_rpc_url or 'default'}")
        self.logger.info("   ⚠️  Will NOT set weights on Bittensor chain")

    async def run_continuous_epochs(self, epoch_minutes: int = 1):
        """Run continuous simulation epochs."""
        self.logger.info("🧪 Starting simulation validator with continuous epochs")
        self.logger.info(f"   Real aggregator: {self.events_client.base_url}")
        self.logger.info(f"   Real simulator: {self.simulator.simulator_image}")
        self.logger.info(f"   Epoch duration: {epoch_minutes} minutes")
        self.logger.info("   ⚠️  Simulation mode - no blockchain operations")

        await self.validation_engine.run_continuous_epochs(epoch_minutes)

    async def run_single_epoch(self, epoch_minutes: int = 1) -> EpochResult:
        """Run a single mock epoch."""
        epoch_key = f"mock-single-{int(time.time())}"
        return await self.validation_engine.run_epoch(epoch_key, epoch_minutes)

    async def run_test_epochs(self, num_epochs: int = 3, epoch_minutes: int = 1):
        """Run multiple test epochs and show results."""
        self.logger.info(f"🧪 Running {num_epochs} test epochs")
        self.logger.info(f"   Aggregator: {self.events_client.base_url}")
        self.logger.info(f"   Epoch duration: {epoch_minutes} minutes")
        self.logger.info("   Starting background validation loop...")

        # Start background validation to fetch orders during epochs
        await self.validation_engine.start_continuous_validation()

        try:
            for epoch in range(num_epochs):
                self.logger.info(f"\n{'='*50}")
                self.logger.info(f"Test Epoch {epoch + 1}/{num_epochs}")
                self.logger.info(f"{'='*50}")

                epoch_result = await self.run_single_epoch(epoch_minutes)

                # Show detailed results
                self._print_epoch_summary(epoch_result)

                # Wait a bit between epochs
                if epoch < num_epochs - 1:
                    await asyncio.sleep(2)

            # Show final statistics
            self._print_final_statistics()
        finally:
            # Stop background validation
            await self.validation_engine.stop_continuous_validation()

    def _print_epoch_summary(self, epoch_result: EpochResult):
        """Print detailed summary of an epoch."""
        duration = (epoch_result.end_time - epoch_result.start_time).total_seconds()

        self.logger.info("📊 Epoch Summary:")
        self.logger.info(f"   Duration: {duration:.1f} seconds")
        self.logger.info(f"   Validations: {len(epoch_result.validation_results)}")

        # Calculate success rate safely
        num_validations = len(epoch_result.validation_results)
        if num_validations > 0:
            success_rate = sum(1 for r in epoch_result.validation_results if r.success) / num_validations * 100
            self.logger.info(f"   Success rate: {success_rate:.1f}%")
        else:
            self.logger.info("   Success rate: N/A (no validations performed)")

        # Miner performance
        miner_stats = {}
        for result in epoch_result.validation_results:
            miner_id = result.miner_id
            if miner_id:
                if miner_id not in miner_stats:
                    miner_stats[miner_id] = {"total": 0, "success": 0}
                miner_stats[miner_id]["total"] += 1
                if result.success:
                    miner_stats[miner_id]["success"] += 1

        self.logger.info("🏆 Top Miners:")
        sorted_miners = sorted(
            miner_stats.items(),
            key=lambda x: x[1]["success"] / x[1]["total"] if x[1]["total"] > 0 else 0,
            reverse=True
        )

        for i, (miner_id, stats) in enumerate(sorted_miners[:3], 1):
            rate = stats["success"] / stats["total"] if stats["total"] > 0 else 0
            weight = epoch_result.weights.get(miner_id, 0)
            self.logger.info(f"   {i}. {miner_id}: {stats['success']}/{stats['total']} ({rate:.1%}) -> {weight:.3f} weight")

    def _print_final_statistics(self):
        """Print final statistics from all epochs."""
        self.logger.info(f"\n{'='*60}")
        self.logger.info("🎯 FINAL TEST STATISTICS")
        self.logger.info(f"{'='*60}")

        weights_history = self.mock_weight_callback.weights_history

        self.logger.info(f"Total epochs run: {len(weights_history)}")

        if weights_history:
            # Average weights across epochs
            all_weights = {}
            for epoch_data in weights_history:
                for miner_id, weight in epoch_data["weights"].items():
                    if miner_id not in all_weights:
                        all_weights[miner_id] = []
                    all_weights[miner_id].append(weight)

            avg_weights = {
                miner_id: sum(weights) / len(weights)
                for miner_id, weights in all_weights.items()
            }

            self.logger.info("📈 Average weights across all epochs:")
            sorted_avg = sorted(avg_weights.items(), key=lambda x: -x[1])
            for i, (miner_id, avg_weight) in enumerate(sorted_avg[:5], 1):
                consistency = len(all_weights[miner_id]) / len(weights_history)
                self.logger.info(f"   {i}. {miner_id}: {avg_weight:.4f} avg ({consistency:.0%} consistency)")

        # Validation statistics
        total_validations = len(self.submitted_validations)
        successful_validations = sum(1 for v in self.submitted_validations if v["success"])

        self.logger.info(f"\n✅ Total validations submitted: {total_validations}")
        self.logger.info(f"✅ Successful validations: {successful_validations}")
        self.logger.info(f"✅ Success rate: {successful_validations / total_validations * 100:.1f}%" if total_validations > 0 else "✅ Success rate: N/A")

        self.logger.info(f"\n{'='*60}")
