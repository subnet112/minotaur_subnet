"""Core validation engine independent of Bittensor.

Handles order simulation, scoring, epoch management, and weight computation
without any blockchain or wallet dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Callable, Protocol

from .simulator import OrderSimulator
from .aggregator_client import AggregatorClient


class ValidationResult:
    """Result of a single order validation."""

    def __init__(
        self,
        order_id: str,
        solver_id: str,
        miner_id: Optional[str],
        success: bool,
        error_message: Optional[str] = None,
        execution_time: Optional[float] = None,
        user_address: Optional[str] = None,
    ):
        self.order_id = order_id
        self.solver_id = solver_id
        self.miner_id = miner_id
        self.success = success
        self.error_message = error_message
        self.execution_time = execution_time
        self.user_address = user_address
        self.timestamp = datetime.now(timezone.utc)


class EpochResult:
    """Result of an epoch with all validation results and computed weights."""

    def __init__(
        self,
        epoch_key: str,
        start_time: datetime,
        end_time: datetime,
        validation_results: List[ValidationResult],
        weights: Dict[str, float],
        stats: Dict[str, Any]
    ):
        self.epoch_key = epoch_key
        self.start_time = start_time
        self.end_time = end_time
        self.validation_results = validation_results
        self.weights = weights
        self.stats = stats


class WeightCallback(Protocol):
    """Callback for when weights are computed and ready to be set."""

    async def __call__(self, weights: Dict[str, float], epoch_result: EpochResult) -> bool:
        """Set weights on the target system. Returns success."""
        ...


class ValidationEngine:
    """Core validation engine independent of blockchain specifics.

    Handles the complete validation pipeline:
    1. Fetch pending orders
    2. Simulate orders using Docker
    3. Submit validation results
    4. Compute miner scores and weights
    5. Coordinate epochs and callbacks
    """

    def __init__(
        self,
        events_client: AggregatorClient,
        validator_id: str,
        simulator: Optional[OrderSimulator] = None,
        logger: Optional[logging.Logger] = None,
        validation_interval_seconds: int = 5,
        burn_percentage: float = 0.0,
        creator_miner_id: Optional[str] = None,
        max_concurrent_simulations: int = 5,
        signing_keypair: Optional[Any] = None,
        submit_weights_to_aggregator: bool = True,
        heartbeat_callback: Optional[Callable[[], None]] = None,
        filter_user_address: Optional[str] = None,
    ):
        self.events_client = events_client
        self.validator_id = validator_id
        
        # Concurrency control for simulations
        self.max_concurrent_simulations = max_concurrent_simulations
        
        # Create simulator with container pool matching concurrency limit if not provided
        if simulator is None:
            simulator = OrderSimulator(container_pool_size=max_concurrent_simulations)
        self.simulator = simulator
        
        self.logger = logger or logging.getLogger(__name__)

        self.validation_interval_seconds = validation_interval_seconds
        self.burn_percentage = burn_percentage
        self.creator_miner_id = creator_miner_id
        self.signing_keypair = signing_keypair  # Bittensor keypair for signing weights
        self.submit_weights_to_aggregator = submit_weights_to_aggregator
        self._heartbeat_callback = heartbeat_callback
        self.filter_user_address = filter_user_address  # Only count orders from this user for scoring (mock mode)

        self._simulation_semaphore: Optional[asyncio.Semaphore] = None  # Created lazily in the event loop
        if self.logger:
            self.logger.info(f"🔒 Simulation concurrency limit: {max_concurrent_simulations} concurrent simulations")
            self.logger.info(f"🐳 Container pool size: {simulator.container_pool_size if hasattr(simulator, 'container_pool_size') else 'N/A'}")

        # Epoch management
        self._current_epoch_key: Optional[str] = None
        self._epoch_results: Dict[str, List[ValidationResult]] = {}
        self._submitted_epochs: set[str] = set()  # Track epochs that have had weights submitted
        self._validation_running = False
        self._validation_thread: Optional[threading.Thread] = None

        # Validation history for chain-aligned windowing
        self._validation_history: List[ValidationResult] = []
        self._validation_history_lock = threading.Lock()
        try:
            self._history_retention_seconds = int(
                os.getenv("VALIDATION_HISTORY_RETENTION_SECONDS", "7200")
            )
        except Exception:
            self._history_retention_seconds = 7200
        
        # Logging state for no-orders tracking
        self._last_no_orders_log: Optional[float] = None
        self._no_orders_check_count = 0
        
        # Health check tracking
        self._health_check_interval = 30  # Check health every 30 seconds
        self._last_health_check: Optional[float] = None
        self._aggregator_healthy: Optional[bool] = None  # None = unknown, True = healthy, False = unhealthy

        # Callbacks
        self._weight_callbacks: List[WeightCallback] = []

    def add_weight_callback(self, callback: WeightCallback):
        """Add a callback to be called when weights are computed."""
        self._weight_callbacks.append(callback)

    def _append_validation_results(self, results: List[ValidationResult]) -> None:
        if not results:
            return
        now = datetime.now(timezone.utc)
        with self._validation_history_lock:
            self._validation_history.extend(results)
            self._prune_validation_history(now)

    def _prune_validation_history(self, now: datetime) -> None:
        if self._history_retention_seconds <= 0:
            self._validation_history.clear()
            return
        cutoff = now.timestamp() - self._history_retention_seconds
        self._validation_history = [
            r for r in self._validation_history if r.timestamp.timestamp() >= cutoff
        ]

    def get_results_for_window(self, from_ts: str, to_ts: str) -> List[ValidationResult]:
        """Return validation results within [from_ts, to_ts)."""
        def _parse(ts: str) -> datetime:
            normalized = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)

        start = _parse(from_ts)
        end = _parse(to_ts)
        with self._validation_history_lock:
            return [
                r for r in self._validation_history
                if start <= r.timestamp < end
            ]

    def _get_simulation_semaphore(self) -> asyncio.Semaphore:
        """Get or create the simulation semaphore in the current event loop.
        
        Semaphores must be created in the same event loop where they're used.
        This method creates the semaphore lazily to avoid cross-loop issues.
        """
        if self._simulation_semaphore is None:
            self._simulation_semaphore = asyncio.Semaphore(self.max_concurrent_simulations)
        return self._simulation_semaphore

    async def validate_single_order(self, order: dict) -> ValidationResult:
        """Validate a single order and return the result.
        
        This method acquires a semaphore slot before simulating to limit
        concurrent simulations to max_concurrent_simulations.
        """
        order_id = order.get("orderId", "unknown")
        solver_id = order.get("solverId")  # Directly from order
        miner_id = order.get("minerId")  # Directly from order

        # Extract user address from order for filtering
        user_address = None
        quote_details = order.get("quoteDetails", {})
        available_inputs = quote_details.get("availableInputs", [])
        if available_inputs and len(available_inputs) > 0:
            user_address = available_inputs[0].get("user")

        if not solver_id:
            self.logger.warning(f"Order {order_id} missing solverId")
            return ValidationResult(order_id, "unknown", miner_id, False, "Missing solverId", user_address=user_address)

        if not miner_id:
            self.logger.warning(f"Order {order_id} missing minerId")
            return ValidationResult(order_id, solver_id, None, False, "Missing minerId", user_address=user_address)

        # Get semaphore for current event loop (created lazily if needed)
        semaphore = self._get_simulation_semaphore()
        
        # Acquire semaphore slot before simulating (limits concurrent simulations)
        async with semaphore:
            self.logger.info(f"🚀 Starting simulation for order {order_id} (solver: {solver_id}, miner: {miner_id})")
            start_time = time.time()
            success, error_message = await self.simulator.simulate_order(order)
            execution_time = time.time() - start_time

        # Submit validation result to aggregator
        notes = error_message or ""
        self.logger.debug(
            f"📤 Submitting validation to aggregator: order_id={order_id}, success={success}, "
            f"notes_length={len(notes)}, notes_preview={notes[:100] if notes else 'N/A'}..."
        )
        
        submission_success = await self.events_client.submit_validation(
            order_id=order_id,
            validator_id=self.validator_id,
            success=success,
            notes=notes
        )

        if submission_success:
            self.logger.debug(f"✅ Validation result submitted to aggregator: order_id={order_id}, success={success}")
        else:
            self.logger.error(f"⚠️  Failed to report validation result to aggregator for order {order_id} (success={success})")

        result = ValidationResult(
            order_id=order_id,
            solver_id=solver_id,
            miner_id=miner_id,
            success=success,
            error_message=error_message,
            execution_time=execution_time,
            user_address=user_address,
        )

        if success:
            # Use SUCCESS level logging if available (e.g., bittensor logging), otherwise use INFO
            if hasattr(self.logger, 'success'):
                self.logger.success(f"✅ Order {order_id} validated successfully ({execution_time:.2f}s)", prefix="VALIDATION")
            else:
                self.logger.info(f"✅ Order {order_id} validated successfully ({execution_time:.2f}s)")
        else:
            self.logger.warning(f"❌ Order {order_id} validation failed: {error_message}")

        return result

    async def fetch_and_validate_orders(self) -> List[ValidationResult]:
        """Fetch pending orders and validate them all."""
        try:
            orders = await self.events_client.fetch_pending_orders(self.validator_id)
            if not orders:
                self.logger.debug(f"📭 No pending orders found for validator {self.validator_id}")
                return []

            self.logger.info(f"🧪 Simulating {len(orders)} pending orders (max {self.max_concurrent_simulations} concurrent)...")

            # Validate all orders concurrently (limited by semaphore)
            tasks = [self.validate_single_order(order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter out exceptions and collect results
            valid_results = []
            exceptions = []
            for result in results:
                if isinstance(result, ValidationResult):
                    valid_results.append(result)
                elif isinstance(result, Exception):
                    exceptions.append(result)
                    self.logger.error(f"Validation error: {result}")

            # Log validation summary
            successful_validations = sum(1 for r in valid_results if r.success)
            failed_validations = len(valid_results) - successful_validations

            if valid_results:
                self._append_validation_results(valid_results)
                success_rate = successful_validations / len(valid_results) * 100
                self.logger.info(
                    f"📊 Validation batch complete: {len(valid_results)} orders processed "
                    f"({successful_validations} ✅ success, {failed_validations} ❌ failed, "
                    f"{success_rate:.1f}% success rate)"
                )
                if exceptions:
                    self.logger.warning(f"⚠️  {len(exceptions)} validation tasks failed with exceptions")

            return valid_results

        except Exception as e:
            self.logger.error(f"Error in fetch_and_validate_orders: {e}")
            return []

    def _compute_scores_from_results(self, validation_results: List[ValidationResult]) -> Tuple[Dict[str, dict], Dict[str, int]]:
        """Compute scores and stats from validation results."""
        miner_validated: Dict[str, int] = defaultdict(int)
        miner_total: Dict[str, int] = defaultdict(int)
        filtered_count = 0

        for result in validation_results:
            miner_id = result.miner_id
            if miner_id is None:
                self.logger.warning(f"Skipping result for order {result.order_id}: missing miner_id")
                continue  # Skip results without miner_id

            # Filter by user address if configured (for mock mode testing)
            if self.filter_user_address:
                if result.user_address != self.filter_user_address:
                    filtered_count += 1
                    continue  # Skip orders not from the specified user

            miner_total[miner_id] += 1
            if result.success:
                miner_validated[miner_id] += 1

        if filtered_count > 0:
            self.logger.info(f"🔍 Filtered {filtered_count} orders not from user {self.filter_user_address[:10]}... (only counting filtered user for scoring)")

        # Compute scores
        scores = {}
        for miner_id in miner_total.keys():
            validated_count = miner_validated.get(miner_id, 0)
            total_count = miner_total[miner_id]

            scores[miner_id] = {
                "score": float(validated_count),  # Simple: validated count = score
                "stats": {
                    "validated_orders": validated_count,
                    "total_orders": total_count,
                    "validation_rate": validated_count / total_count if total_count > 0 else 0.0
                },
                "breakdown": {
                    "validated_orders": validated_count,
                    "total_orders": total_count,
                    "validation_rate": validated_count / total_count if total_count > 0 else 0.0
                }
            }

        stats = {
            "total_simulations": len(validation_results),
            "valid_miners": len(scores),
            "total_miners": len(miner_total),
            "burn_percentage": self.burn_percentage,
            "filtered_orders": filtered_count,
            "filter_user_address": self.filter_user_address,
        }

        return scores, stats

    def _normalize_scores_to_weights(self, scores: Dict[str, dict]) -> Dict[str, float]:
        """Normalize scores to weights summing to 1.0.
        
        When no miners have scores, falls back to 100% burn to creator if set.
        """
        total_score = sum(data["score"] for data in scores.values())
        
        if total_score == 0:
            # No miner activity
            if not scores:
                # No miners at all - fallback to 100% burn to creator
                if self.creator_miner_id:
                    self.logger.info(f"🔥 No miner scores - falling back to 100% burn to creator {self.creator_miner_id[:8]}...")
                    return {self.creator_miner_id: 1.0}
                else:
                    self.logger.debug("No miner scores and no creator_miner_id - weights will be empty")
                    return {}
            else:
                # Miners exist but no activity - equal distribution
                weights = {miner_id: 1.0 / len(scores) for miner_id in scores.keys()}
                self.logger.debug(f"Equal distribution (no activity): {weights}")
        else:
            weights = {miner_id: data["score"] / total_score for miner_id, data in scores.items()}
            self.logger.debug(f"Normalized weights from scores: {weights}")

        # Apply burn allocation if specified
        if self.burn_percentage > 0.0 and self.creator_miner_id:
            if weights:
                # Miners exist - apply burn: miners share (1 - burn_percentage), creator gets burn_percentage
                allocate_percentage = 1.0 - self.burn_percentage
                for miner_id in weights:
                    weights[miner_id] *= allocate_percentage
                self.logger.debug(f"Applied burn {self.burn_percentage:.1%}: miners share {allocate_percentage:.1%}")
                
                # Allocate burn percentage to creator
                if self.creator_miner_id in weights:
                    weights[self.creator_miner_id] += self.burn_percentage
                else:
                    weights[self.creator_miner_id] = self.burn_percentage
            else:
                # No miner weights - 100% burn to creator
                self.logger.info(f"🔥 No miner weights - 100% burn to creator {self.creator_miner_id[:8]}...")
                weights = {self.creator_miner_id: 1.0}

        if weights:
            self.logger.debug(f"Final weights: {weights} (sum: {sum(weights.values()):.6f})")
        else:
            self.logger.info(f"Final weights: {{}} (empty - no creator_miner_id set)")
        return weights

    async def compute_weights_for_epoch(
        self,
        epoch_key: str,
        validation_results: List[ValidationResult]
    ) -> EpochResult:
        """Compute weights for an epoch from validation results."""
        start_time = datetime.now(timezone.utc)
        epoch_start = min((r.timestamp for r in validation_results), default=start_time)

        # Check aggregator health - if unhealthy, fallback to 100% burn to creator
        if self._aggregator_healthy is False:
            if self.creator_miner_id:
                self.logger.warning(f"🔥 Aggregator is unhealthy - falling back to 100% burn to creator {self.creator_miner_id[:8]}...")
                weights = {self.creator_miner_id: 1.0}
            else:
                self.logger.error(f"❌ Aggregator is unhealthy and no creator_miner_id set - cannot emit burn weights")
                weights = {}
            stats = {
                "total_simulations": len(validation_results),
                "valid_miners": 0,
                "total_miners": 0,
                "burn_percentage": 1.0,
                "burn_fallback": True,
                "error": "aggregator_unhealthy"
            }
            end_time = datetime.now(timezone.utc)
            epoch_result = EpochResult(
                epoch_key=epoch_key,
                start_time=epoch_start,
                end_time=end_time,
                validation_results=validation_results,
                weights=weights,
                stats=stats
            )
            await self.process_epoch_results(epoch_result)
            return epoch_result

        if not validation_results:
            self.logger.warning(f"⚠️  No validation results to compute weights for epoch {epoch_key}")
            if self.creator_miner_id:
                self.logger.info(f"🔥 No validation results - falling back to 100% burn to creator {self.creator_miner_id[:8]}...")
            else:
                self.logger.info("No validation results and no creator_miner_id set - weights will be empty")

        # Compute scores and stats (miner_id is now included directly in validation results)
        scores, stats = self._compute_scores_from_results(validation_results)
        
        self.logger.debug(f"Computed scores: {scores}")

        # Normalize to weights
        weights = self._normalize_scores_to_weights(scores)

        # Log weight summary (INFO level so it's always visible)
        if weights:
            miner_weights = {k: v for k, v in weights.items() if k != self.creator_miner_id}
            creator_weight = weights.get(self.creator_miner_id, 0.0)
            
            if miner_weights:
                self.logger.info(f"📊 Weight distribution: {len(miner_weights)} miner(s) share {sum(miner_weights.values()):.1%}, creator: {creator_weight:.1%}")
            else:
                self.logger.info(f"📊 Weight distribution: 0 miners, creator: {creator_weight:.1%} (100% burn)")
        else:
            self.logger.info(f"📊 Weight distribution: No weights computed (empty result - 100% burn, 0% to all miners)")

        # Log burn configuration and application
        self.logger.info(
            f"🔥 Burn configuration:\n"
            f"   burn_percentage: {self.burn_percentage:.1%}\n"
            f"   creator_miner_id: {self.creator_miner_id or 'NOT SET'}\n"
            f"   burn will be applied: {self.burn_percentage > 0.0 and self.creator_miner_id is not None}"
        )
        
        # Apply burn allocation logging
        if self.burn_percentage > 0.0 and self.creator_miner_id:
            if weights:
                creator_weight = weights.get(self.creator_miner_id, 0.0)
                miner_total = sum(v for k, v in weights.items() if k != self.creator_miner_id)
                self.logger.info(
                    f"🔥 Burn allocation applied:\n"
                    f"   {self.burn_percentage:.1%} to creator miner {self.creator_miner_id[:8]}... (weight: {creator_weight:.4f})\n"
                    f"   {1.0 - self.burn_percentage:.1%} to other miners (total: {miner_total:.4f})"
                )
            else:
                self.logger.info(f"🔥 Burn allocation: 100% burn (no weights allocated)")
        elif self.burn_percentage > 0.0:
            self.logger.warning(
                f"⚠️  Burn percentage is {self.burn_percentage:.1%} but creator_miner_id is not set - burn will NOT be applied!"
            )

        end_time = datetime.now(timezone.utc)
        epoch_result = EpochResult(
            epoch_key=epoch_key,
            start_time=epoch_start,
            end_time=end_time,
            validation_results=validation_results,
            weights=weights,
            stats=stats
        )

        return epoch_result

    async def process_epoch_results(self, epoch_result: EpochResult):
        """Process epoch results and notify callbacks."""
        # Calculate detailed validation statistics
        total_validations = len(epoch_result.validation_results)
        successful_validations = sum(1 for r in epoch_result.validation_results if r.success)
        failed_validations = total_validations - successful_validations

        # Log epoch completion with detailed statistics
        duration = (epoch_result.end_time - epoch_result.start_time).total_seconds()
        if total_validations > 0:
            success_rate = successful_validations / total_validations * 100
            avg_execution_time = sum(r.execution_time for r in epoch_result.validation_results if r.execution_time) / total_validations
            self.logger.info(
                f"📊 Epoch {epoch_result.epoch_key} completed in {duration:.1f}s:\n"
                f"   • Total validations: {total_validations}\n"
                f"   • ✅ Successful: {successful_validations} ({success_rate:.1f}%)\n"
                f"   • ❌ Failed: {failed_validations}\n"
                f"   • ⏱️  Average execution time: {avg_execution_time:.2f}s\n"
                f"   • 🏆 Unique miners validated: {len(epoch_result.weights)}"
            )
        else:
            self.logger.info(f"📊 Epoch {epoch_result.epoch_key} completed in {duration:.1f}s: No validations performed")

        # Submit weights to aggregator if enabled
        if self.submit_weights_to_aggregator:
            await self._submit_weights_to_aggregator(epoch_result)

        # Call weight callbacks
        success_count = 0
        for callback in self._weight_callbacks:
            try:
                success = await callback(epoch_result.weights, epoch_result)
                if success:
                    success_count += 1
            except Exception as e:
                self.logger.error(f"Weight callback error: {e}")

        self.logger.info(f"✅ Weights set successfully by {success_count}/{len(self._weight_callbacks)} callbacks")

    async def _submit_weights_to_aggregator(self, epoch_result: EpochResult):
        """Submit computed weights to aggregator."""
        # Check if weights have already been submitted for this epoch
        if epoch_result.epoch_key in self._submitted_epochs:
            self.logger.warning(f"⚠️  Weights already submitted for epoch {epoch_result.epoch_key}, skipping duplicate submission")
            return

        self.logger.info(f"📤 Starting weight submission for epoch {epoch_result.epoch_key}")
        self.logger.info(f"   validator_id: {self.validator_id}")
        self.logger.info(f"   signing_keypair available: {self.signing_keypair is not None}")
        if self.signing_keypair:
            try:
                keypair_ss58 = getattr(self.signing_keypair, 'ss58_address', None)
                self.logger.info(f"   keypair SS58: {keypair_ss58}")
            except Exception as e:
                self.logger.warning(f"   Could not get keypair SS58: {e}")
        
        try:
            # Sort weights for canonical format
            sorted_weights = dict(sorted(epoch_result.weights.items()))
            weights_sum = sum(epoch_result.weights.values())
            
            self.logger.info(f"   weights count: {len(sorted_weights)}, weights_sum: {weights_sum}")
            
            # Build stats
            stats = {
                "totalSimulations": epoch_result.stats.get("total_simulations", 0),
                "validMiners": epoch_result.stats.get("valid_miners", 0),
                "totalMiners": epoch_result.stats.get("total_miners", 0),
                "burnPercentage": epoch_result.stats.get("burn_percentage", 0.0),
                "weightsSum": weights_sum
            }
            
            # Prepare timestamp
            timestamp = datetime.now(timezone.utc).isoformat()
            
            # Build canonical payload
            canonical_payload = self.events_client._build_canonical_weights_payload(
                validator_id=self.validator_id,
                epoch_key=epoch_result.epoch_key,
                timestamp=timestamp,
                block_number=None,  # Block number not available in ValidationEngine
                weights=sorted_weights,
                stats=stats
            )
            
            # Log canonical payload BEFORE signing
            self.logger.info(
                f"📝 Canonical payload being signed:\n"
                f"--- Payload (repr - shows special chars) ---\n"
                f"{repr(canonical_payload)}\n"
                f"--- Payload (readable string) ---\n"
                f"{canonical_payload}\n"
                f"--- Payload (hex bytes) ---\n"
                f"{canonical_payload.encode('utf-8').hex()}\n"
                f"--- Payload length: {len(canonical_payload)} chars, {len(canonical_payload.encode('utf-8'))} bytes ---"
            )
            
            # Sign the payload if we have a signing keypair
            if self.signing_keypair:
                try:
                    # Verify keypair SS58 address matches validator_id
                    keypair_ss58 = None
                    if hasattr(self.signing_keypair, 'ss58_address'):
                        keypair_ss58 = self.signing_keypair.ss58_address
                    elif hasattr(self.signing_keypair, 'public_key'):
                        # Try to derive SS58 from public key if available
                        try:
                            import bittensor as bt
                            temp_keypair = bt.Keypair(ss58_address=self.validator_id)
                            keypair_ss58 = temp_keypair.ss58_address
                        except Exception:
                            pass
                    
                    # Log keypair info for debugging
                    self.logger.info(
                        f"🔐 Signing weights payload:\n"
                        f"   validator_id (from config): {self.validator_id}\n"
                        f"   keypair SS58 address: {keypair_ss58 or 'N/A'}\n"
                        f"   keypair matches validator_id: {keypair_ss58 == self.validator_id if keypair_ss58 else 'unknown'}"
                    )
                    
                    # Bittensor keypair.sign() - use generate.py pattern: sign(data=string) -> result.hex()
                    # This matches how generate.py signs messages with Bittensor keypairs
                    self.logger.info(f"🔑 Signing with Bittensor keypair using sign(data=string) pattern...")
                    signature_result = self.signing_keypair.sign(data=canonical_payload)
                    self.logger.debug(f"   Sign result type: {type(signature_result)}")
                    self.logger.debug(f"   Sign result attributes: {[a for a in dir(signature_result) if not a.startswith('_')]}")
                    
                    # Bittensor sign() returns an object with .hex() method (like generate.py)
                    # NOT .signature attribute (that's for nacl.signing.SigningKey used by miner)
                    if hasattr(signature_result, 'hex'):
                        signature_hex = signature_result.hex()
                        signature_bytes = bytes.fromhex(signature_hex)
                        self.logger.info(f"   ✓ Used .hex() method, signature hex length: {len(signature_hex)}, bytes length: {len(signature_bytes)}")
                    elif isinstance(signature_result, bytes):
                        signature_bytes = signature_result
                        self.logger.info(f"   ✓ Result is bytes directly, length: {len(signature_bytes)}")
                    else:
                        # Try to get bytes from the result
                        self.logger.warning(f"   Unexpected result type {type(signature_result)}, trying to extract bytes...")
                        if hasattr(signature_result, 'signature'):
                            signature_bytes = signature_result.signature
                            self.logger.info(f"   ✓ Found .signature attribute, length: {len(signature_bytes)}")
                        else:
                            raise ValueError(f"Cannot extract signature bytes from {type(signature_result)}")
                    
                    self.logger.info(f"   Final signature bytes length: {len(signature_bytes)} bytes")
                    
                    # Verify signature length (sr25519 signatures are 64 bytes)
                    if len(signature_bytes) != 64:
                        self.logger.error(
                            f"❌ Invalid signature length: {len(signature_bytes)} bytes "
                            f"(expected 64 for sr25519). Signature type: {type(signature_result)}"
                        )
                        return
                    
                    signature = "0x" + signature_bytes.hex()
                    signature_type = "sr25519"  # Bittensor uses sr25519
                    
                    # Verify signature locally before sending (if possible)
                    try:
                        import bittensor as bt
                        # Try to verify the signature
                        if hasattr(self.signing_keypair, 'verify'):
                            is_valid = self.signing_keypair.verify(
                                data=canonical_payload.encode('utf-8'),
                                signature=signature_bytes
                            )
                            self.logger.info(f"   Local signature verification: {'✓ VALID' if is_valid else '✗ INVALID'}")
                        else:
                            # Create a keypair from validator_id to verify
                            verify_keypair = bt.Keypair(ss58_address=self.validator_id)
                            is_valid = verify_keypair.verify(
                                data=canonical_payload.encode('utf-8'),
                                signature=signature_bytes
                            )
                            self.logger.info(f"   Local signature verification (using validator_id keypair): {'✓ VALID' if is_valid else '✗ INVALID'}")
                    except Exception as verify_err:
                        self.logger.debug(f"   Could not verify signature locally: {verify_err}")
                    
                    # Comprehensive debug logging
                    self.logger.info(
                        f"✍️  Signature generated:\n"
                        f"   signature (hex): {signature}\n"
                        f"   signature length: {len(signature_bytes)} bytes\n"
                        f"   signature type: {signature_type}\n"
                        f"   validator_id: {self.validator_id}"
                    )
                except Exception as e:
                    self.logger.error(f"❌ Failed to sign weights payload: {e}", exc_info=True)
                    return
            else:
                # No signing keypair available - generate a placeholder signature for testing
                # This allows weight submission even without a real keypair (e.g., pure simulation)
                self.logger.warning("No signing keypair available, using placeholder signature for weight submission")
                # Generate a deterministic placeholder signature based on payload hash
                import hashlib
                payload_hash = hashlib.sha256(canonical_payload.encode('utf-8')).digest()
                # Use first 64 bytes as placeholder signature (sr25519 signature length)
                placeholder_sig = payload_hash[:64] if len(payload_hash) >= 64 else payload_hash + b'\x00' * (64 - len(payload_hash))
                signature = "0x" + placeholder_sig.hex()
                signature_type = "sr25519"
            
            # Submit to aggregator
            response = await self.events_client.submit_weights(
                validator_id=self.validator_id,
                epoch_key=epoch_result.epoch_key,
                weights=sorted_weights,
                stats=stats,
                timestamp=timestamp,
                signature=signature,
                signature_type=signature_type,
                block_number=None
            )
            
            if response:
                submission_id = response.get("weightSubmissionId", "unknown")
                self.logger.info(f"✅ Weights submitted to aggregator: {submission_id}")
                # Mark epoch as submitted to prevent duplicates
                self._submitted_epochs.add(epoch_result.epoch_key)
            else:
                self.logger.warning("⚠️  Failed to submit weights to aggregator")
                
        except Exception as e:
            self.logger.error(f"Error submitting weights to aggregator: {e}", exc_info=True)

    async def run_epoch(
        self,
        epoch_key: str,
        duration_minutes: int = 5
    ) -> EpochResult:
        """Run a single epoch and return results."""
        self.logger.info(f"🔔 Starting epoch {epoch_key} ({duration_minutes} minutes)")

        # Set current epoch
        self._current_epoch_key = epoch_key
        self._epoch_results[epoch_key] = []

        # Collect validation results for the epoch duration
        start_time = datetime.now(timezone.utc)
        end_time = start_time.replace(second=0, microsecond=0)  # Align to minute boundary

        # Run validation for the epoch duration
        self.logger.info(f"⏳ Collecting validation results for {duration_minutes} minute(s)...")
        
        # Sleep in smaller chunks to show progress
        total_seconds = duration_minutes * 60
        check_interval = min(10, total_seconds // 6)  # Log every ~10 seconds or 6 times total
        elapsed = 0
        
        while elapsed < total_seconds:
            if self._heartbeat_callback:
                self._heartbeat_callback()
            sleep_time = min(check_interval, total_seconds - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time
            
            # Show progress
            current_count = len(self._epoch_results.get(epoch_key, []))
            progress_pct = int((elapsed / total_seconds) * 100)
            if current_count > 0:
                self.logger.info(f"   📊 [{progress_pct}%] Collected {current_count} validation(s) so far...")
            elif progress_pct % 25 == 0:  # Log every 25% when no orders found
                self.logger.info(f"   📭 [{progress_pct}%] No orders found yet, continuing to check...")

        # Get results for this epoch
        validation_results = self._epoch_results.get(epoch_key, [])
        if validation_results:
            self.logger.info(f"✅ Epoch collection complete: {len(validation_results)} validation(s) collected")
        else:
            self.logger.warning(f"⚠️  Epoch collection complete: No validations collected (no orders found during this epoch)")

        # Compute weights
        epoch_result = await self.compute_weights_for_epoch(epoch_key, validation_results)

        # Process results
        await self.process_epoch_results(epoch_result)

        # Clear epoch
        self._current_epoch_key = None

        return epoch_result

    def _run_background_validation(self):
        """Run validation loop in background thread."""
        async def validation_loop():
            self.logger.info(f"🔄 Background validation loop started (checking every {self.validation_interval_seconds}s)")
            
            # Perform initial health check immediately
            try:
                health_data = await self.events_client.fetch_health()
                if health_data:
                    status = health_data.get("status", "unknown")
                    storage = health_data.get("storage", {})
                    storage_healthy = storage.get("healthy", False)
                    is_healthy = status.lower() in ("healthy", "ok") and storage_healthy
                    self._aggregator_healthy = is_healthy
                    if not is_healthy:
                        self.logger.warning(f"⚠️  Initial aggregator health check: unhealthy (status={status}, storage={storage_healthy})")
                else:
                    self._aggregator_healthy = False
                    self.logger.warning("⚠️  Initial aggregator health check failed - weights will be set to 0")
                self._last_health_check = time.time()
            except Exception as e:
                self.logger.warning(f"⚠️  Initial aggregator health check error: {e} - weights will be set to 0")
                self._aggregator_healthy = False
            
            while self._validation_running:
                try:
                    current_time = time.time()
                    if self._heartbeat_callback:
                        self._heartbeat_callback()
                    
                    # Check aggregator health periodically
                    should_check_health = (
                        self._last_health_check is None or
                        current_time - self._last_health_check >= self._health_check_interval
                    )
                    
                    if should_check_health:
                        health_data = await self.events_client.fetch_health()
                        if health_data:
                            # Format health status for logging
                            status = health_data.get("status", "unknown")
                            version = health_data.get("version", "unknown")
                            solvers = health_data.get("solvers", {})
                            storage = health_data.get("storage", {})
                            
                            # Determine if aggregator is healthy
                            # Aggregator is healthy if status is "healthy" or "ok", and storage is healthy
                            storage_healthy = storage.get("healthy", False)
                            is_healthy = status.lower() in ("healthy", "ok") and storage_healthy
                            self._aggregator_healthy = is_healthy
                            
                            solver_summary = (
                                f"total={solvers.get('total', 0)}, "
                                f"active={solvers.get('active', 0)}, "
                                f"healthy={solvers.get('healthy', 0)}, "
                                f"unhealthy={solvers.get('unhealthy', 0)}"
                            )
                            storage_status = "healthy" if storage_healthy else "unhealthy"
                            
                            # Log health summary at INFO level
                            health_emoji = "✅" if is_healthy else "⚠️"
                            self.logger.info(
                                f"{health_emoji} Aggregator health: status={status}, version={version}, "
                                f"solvers({solver_summary}), storage={storage_status}"
                            )
                            
                            if not is_healthy:
                                self.logger.warning(f"⚠️  Aggregator is unhealthy - weights will be set to 0 (100% burn)")
                            
                            # Log detailed health info at debug level
                            self.logger.debug(f"   Full health data: {health_data}")
                        else:
                            # Health check failed - mark as unhealthy
                            self._aggregator_healthy = False
                            self.logger.warning(f"🏥 Aggregator health check returned no data (health_data={health_data})")
                            self.logger.warning(f"⚠️  Aggregator health check failed - weights will be set to 0 (100% burn)")
                        self._last_health_check = current_time
                    
                    # Run validation and store results in current epoch
                    validation_results = await self.fetch_and_validate_orders()

                    if validation_results and self._current_epoch_key:
                        if self._current_epoch_key not in self._epoch_results:
                            self._epoch_results[self._current_epoch_key] = []
                        self._epoch_results[self._current_epoch_key].extend(validation_results)
                        # Reset no-orders counter when we find orders
                        self._no_orders_check_count = 0
                    elif not validation_results:
                        # Log when no orders are found, but throttle to avoid spam
                        self._no_orders_check_count += 1
                        
                        # Log every 10 checks (or every 60 seconds, whichever comes first)
                        should_log = False
                        if self._last_no_orders_log is None:
                            should_log = True
                        elif current_time - self._last_no_orders_log >= 60:
                            should_log = True
                        elif self._no_orders_check_count >= 10:
                            should_log = True
                        
                        if should_log:
                            if self._current_epoch_key:
                                self.logger.info(f"📭 No pending orders found (epoch: {self._current_epoch_key})")
                            else:
                                self.logger.info(f"📭 No pending orders found (waiting for epoch to start)")
                            self._last_no_orders_log = current_time
                            self._no_orders_check_count = 0

                except Exception as e:
                    self.logger.error(f"Error in background validation: {e}")

                # Sleep between validation checks
                await asyncio.sleep(self.validation_interval_seconds)

        # Run the async validation loop
        asyncio.run(validation_loop())
        self.logger.info("Background validation loop stopped")

    async def start_continuous_validation(self):
        """Start continuous background validation."""
        if self._validation_running:
            return

        self._validation_running = True
        self._validation_thread = threading.Thread(
            target=self._run_background_validation,
            daemon=True
        )
        self._validation_thread.start()
        self.logger.info("Started continuous validation loop")

    async def stop_continuous_validation(self):
        """Stop continuous background validation."""
        if not self._validation_running:
            return

        self._validation_running = False
        if self._validation_thread:
            self._validation_thread.join(timeout=2)
        self.logger.info("Stopped continuous validation loop")

    async def run_continuous_epochs(self, epoch_minutes: int = 5):
        """Run continuous epochs indefinitely."""
        await self.start_continuous_validation()

        try:
            epoch_count = 0
            while True:
                epoch_count += 1
                epoch_key = f"epoch-{epoch_count}-{datetime.now(timezone.utc).isoformat()}"

                await self.run_epoch(epoch_key, epoch_minutes)

        except KeyboardInterrupt:
            self.logger.info("Continuous epochs interrupted")
        finally:
            await self.stop_continuous_validation()
