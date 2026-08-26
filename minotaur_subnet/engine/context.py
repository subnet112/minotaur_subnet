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
    # The platform's destination-leg delivery measurement for a multi-leg
    # plan (benchmark path only — see SimulationResult). The amount stays a
    # DECIMAL STRING end-to-end: it is wei that can exceed 2^53, and the
    # scorer must never see it as a lossy JS Number. Absent for single-leg
    # plans and off the benchmark path, so a scorer that ignores it is
    # bit-identical to today.
    if getattr(sim, "destination_delivered", None) is not None:
        result["destination_delivered"] = str(sim.destination_delivered)
        result["destinationDelivered"] = result["destination_delivered"]
    if getattr(sim, "destination_amount_source", None) is not None:
        result["destination_amount_source"] = str(sim.destination_amount_source)
        result["destinationAmountSource"] = result["destination_amount_source"]
    # The App's OWN verdict, as returned by its on-chain scoreIntent (BPS,
    # 0..10000). The counterpart to token_transfers: one channel for "what
    # moved", one for "what the App ruled".
    #
    # Absent until now, which made a whole App shape unscoreable. The DEX
    # aggregator measures an OUTCOME — its plan executes real calls, tokens
    # move, and the scorer reads token_transfers — so it never needed this and
    # nobody noticed it was missing. An App whose plan is DATA rather than code
    # has no such observable: AlphaYieldApp (chain 964) executes no solver calls
    # at all, so token_transfers and state_changes are both legitimately empty
    # and its scoreIntent return is the ONLY signal that exists. Its scorer read
    # `sim.score`, got undefined, and fell back to a flat 0.15 floor while the
    # contract was returning a correctly graded verdict (measured 2026-08-26:
    # 10000 / 1427 / 588 / 21 / 0 across the five allowlisted candidates,
    # matching survey() exactly).
    #
    # Additive and inert for existing scorers: the DEX references neither
    # `on_chain_score` nor `score`, so its output is bit-identical.
    if getattr(sim, "on_chain_score", None) is not None:
        result["on_chain_score"] = int(sim.on_chain_score)
        result["onChainScore"] = result["on_chain_score"]
        # `score` is the name an App scorer reaches for first (and what
        # AlphaYieldApp's `pick(sim, "score")` already looks for).
        result["score"] = result["on_chain_score"]
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
