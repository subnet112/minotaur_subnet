"""Protocol types for the benchmarking harness JSON communication.

Defines the message format for communication between the host-side
orchestrator and in-container runner via stdin/stdout. Follows the same
pattern as engine/sandbox.py (JSON-over-stdio) but with a richer
command set for the IntentSolver lifecycle.

Protocol:
    - Host sends one JSON object per line on stdin (newline-delimited)
    - Container responds with one JSON object per line on stdout
    - Each command gets exactly one response
    - stderr is captured for logging but does not affect scoring

Message format:
    Request:  {"command": "<name>", ...params}
    Response: {"success": true, "result": ...} or
              {"success": false, "error": "...", "error_type": "..."}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot


class Command(str, Enum):
    """Commands the host can send to the solver container."""
    INITIALIZE = "initialize"
    GENERATE_PLAN = "generate_plan"
    CHECK_TRIGGER = "check_trigger"
    ON_BENCHMARK_START = "on_benchmark_start"
    ON_BENCHMARK_END = "on_benchmark_end"
    SERIALIZE_STATE = "serialize_state"
    RESTORE_STATE = "restore_state"
    METADATA = "metadata"
    QUOTE = "quote"
    SHUTDOWN = "shutdown"


# ═══════════════════════════════════════════════════════════════════════════════
#                          TIMEOUT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Per-command timeout in seconds
TIMEOUTS: dict[str, float] = {
    Command.INITIALIZE: 60.0,
    Command.GENERATE_PLAN: 30.0,
    Command.CHECK_TRIGGER: 10.0,
    Command.ON_BENCHMARK_START: 10.0,
    Command.ON_BENCHMARK_END: 30.0,
    Command.SERIALIZE_STATE: 30.0,
    Command.RESTORE_STATE: 30.0,
    Command.METADATA: 5.0,
    Command.QUOTE: 5.0,
    Command.SHUTDOWN: 5.0,
}

# Hard cap on total container lifetime. Sized to comfortably accommodate
# the worst-case plan-generation cost: ~20 scenarios × 30s/plan = 600s,
# leaving 300s for per-scenario simulation, scoring, and misc overhead.
# Benchmark throughput is bounded by this cap and by anvil-base capacity;
# raising this further requires provisioning more per-validator Anvil
# forks before benchmark concurrency becomes the bottleneck.
TOTAL_BENCHMARK_TIMEOUT = 900.0  # 15 minutes


# ═══════════════════════════════════════════════════════════════════════════════
#                          REQUEST / RESPONSE
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class HarnessRequest:
    """A command from the host to the solver container."""
    command: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to a single JSON line."""
        msg = {"command": self.command, **self.params}
        return json.dumps(msg, default=_json_default)

    @classmethod
    def from_json(cls, line: str) -> HarnessRequest:
        """Parse a JSON line into a request."""
        data = json.loads(line)
        command = data.pop("command")
        return cls(command=command, params=data)


@dataclass
class HarnessResponse:
    """A response from the solver container to the host."""
    success: bool
    result: Any = None
    error: str | None = None
    error_type: str | None = None

    def to_json(self) -> str:
        """Serialize to a single JSON line."""
        msg: dict[str, Any] = {"success": self.success}
        if self.success:
            msg["result"] = self.result
        else:
            msg["error"] = self.error or "Unknown error"
            msg["error_type"] = self.error_type or "RuntimeError"
        return json.dumps(msg, default=_json_default)

    @classmethod
    def from_json(cls, line: str) -> HarnessResponse:
        """Parse a JSON line into a response."""
        data = json.loads(line)
        return cls(
            success=data["success"],
            result=data.get("result"),
            error=data.get("error"),
            error_type=data.get("error_type"),
        )

    @classmethod
    def ok(cls, result: Any = None) -> HarnessResponse:
        """Create a success response."""
        return cls(success=True, result=result)

    @classmethod
    def fail(cls, error: str, error_type: str = "RuntimeError") -> HarnessResponse:
        """Create an error response."""
        return cls(success=False, error=error, error_type=error_type)


