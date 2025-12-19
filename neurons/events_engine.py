"""Order-based weights computation engine with simulation validation.

NOTE: This module is legacy code and is only used in tests. The production code uses
ValidationEngine instead. This file is kept for backward compatibility with existing tests.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple, Set

from .aggregator_client import AggregatorClient, AggregatorClientError
from .simulator import OrderSimulator, SimulationError
from .state_store import StateStore


class EventsWeightsEngine:
    """Engine for computing weights based on order validation results."""

    def __init__(
        self,
        events_client: AggregatorClient,
        state_store: StateStore,
        validator_id: str,
        logger: Optional[logging.Logger] = None,
        simulator: Optional[OrderSimulator] = None,
        burn_percentage: float = 0.0,
        creator_hotkey: Optional[str] = None,
    ):
        self.events_client = events_client
        self.state_store = state_store
        self.validator_id = validator_id
        self.logger = logger or logging.getLogger(__name__)
        self.simulator = simulator or OrderSimulator()
        self.burn_percentage = burn_percentage
        self.creator_hotkey = creator_hotkey

        # Cache for solver_id -> miner_id mapping
        self._solver_to_miner_cache: Dict[str, Optional[str]] = {}
        self._miners_stats_cache: Optional[List[dict]] = None

    async def _refresh_miners_cache(self) -> bool:
        """Fetch miners stats and populate solver->miner cache. Returns True if successful."""
        try:
            miners_stats = await self.events_client.fetch_miners_stats()
            if miners_stats is None:
                return False

            self._miners_stats_cache = miners_stats

            # Build solver_id -> miner_id mapping
            self._solver_to_miner_cache.clear()
            for miner in miners_stats:
                miner_id = miner.get("minerId")
                solver_ids = miner.get("solverIds", [])
                for solver_id in solver_ids:
                    self._solver_to_miner_cache[solver_id] = miner_id

            return True
        except Exception as e:
            self.logger.error(f"Error fetching miners stats: {e}")
            return False

    def get_miner_for_solver(self, solver_id: str) -> Optional[str]:
        """Get miner_id for a solver_id using cached miners stats."""
        # Check cache first
        if solver_id in self._solver_to_miner_cache:
            return self._solver_to_miner_cache[solver_id]

        # Solver not found in any miner
        # Cache the None result to avoid repeated lookups
        self._solver_to_miner_cache[solver_id] = None
        return None

    async def fetch_and_validate_orders(self) -> List[Tuple[str, str, bool]]:
        """Fetch pending orders and run simulations to validate them.

        Returns:
            List of (order_id, solver_id, success) tuples
        """
        try:
            # Fetch pending orders for this validator
            orders = await self.events_client.fetch_pending_orders(self.validator_id)
            if not orders:
                return []

            self.logger.info(f"🧪 Running simulations for {len(orders)} pending orders...")

            results = []
            for order in orders:
                order_id = order.get("orderId")
                if not order_id:
                    continue

                # Extract solver_id from quoteDetails
                quote_details = order.get("quoteDetails", {})
                solver_id = quote_details.get("solverId")
                if not solver_id:
                    self.logger.warning(f"⚠️  Order {order_id} missing solverId in quoteDetails, skipping")
                    continue

                # Simulate the order
                success, error_message = await self.simulator.simulate_order(order)

                # Submit validation result
                notes = "Simulation succeeded" if success else f"Simulation failed: {error_message or 'Unknown error'}"
                await self.events_client.submit_validation(order_id, success, notes)

                if success:
                    self.logger.info(f"✅ Order {order_id} validated successfully")
                else:
                    self.logger.warning(f"❌ Order {order_id} validation failed: {error_message}")

                results.append((order_id, solver_id, success))

            return results

        except Exception as e:
            self.logger.error(f"Error in fetch_and_validate_orders: {e}")
            return []

    async def compute_weights_for_epoch(
        self,
        epoch_key: str,
        simulation_results: List[Tuple[str, str, bool]]
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, int]]:
        """Compute weights for an epoch based on simulation validation results.

        Args:
            epoch_key: Identifier for this epoch
            simulation_results: List of (order_id, solver_id, success) tuples

        Returns:
            (weights, scores, stats)
        """
        if not simulation_results:
            self.logger.warning(f"No simulation results for epoch {epoch_key}")
            return {}, {}, {"total_simulations": 0, "valid_miners": 0}

        # Refresh miners cache to ensure we have latest data
        await self._refresh_miners_cache()

        # Aggregate results by miner
        miner_validated: Dict[str, int] = defaultdict(int)
        miner_total: Dict[str, int] = defaultdict(int)

        self.logger.info(f"📊 Processing {len(simulation_results)} simulation results...")

        for order_id, solver_id, success in simulation_results:
            # Get miner_id for this solver
            miner_id = self.get_miner_for_solver(solver_id)

            if miner_id is None:
                # Skip orders from solvers without miner mapping
                continue

            miner_total[miner_id] += 1
            if success:
                miner_validated[miner_id] += 1

        # Compute scores: validated orders count (simple and reliable)
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

        # Normalize to weights
        total_score = sum(data["score"] for data in scores.values())
        if total_score == 0:
            # Equal distribution if no activity
            weights = {miner_id: 1.0 / len(scores) for miner_id in scores.keys()}
        else:
            weights = {miner_id: data["score"] / total_score for miner_id, data in scores.items()}

        # Apply burn allocation if specified
        if self.burn_percentage > 0.0 and self.creator_hotkey:
            # Scale down legitimate miner weights
            allocate_percentage = 1.0 - self.burn_percentage
            for miner_id in weights:
                weights[miner_id] *= allocate_percentage

            # Allocate burn percentage to creator
            if self.creator_hotkey in weights:
                weights[self.creator_hotkey] += self.burn_percentage
            else:
                # Add creator to weights if not present
                weights[self.creator_hotkey] = self.burn_percentage

            self.logger.info(
                f"Burn allocation applied: {self.burn_percentage:.1%} to creator hotkey {self.creator_hotkey[:8]}...",
                prefix="BURN"
            )

        stats = {
            "total_simulations": len(simulation_results),
            "valid_miners": len(scores),
            "total_miners": len(miner_total),
            "burn_percentage": self.burn_percentage
        }

        return weights, scores, stats

    async def compute_weights_for_window(
        self,
        from_ts: str,
        to_ts: str,
        allowed_hotkeys: Set[str],
        burn_percentage: float = 0.0,
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, int]]:
        """Legacy method for compatibility - now fetches orders and simulates them."""
        # For backward compatibility, we simulate the old interface
        # In the new model, weights are computed per epoch from simulation results
        simulation_results = await self.fetch_and_validate_orders()

        # Create a dummy epoch key for this window
        epoch_key = f"window-{from_ts}-{to_ts}"

        return await self.compute_weights_for_epoch(epoch_key, simulation_results)


