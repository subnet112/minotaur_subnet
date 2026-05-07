"""
JsContext - Builds the context object provided to JS scoring functions.

For the MVP, oracle/simulator/state APIs are stubbed; the real
simulation result is passed directly.
"""

import time
from dataclasses import asdict, is_dataclass
from typing import Any

from minotaur_subnet.shared.types import IntentState, SimulationResult


class JsContext:
    """Builds the context dict that JS scoring functions receive.

    The context matches the AppIntentContext TypeScript interface, with API
    objects stubbed for the MVP. The simulation result and intent state are
    injected directly as data (not callable APIs) since JS scoring functions
    in the MVP access them as plain objects.
    """

    def __init__(self, chain_id: int, contract_address: str):
        self.chain_id = chain_id
        self.contract_address = contract_address

    def build_context(
        self,
        simulation: SimulationResult,
        state: IntentState,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Build the context dict that gets injected into JS execution.

        Returns a dict matching the AppIntentContext TypeScript interface:
        {
            "oracle": { ... },          # Oracle API (stubbed for MVP)
            "simulator": { ... },       # NOT the API - just the result data
            "simulation": { ... },      # Direct simulation result for easy access
            "state": { ... },           # On-chain intent state
            "timestamp": int,
            "blockNumber": int,
            "chainId": int,
            "contractAddress": str,
        }
        """
        now = timestamp if timestamp is not None else int(time.time())

        # Convert simulation result to a plain dict for JS consumption.
        # Use snake_case keys matching the Python dataclass - the JS code
        # in the scoring functions accesses these directly.
        sim_dict = _simulation_to_dict(simulation)

        # Convert intent state to a plain dict.
        state_dict = _state_to_dict(state)

        return {
            # Oracle data (populated by the validator before scoring).
            # Scoring code can also use the global ethCall() and httpGet()
            # functions to independently verify on-chain state.
            "oracle": {},
            "simulator": sim_dict,
            # Direct simulation result - primary way scoring code accesses it
            "simulation": sim_dict,
            # Intent state
            "state": state_dict,
            # Block/time context
            "timestamp": now,
            "blockNumber": 0,  # Would come from chain in production
            "chainId": self.chain_id,
            "contractAddress": self.contract_address,
        }


def _simulation_to_dict(sim: SimulationResult) -> dict[str, Any]:
    """Convert a SimulationResult to a plain dict suitable for JSON serialization."""
    result = {
        "success": sim.success,
        "gas_used": sim.gas_used,
        "gasUsed": sim.gas_used,  # Also provide camelCase for JS convention
    }
    if sim.error is not None:
        result["error"] = sim.error
    if sim.token_transfers:
        result["token_transfers"] = [asdict(t) for t in sim.token_transfers]
        result["tokenTransfers"] = result["token_transfers"]
    else:
        result["token_transfers"] = []
        result["tokenTransfers"] = []
    if sim.state_changes:
        result["state_changes"] = sim.state_changes
        result["stateChanges"] = sim.state_changes
    else:
        result["state_changes"] = []
        result["stateChanges"] = []
    if sim.approval_changes:
        result["approval_changes"] = sim.approval_changes
    else:
        result["approval_changes"] = []
    if sim.price_impact is not None:
        result["price_impact"] = sim.price_impact
        result["priceImpact"] = sim.price_impact
    return result


def _state_to_dict(state: IntentState) -> dict[str, Any]:
    """Convert an IntentState to a plain dict suitable for JSON serialization."""
    result = {
        "contract_address": state.contract_address,
        "contractAddress": state.contract_address,
        "chain_id": state.chain_id,
        "chainId": state.chain_id,
        "nonce": state.nonce,
        "owner": state.owner,
        "raw_params": state.raw_params_view(),
        "rawParams": state.raw_params_view(),
        "control": state.control_view(),
    }
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        if is_dataclass(typed):
            result["typed_context"] = asdict(typed)
        elif hasattr(typed, "__dict__"):
            result["typed_context"] = dict(typed.__dict__)
    return result