# ═══════════════════════════════════════════════════════════════════════════════
#                          REQUEST BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════


def make_initialize_request(config: dict[str, Any]) -> HarnessRequest:
    """Build an initialize command."""
    return HarnessRequest(command=Command.INITIALIZE, params={"config": config})


def make_generate_plan_request(
    intent: AppIntentDefinition,
    state: IntentState,
    snapshot: MarketSnapshot,
) -> HarnessRequest:
    """Build a generate_plan command."""
    return HarnessRequest(
        command=Command.GENERATE_PLAN,
        params={
            "intent": _to_dict(intent),
            "state": _to_dict(state),
            "snapshot": _to_dict(snapshot),
        },
    )


def make_check_trigger_request(
    intent: AppIntentDefinition,
    state: IntentState,
    snapshot: MarketSnapshot,
) -> HarnessRequest:
    """Build a check_trigger command."""
    return HarnessRequest(
        command=Command.CHECK_TRIGGER,
        params={
            "intent": _to_dict(intent),
            "state": _to_dict(state),
            "snapshot": _to_dict(snapshot),
        },
    )


def make_benchmark_start_request(intent_count: int) -> HarnessRequest:
    """Build an on_benchmark_start command."""
    return HarnessRequest(
        command=Command.ON_BENCHMARK_START,
        params={"intent_count": intent_count},
    )


def make_benchmark_end_request(
    results: list[dict[str, Any]],
) -> HarnessRequest:
    """Build an on_benchmark_end command."""
    return HarnessRequest(
        command=Command.ON_BENCHMARK_END,
        params={"results": results},
    )


def make_serialize_state_request() -> HarnessRequest:
    """Build a serialize_state command."""
    return HarnessRequest(command=Command.SERIALIZE_STATE)


def make_restore_state_request(state_b64: str) -> HarnessRequest:
    """Build a restore_state command with base64-encoded state."""
    return HarnessRequest(
        command=Command.RESTORE_STATE,
        params={"state_b64": state_b64},
    )


def make_metadata_request() -> HarnessRequest:
    """Build a metadata command."""
    return HarnessRequest(command=Command.METADATA)


def make_quote_request(
    intent: AppIntentDefinition,
    state: IntentState,
    snapshot: MarketSnapshot,
) -> HarnessRequest:
    """Build a quote command."""
    return HarnessRequest(
        command=Command.QUOTE,
        params={
            "intent": _to_dict(intent),
            "state": _to_dict(state),
            "snapshot": _to_dict(snapshot),
        },
    )


def make_shutdown_request() -> HarnessRequest:
    """Build a shutdown command."""
    return HarnessRequest(command=Command.SHUTDOWN)


# ═══════════════════════════════════════════════════════════════════════════════
#                          RESPONSE PARSERS
# ═══════════════════════════════════════════════════════════════════════════════


def parse_plan_response(response: HarnessResponse) -> ExecutionPlan | None:
    """Parse a generate_plan response into an ExecutionPlan."""
    if not response.success or response.result is None:
        return None

    r = response.result
    interactions = [
        Interaction(
            target=ix["target"],
            value=ix["value"],
            call_data=ix.get("call_data", ix.get("callData", "")),
            chain_id=ix.get("chain_id", ix.get("chainId", 1)),
        )
        for ix in r.get("interactions", [])
    ]

    return ExecutionPlan(
        intent_id=r["intent_id"],
        interactions=interactions,
        deadline=r["deadline"],
        nonce=r["nonce"],
        metadata=r.get("metadata", {}),
    )


def parse_quote_response(response: HarnessResponse) -> "QuoteResult | None":
    """Parse a quote response into a QuoteResult."""
    from minotaur_subnet.shared.types import QuoteResult

    if not response.success or response.result is None:
        return None

    r = response.result
    return QuoteResult(
        estimated_output=r.get("estimated_output", "0"),
        computed_params=r.get("computed_params", {}),
        route_summary=r.get("route_summary", ""),
        gas_estimate=r.get("gas_estimate", 0),
        metadata=r.get("metadata", {}),
        platform_fee_wei=r.get("platform_fee_wei", "0"),
        platform_fee_token=r.get("platform_fee_token", ""),
        platform_fee_symbol=r.get("platform_fee_symbol", ""),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          SERIALIZATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _strip_sensitive_fields(intent_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove scoring/source code from an AppIntentDefinition dict.

    Solvers only need structural metadata (app_id, name, config, manifest,
    deployer, description) to generate execution plans. Sending js_code or
    solidity_code would let miners reverse-engineer scoring functions and
    game benchmarks.
    """
    sensitive_keys = {"js_code", "solidity_code", "js_code_hash"}
    return {k: v for k, v in intent_dict.items() if k not in sensitive_keys}


def _to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass to a dict, handling nested structures.

    If the object is an AppIntentDefinition, sensitive fields (js_code,
    solidity_code, js_code_hash) are stripped to prevent solver containers
    from accessing scoring code.
    """
    if hasattr(obj, "__dataclass_fields__"):
        d = asdict(obj)
        # Strip scoring code from AppIntentDefinition before sending to solvers
        if isinstance(obj, AppIntentDefinition):
            d = _strip_sensitive_fields(d)
        return d
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"Cannot convert {type(obj).__name__} to dict")


def _json_default(obj: Any) -> Any:
    """JSON serializer fallback for non-standard types."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        import base64
        return base64.b64encode(obj).decode("ascii")
    return str(obj)


def dict_to_intent(d: dict[str, Any]) -> AppIntentDefinition:
    """Reconstruct an AppIntentDefinition from a dict (received over protocol)."""
    from minotaur_subnet.shared.types import AppIntentConfig, TriggerType

    config_d = d.get("config", {})
    trigger_raw = config_d.get("trigger_type", "user_triggered")
    trigger_type = TriggerType(trigger_raw) if isinstance(trigger_raw, str) else TriggerType.USER_TRIGGERED

    config = AppIntentConfig(
        supported_chains=config_d.get("supported_chains", [1]),
        score_threshold=config_d.get("score_threshold", 0.5),
        on_chain_threshold=config_d.get("on_chain_threshold", 5000),
        trigger_type=trigger_type,
        max_gas=config_d.get("max_gas", 500_000),
    )

    return AppIntentDefinition(
        app_id=d["app_id"],
        name=d.get("name", ""),
        version=d.get("version", ""),
        intent_type=d.get("intent_type", ""),
        js_code=d.get("js_code", ""),
        solidity_code=d.get("solidity_code"),
        config=config,
        deployer=d.get("deployer", ""),
        description=d.get("description", ""),
    )


def dict_to_state(d: dict[str, Any]) -> IntentState:
    """Reconstruct an IntentState from a dict."""
    from minotaur_subnet.shared.types import PolicyTier
    from minotaur_subnet.v3.contexts import typed_context_from_dict

    legacy_extra = d.get("extra", {})
    legacy_raw, legacy_control = IntentState._split_extra(legacy_extra)
    state = IntentState(
        contract_address=d["contract_address"],
        chain_id=d.get("chain_id", 1),
        nonce=d.get("nonce", 0),
        owner=d.get("owner", ""),
        raw_params=d.get("raw_params", legacy_raw),
        control=d.get("control", legacy_control),
        context_version=d.get("context_version", "v2"),
        policy_tier=PolicyTier(d.get("policy_tier", PolicyTier.HYBRID.value)),
    )
    state.typed_context = typed_context_from_dict(d.get("typed_context"))
    return state


def dict_to_snapshot(d: dict[str, Any]) -> MarketSnapshot:
    """Reconstruct a MarketSnapshot from a dict."""
    return MarketSnapshot(
        chain_id=d["chain_id"],
        block_number=d["block_number"],
        timestamp=d["timestamp"],
        prices=d.get("prices", {}),
        pool_states=d.get("pool_states", {}),
        balances=d.get("balances", {}),
        dex_config=d.get("dex_config", {}),
        raw_state=d.get("raw_state", {}),
    )
