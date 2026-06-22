"""
Shared data types for the App Intents system.

These types define the contracts between all components:
- MCP server (user-facing tools)
- Blockchain layer (wallets, contracts, execution)
- JsExecutionEngine (scoring on validators)
- IntentProcessor SDK (miner-submitted solving code)

All agents MUST use these types for interoperability.
"""

import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#                              ENUMS
# ═══════════════════════════════════════════════════════════════════════════════


class TriggerType(str, Enum):
    """How an intent gets initiated."""
    USER_TRIGGERED = "user_triggered"      # User explicitly requests (e.g., swap)
    AUTO_TRIGGERED = "auto_triggered"      # Subnet monitors and triggers (e.g., rebalance)


class PolicyTier(str, Enum):
    """Execution policy tier for apps, wallets, and orders."""
    STRICT = "strict"
    HYBRID = "hybrid"
    EXPERT = "expert"


class NativeBittensorAction(str, Enum):
    """Supported delegated native Bittensor staking actions."""
    ADD_STAKE = "add_stake"
    MOVE_STAKE = "move_stake"
    REMOVE_STAKE = "remove_stake"


class NativeBittensorPermissionStatus(str, Enum):
    """Lifecycle state of a native Bittensor delegated permission."""
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"
    EXPIRED = "expired"


class NativeBittensorExecutionStatus(str, Enum):
    """Execution state for a delegated native Bittensor action."""
    PENDING = "pending"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class AppStatus(str, Enum):
    """Lifecycle status of a deployed App Intent."""
    DRAFT = "draft"              # Created but not deployed
    DEPLOYING = "deploying"      # Deployment in progress
    SOLVING = "solving"          # Deployed, awaiting solver support
    SOLVED = "solved"            # Solver proven via benchmark
    ACTIVE = "active"            # Legacy — treated same as SOLVED
    PARTIAL = "partial"          # Some chains deployed, some not yet
    PAUSED = "paused"            # Temporarily stopped
    RETIRED = "retired"          # Permanently stopped

    def is_operational(self) -> bool:
        """Can this app be loaded for benchmarking/solving?"""
        return self in (AppStatus.SOLVING, AppStatus.SOLVED, AppStatus.ACTIVE)

    def is_order_ready(self) -> bool:
        """Can users submit orders for this app?"""
        return self in (AppStatus.SOLVED, AppStatus.ACTIVE)


class IntentInstanceStatus(str, Enum):
    """Status of an intent instance (a single user request)."""
    PENDING = "pending"          # Submitted, waiting for processing
    PROCESSING = "processing"    # IntentProcessor generating plans
    SCORING = "scoring"          # Plans being scored by JS engine
    APPROVED = "approved"        # Plan approved, awaiting execution
    EXECUTING = "executing"      # On-chain execution in progress
    COMPLETED = "completed"      # Successfully executed
    FAILED = "failed"            # Execution failed
    EXPIRED = "expired"          # Deadline passed


# ═══════════════════════════════════════════════════════════════════════════════
#                          EXECUTION TYPES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class Interaction:
    """A single on-chain call in an execution plan."""
    target: str          # Contract address (0x...)
    value: str           # Wei value as decimal string ("0" for no ETH)
    call_data: str       # Encoded calldata (0x...)
    chain_id: int = 0    # Target chain (must be set explicitly)


# Bridge protocol call selectors that can't be simulated on Anvil forks.
# DEPRECATED: Use mock_bridge_interactions_from_config() with adapter.mock_config() instead.
# Kept for backward compatibility with legacy cross-chain path.
_BRIDGE_CALL_SELECTORS = {
    "81b4e8b4",  # Hyperlane transferRemote(uint32,bytes32,uint256)
}
_MOCK_BRIDGE_TARGET = "0x" + "BB" * 20


def mock_bridge_interactions(
    interactions: list["Interaction"],
    token_address: str = "",
    amount: int = 0,
) -> list["Interaction"]:
    """Replace bridge protocol calls with mock ERC-20 transfers.

    Used during simulation — bridge contracts can't execute on Anvil forks
    because bridge infrastructure (relayers, validators) doesn't exist.
    The mock transfer satisfies the contract's invariant (tokens left proxy)
    without actually calling the bridge protocol.

    Returns a new list; original is not modified.
    """
    result = []
    for ix in interactions:
        cd = ix.call_data or ""
        # Extract 4-byte selector from calldata
        raw = cd[2:] if cd.startswith("0x") else cd
        selector = raw[:8] if len(raw) >= 8 else ""

        if selector in _BRIDGE_CALL_SELECTORS:
            # Replace with: token.transfer(mockBridge, amount)
            if not amount:
                logger.warning("mock_bridge_interactions: no amount provided, mock may be inaccurate")
            from eth_abi import encode as _enc
            mock_cd = "0x" + "a9059cbb" + _enc(
                ["address", "uint256"],
                [_MOCK_BRIDGE_TARGET, amount if amount else 0],
            ).hex()
            result.append(Interaction(
                target=token_address or ix.target,
                value="0",
                call_data=mock_cd,
                chain_id=ix.chain_id,
            ))
        else:
            result.append(ix)
    return result


def mock_bridge_interactions_from_config(
    interactions: list["Interaction"],
    mock_config: dict[str, Any],
) -> list["Interaction"]:
    """Replace bridge calls based on adapter-provided mock configuration.

    Uses the adapter's mock_config() instead of hardcoded selectors.
    Each bridge adapter defines its own selectors and mock behavior.

    Args:
        interactions: Original interactions from the plan.
        mock_config: From adapter.mock_config(quote), containing:
            - selectors: list[str] — 4-byte hex selectors to replace
            - mock_type: "erc20_transfer" | "noop"
            - mock_token: str — token address for mock transfer
            - mock_amount: int — amount for mock transfer
    """
    selectors = set(mock_config.get("selectors", []))
    if not selectors:
        return list(interactions)

    mock_type = mock_config.get("mock_type", "noop")
    mock_token = mock_config.get("mock_token", "")
    mock_amount = mock_config.get("mock_amount", 0)

    result = []
    for ix in interactions:
        raw = (ix.call_data or "")[2:] if (ix.call_data or "").startswith("0x") else (ix.call_data or "")
        selector = raw[:8] if len(raw) >= 8 else ""

        if selector in selectors and mock_type == "erc20_transfer":
            from eth_abi import encode as _enc
            mock_cd = "0x" + "a9059cbb" + _enc(
                ["address", "uint256"],
                [_MOCK_BRIDGE_TARGET, mock_amount],
            ).hex()
            result.append(Interaction(
                target=mock_token or ix.target,
                value="0",
                call_data=mock_cd,
                chain_id=ix.chain_id,
            ))
        else:
            result.append(ix)
    return result


@dataclass
class ExecutionPlan:
    """Complete execution plan submitted by solvers / generated by IntentProcessor."""
    intent_id: str                           # Which intent this plan fulfills
    interactions: list[Interaction]           # Ordered calls to execute
    deadline: int                            # Unix timestamp - plan expires after
    nonce: int                               # Replay protection
    metadata: dict[str, Any] = field(default_factory=dict)  # App-specific data


@dataclass
class LegPlan:
    """A single leg in a multi-leg intent execution.

    Each leg represents one transaction on one chain. Legs execute
    sequentially; the platform enforces ordering. Both forward legs
    (achieving the desired outcome) and rollback legs (recovering from
    failure) use this same structure.
    """
    leg_index: int                     # Position in execution sequence
    chain_id: int                      # Which chain this leg executes on
    intent_selector: str               # 4-byte hex selector for this leg's intent function
    intent_params_hex: str             # ABI-encoded params for this leg
    interactions: list[Interaction]    # Plan calls for this leg
    depends_on: list[int] = field(default_factory=list)  # Leg indices that must complete first
    rollback_for: int | None = None    # Which forward leg this rolls back (None = forward leg)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_index": self.leg_index,
            "chain_id": self.chain_id,
            "intent_selector": self.intent_selector,
            "intent_params_hex": self.intent_params_hex,
            "interactions": [
                {"target": ix.target, "value": ix.value, "call_data": ix.call_data, "chain_id": ix.chain_id}
                for ix in self.interactions
            ],
            "depends_on": self.depends_on,
            "rollback_for": self.rollback_for,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LegPlan":
        return cls(
            leg_index=d["leg_index"],
            chain_id=d["chain_id"],
            intent_selector=d.get("intent_selector", ""),
            intent_params_hex=d.get("intent_params_hex", ""),
            interactions=[
                Interaction(target=ix["target"], value=ix["value"], call_data=ix["call_data"], chain_id=ix.get("chain_id", 0))
                for ix in d.get("interactions", [])
            ],
            depends_on=d.get("depends_on", []),
            rollback_for=d.get("rollback_for"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class MultiLegPlan:
    """Complete multi-leg execution plan with forward and rollback paths.

    The solver generates both:
    - forward_legs: achieve the desired outcome (e.g., bridge + swap)
    - rollback_legs: recover from failure (reverse bridge, refund tokens)

    The user signs both plans. Validators verify both before approving.
    Single-leg intents use forward_legs=[single_leg] with empty rollback.
    """
    forward_legs: list[LegPlan]
    rollback_legs: list[LegPlan] = field(default_factory=list)
    rollback_plan_hash: str = ""  # Hash of rollback plan, included in user signature

    def is_multi_leg(self) -> bool:
        return len(self.forward_legs) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "forward_legs": [leg.to_dict() for leg in self.forward_legs],
            "rollback_legs": [leg.to_dict() for leg in self.rollback_legs],
            "rollback_plan_hash": self.rollback_plan_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MultiLegPlan":
        return cls(
            forward_legs=[LegPlan.from_dict(l) for l in d.get("forward_legs", [])],
            rollback_legs=[LegPlan.from_dict(l) for l in d.get("rollback_legs", [])],
            rollback_plan_hash=d.get("rollback_plan_hash", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                    CROSS-CHAIN PLAN (Platform Primitive)
#
# Solvers declare cross-chain intent via CrossChainPlan. The platform's
# CrossChainCompiler converts this into an executable MultiLegPlan with
# bridge calldata, escrow, rollback, and simulation mocks injected.
# Solvers NEVER generate bridge calldata or escrow parameters directly.
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BridgeRequest:
    """Solver declares 'I need assets moved cross-chain'.

    The solver does NOT specify protocol, calldata, or escrow details.
    The platform selects the bridge adapter, builds calldata, and wraps
    with escrow deposit/release automatically.
    """
    token: str              # ERC-20 address on source chain (0x...)
    amount: int             # Amount in token's smallest unit (wei/decimals)
    src_chain_id: int       # Source chain
    dst_chain_id: int       # Destination chain
    recipient: str          # Address to receive on dest chain (usually user)
    min_output: int = 0     # Minimum acceptable output after bridge fees (0 = platform default 99%)
    purpose: str = ""       # Human-readable: "bridge USDC for dest swap"

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "amount": self.amount,
            "src_chain_id": self.src_chain_id,
            "dst_chain_id": self.dst_chain_id,
            "recipient": self.recipient,
            "min_output": self.min_output,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BridgeRequest":
        return cls(
            token=d["token"],
            amount=int(d["amount"]),
            src_chain_id=int(d["src_chain_id"]),
            dst_chain_id=int(d["dst_chain_id"]),
            recipient=d.get("recipient", ""),
            min_output=int(d.get("min_output", 0)),
            purpose=d.get("purpose", ""),
        )


@dataclass
class ChainLeg:
    """A set of interactions the solver wants executed on a specific chain.

    The solver provides ONLY the business-logic interactions (swap, stake,
    vote, mint, etc.). Bridge mechanics, escrow, and rollback are added
    by the platform's CrossChainCompiler.
    """
    chain_id: int
    interactions: list[Interaction]
    intent_selector: str = ""             # 4-byte hex for on-chain dispatch
    intent_params_hex: str = ""           # ABI-encoded intent params
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "interactions": [
                {"target": ix.target, "value": ix.value, "call_data": ix.call_data, "chain_id": ix.chain_id}
                for ix in self.interactions
            ],
            "intent_selector": self.intent_selector,
            "intent_params_hex": self.intent_params_hex,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainLeg":
        return cls(
            chain_id=int(d["chain_id"]),
            interactions=[
                Interaction(
                    target=ix["target"], value=ix["value"],
                    call_data=ix["call_data"], chain_id=ix.get("chain_id", 0),
                )
                for ix in d.get("interactions", [])
            ],
            intent_selector=d.get("intent_selector", ""),
            intent_params_hex=d.get("intent_params_hex", ""),
            metadata=d.get("metadata", {}),
        )


@dataclass
class CrossChainPlan:
    """Solver's cross-chain execution request — the platform primitive.

    The solver provides:
      - Ordered chain legs with business-logic interactions
      - Bridge requests declaring where assets need to cross chains

    Convention: bridge_requests[i] sits between legs[i] and legs[i+1].
    So len(bridge_requests) == len(legs) - 1.

    Chain continuity is enforced by the compiler:
      legs[i].chain_id == bridge_requests[i].src_chain_id
      legs[i+1].chain_id == bridge_requests[i].dst_chain_id

    The platform's CrossChainCompiler converts this into an executable
    MultiLegPlan with bridge calldata, escrow, rollback, and simulation
    mocks — none of which the solver controls.
    """
    legs: list[ChainLeg]
    bridge_requests: list[BridgeRequest]

    @property
    def is_cross_chain(self) -> bool:
        return len(self.bridge_requests) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "bridge_requests": [br.to_dict() for br in self.bridge_requests],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CrossChainPlan":
        return cls(
            legs=[ChainLeg.from_dict(l) for l in d.get("legs", [])],
            bridge_requests=[BridgeRequest.from_dict(br) for br in d.get("bridge_requests", [])],
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                    SIMULATION & EXECUTION RESULTS
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class TokenTransfer:
    """A token transfer observed during simulation."""
    token: str       # Token contract address
    from_addr: str   # Sender
    to_addr: str     # Receiver
    amount: str      # Amount as decimal string (wei)


@dataclass
class SimulationResult:
    """Result from Docker-based simulation of an execution plan."""
    success: bool
    gas_used: int = 0
    error: str | None = None
    token_transfers: list[TokenTransfer] = field(default_factory=list)
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    approval_changes: list[dict[str, Any]] = field(default_factory=list)
    on_chain_score: int | None = None    # BPS (0-10000) from contract scoreIntent()
    leg_results: dict[int, Any] | None = None       # leg_id -> per-leg sim result dict
    bridge_estimate: dict[str, Any] | None = None    # bridge quote data for cross-chain


# ── cross-chain plan helpers ─────────────────────────────────────────────────
#
# Cross-chain plans use the following metadata convention:
#   metadata["cross_chain"] = True
#   metadata["src_chain_id"] = 1          # Source chain
#   metadata["dst_chain_id"] = 964        # Destination chain
#   metadata["bridge_protocol"] = "mock"  # Bridge adapter protocol name
#   metadata["bridge_token"] = "0x..."    # Token bridged (for quoting)
#   metadata["bridge_amount"] = 1000      # Amount bridged in wei (for quoting)
#   metadata["legs"] = [
#       {"leg_id": 0, "chain_id": 1,   "type": "source",      "interaction_indices": [0, 1]},
#       {"leg_id": 1, "chain_id": 1,   "type": "bridge",      "bridge_protocol": "mock", ...},
#       {"leg_id": 2, "chain_id": 964, "type": "destination",  "interaction_indices": [3]},
#   ]
# Single-chain plans omit the "cross_chain" key entirely.


def partition_plan_by_leg(plan: ExecutionPlan) -> dict[int, list[Interaction]]:
    """Group plan interactions by leg_id from ``metadata["legs"]``.

    Returns a dict mapping leg_id to the list of Interactions for that leg.
    If the plan has no legs metadata, returns ``{0: plan.interactions}``.
    """
    legs = plan.metadata.get("legs")
    if not legs:
        return {0: list(plan.interactions)}

    result: dict[int, list[Interaction]] = {}
    for leg in legs:
        leg_id = leg["leg_id"]
        indices = leg.get("interaction_indices", [])
        result[leg_id] = [plan.interactions[i] for i in indices if i < len(plan.interactions)]
    return result


def extract_leg_plan(plan: ExecutionPlan, leg_id: int) -> ExecutionPlan:
    """Extract a sub-plan containing only the interactions for a single leg.

    The returned plan copies the original's metadata with the specific
    leg's chain_id set in ``metadata["chain_id"]``.
    """
    legs = plan.metadata.get("legs", [])
    leg_meta = next((l for l in legs if l["leg_id"] == leg_id), None)

    if leg_meta is None:
        return plan  # no legs metadata — return original

    indices = leg_meta.get("interaction_indices", [])
    interactions = [plan.interactions[i] for i in indices if i < len(plan.interactions)]

    meta = dict(plan.metadata)
    meta["chain_id"] = leg_meta.get("chain_id", plan.metadata.get("chain_id"))

    return ExecutionPlan(
        intent_id=plan.intent_id,
        interactions=interactions,
        deadline=plan.deadline,
        nonce=plan.nonce,
        metadata=meta,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                           SCORING TYPES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class QuoteResult:
    """Result of a solver quote computation (no simulation needed)."""
    estimated_output: str                    # Best output amount as decimal string
    computed_params: dict[str, str] = field(default_factory=dict)  # Matches manifest source:"quote" params
    route_summary: str = ""                  # Human-readable route description
    gas_estimate: int = 0                    # Estimated gas units
    metadata: dict[str, Any] = field(default_factory=dict)  # Extra info (pool used, price impact, etc.)
    platform_fee_wei: str = "0"              # Platform fee in wrapped native token (WETH/WTAO) wei
    platform_fee_token: str = ""             # Address of the wrapped native token
    platform_fee_symbol: str = ""            # "ETH" or "TAO"


@dataclass
class ScoreResult:
    """Result of scoring an execution plan via JS engine."""
    score: float                             # 0.0 - 1.0
    valid: bool = True                       # Whether the plan is structurally valid
    reason: str = ""                         # Human-readable explanation
    breakdown: dict[str, float] = field(default_factory=dict)  # Score components
    metadata: dict[str, Any] = field(default_factory=dict)     # Extra data for logging


@dataclass
class ValidationResult:
    """Complete validation result for an intent execution."""
    intent_id: str
    plan_hash: str
    simulation: SimulationResult
    js_score: ScoreResult
    on_chain_score: int | None = None        # BPS (0-10000) from Solidity, if checked
    approved: bool = False                   # Final decision
    validator_id: str = ""
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#                         INTENT STATE TYPES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class IntentState:
    """Current on-chain state of an App Intent contract."""
    contract_address: str
    chain_id: int
    nonce: int
    owner: str
    raw_params: dict[str, Any] = field(default_factory=dict)
    control: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # Legacy compatibility payload
    context_version: str = "v2"
    policy_tier: PolicyTier = PolicyTier.HYBRID
    typed_context: Any | None = None

    def __post_init__(self) -> None:
        self.raw_params = dict(self.raw_params or {})
        self.control = dict(self.control or {})
        legacy_extra = dict(self.extra or {})

        if legacy_extra:
            extra_raw, extra_control = self._split_extra(legacy_extra)
            if not self.raw_params:
                self.raw_params = extra_raw
            else:
                self.raw_params = {**extra_raw, **self.raw_params}
            if not self.control:
                self.control = extra_control
            else:
                self.control = {**extra_control, **self.control}

        self.sync_extra()

    @staticmethod
    def _split_extra(extra: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = {
            key: value
            for key, value in extra.items()
            if not str(key).startswith("_")
        }
        control = {
            key: value
            for key, value in extra.items()
            if str(key).startswith("_")
        }
        return raw, control

    def raw_params_view(self) -> dict[str, Any]:
        """Return the current app/runtime params view."""
        return dict(self.raw_params)

    def control_view(self) -> dict[str, Any]:
        """Return the current system/control metadata view."""
        return dict(self.control)

    def sync_extra(self) -> None:
        """Refresh the legacy compatibility payload from structured state fields."""
        self.extra = {**self.raw_params, **self.control}


# ═══════════════════════════════════════════════════════════════════════════════
#                       APP INTENT DEFINITION
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class AppIntentConfig:
    """Configuration for an App Intent."""
    supported_chains: list[int] = field(default_factory=list)
    score_threshold: float = 0.5             # JS score minimum for execution
    on_chain_threshold: int = 5000           # BPS minimum (Solidity safety backstop)
    trigger_type: TriggerType = TriggerType.USER_TRIGGERED
    max_gas: int = 500_000                   # Gas limit for execution
    policy_tier: PolicyTier = PolicyTier.HYBRID
    supported_policy_tiers: list[PolicyTier] = field(
        default_factory=lambda: [PolicyTier.STRICT, PolicyTier.HYBRID, PolicyTier.EXPERT]
    )
    manifest_version: str = "v1"
    # Per-App on-chain fee mode baked into the contract at deploy (#239):
    # "USER" = users pay the fee, "APP" = the App's paymaster pays. Empty string
    # means "fall back to the operator's FEE_MODE_DEFAULT" at deploy time.
    fee_mode: str = ""


@dataclass
class AppIntentDefinition:
    """Complete definition of an App Intent - what gets deployed."""
    app_id: str                              # Unique identifier
    name: str                                # Human-readable name
    version: str                             # Semantic version
    intent_type: str                         # "swap", "limit_order", "rebalance", etc.
    js_code: str                             # JS scoring function source
    solidity_code: str | None = None         # On-chain contract source (optional for MVP)
    config: AppIntentConfig = field(default_factory=AppIntentConfig)
    deployer: str = ""                       # Address of deployer
    description: str = ""                    # What this app does
    manifest: dict[str, Any] | None = None   # JS manifest (intent_functions, param schemas) (SE-11)
    constructor_args: list[tuple[str, str]] | None = None  # Extra ctor args: [(abi_type, value), ...]
    schema_id: str = ""
    policy_metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
#                         WALLET & DEPLOYMENT
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class WalletInfo:
    """Information about a managed wallet."""
    address: str                             # Wallet address
    chain_ids: list[int] = field(default_factory=list)
    wallet_type: str = "lit_mpc"             # "lit_mpc", "local" (dev only)
    created_at: float = 0.0
    policy_tier: PolicyTier = PolicyTier.HYBRID
    policy_id: str = ""
    policy_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class NativeBittensorPermission:
    """Policy-bounded delegated authority for native Bittensor staking flows."""
    permission_id: str
    owner_ss58: str
    delegate_ss58: str
    proxy_type: str = "Staking"
    proxy_delay_blocks: int = 0
    status: NativeBittensorPermissionStatus = NativeBittensorPermissionStatus.PENDING
    enabled_actions: list[NativeBittensorAction] = field(
        default_factory=lambda: [
            NativeBittensorAction.ADD_STAKE,
            NativeBittensorAction.MOVE_STAKE,
        ]
    )
    allowed_netuids: list[int] = field(default_factory=list)
    allowed_hotkeys: list[str] = field(default_factory=list)
    max_rao_per_action: int | None = None
    max_rao_per_day: int | None = None
    max_slippage_bps: int | None = None
    cooldown_seconds: int | None = None
    expires_at: float | None = None
    policy_tier: PolicyTier = PolicyTier.STRICT
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self, now: float | None = None) -> bool:
        """Return whether the permission is currently usable."""
        if self.status is not NativeBittensorPermissionStatus.ACTIVE:
            return False
        if self.expires_at is None:
            return True
        current = now if now is not None else time.time()
        return current <= self.expires_at

    def allows_action(self, action: NativeBittensorAction) -> bool:
        """Return whether the permission explicitly allows an action."""
        return action in self.enabled_actions


@dataclass
class NativeBittensorActionRequest:
    """Normalized request for a delegated native Bittensor action."""
    permission_id: str
    action: NativeBittensorAction
    owner_ss58: str
    delegate_ss58: str
    amount_rao: int
    netuid: int | None = None
    hotkey_ss58: str = ""
    origin_netuid: int | None = None
    origin_hotkey_ss58: str = ""
    destination_netuid: int | None = None
    destination_hotkey_ss58: str = ""
    limit_price: int | None = None
    allow_partial: bool = False
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def related_netuids(self) -> list[int]:
        """Return the netuids touched by this request."""
        values = [
            self.netuid,
            self.origin_netuid,
            self.destination_netuid,
        ]
        return [value for value in values if value is not None]

    def related_hotkeys(self) -> list[str]:
        """Return the validator hotkeys touched by this request."""
        values = [
            self.hotkey_ss58,
            self.origin_hotkey_ss58,
            self.destination_hotkey_ss58,
        ]
        return [value for value in values if value]


@dataclass
class NativeBittensorExecutionRecord:
    """Audit record for a delegated native Bittensor action."""
    execution_id: str
    permission_id: str
    action: NativeBittensorAction
    owner_ss58: str
    delegate_ss58: str
    amount_rao: int
    status: NativeBittensorExecutionStatus = NativeBittensorExecutionStatus.PENDING
    netuid: int | None = None
    hotkey_ss58: str = ""
    origin_netuid: int | None = None
    origin_hotkey_ss58: str = ""
    destination_netuid: int | None = None
    destination_hotkey_ss58: str = ""
    call_hash: str = ""
    extrinsic_hash: str = ""
    error: str = ""
    reason: str = ""
    submitted_at: float = 0.0
    finalized_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubstrateAction:
    """A Bittensor substrate extrinsic in a cross-chain execution plan.

    Used alongside EVM ``Interaction`` objects in multi-runtime plans.
    The blockloop routes these to the SubstrateRelayer instead of the
    EvmRelayer.

    Leg metadata convention::

        legs[i]["runtime"] = "substrate"
        legs[i]["substrate_actions"] = [SubstrateAction(...).to_dict(), ...]
    """
    action: str           # "remove_stake" | "bridge_deposit"
    owner_ss58: str       # User's Bittensor SS58 address
    amount_rao: int       # Amount in RAO (1 TAO = 1e9 RAO)
    netuid: int = 0       # For unstake: subnet netuid
    hotkey_ss58: str = "" # For unstake: validator hotkey
    dest_address: str = "" # For bridge deposit: lock address (SS58)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubstrateAction":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CompilationResult:
    """Result of compiling a Solidity contract via Forge."""
    contract_name: str
    bytecode: str       # "0x..." creation bytecode
    abi: list
    error: str | None = None


@dataclass
class CodeValidationResult:
    """Pre-flight validation result for App Intent JS and/or Solidity code."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    js_config: dict[str, Any] | None = None
    js_manifest: dict[str, Any] | None = None
    js_exports: list[str] = field(default_factory=list)
    solidity_abi: list | None = None
    solidity_contract_name: str = ""


@dataclass
class DeploymentResult:
    """Result of deploying an App Intent."""
    app_id: str
    status: AppStatus
    contract_address: str | None = None      # On-chain contract (if deployed)
    js_code_hash: str = ""                   # Hash of deployed JS
    chain_id: int = 0                        # Must be set explicitly by caller
    error: str | None = None
    tx_hash: str | None = None
    abi: list | None = None


# ═══════════════════════════════════════════════════════════════════════════════
#                        INTENT INSTANCES (USER REQUESTS)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class IntentInstance:
    """A single user request submitted to a deployed App Intent."""
    instance_id: str                         # Unique ID for this request
    app_id: str                              # Which App this belongs to
    params: dict[str, Any] = field(default_factory=dict)  # User-provided parameters
    status: IntentInstanceStatus = IntentInstanceStatus.PENDING
    plan: ExecutionPlan | None = None        # Generated execution plan
    score: ScoreResult | None = None         # Score from JS engine
    result: dict[str, Any] = field(default_factory=dict)  # Execution result
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    submitted_by: str = ""                   # User wallet address


# ═══════════════════════════════════════════════════════════════════════════════
#                       ORDERBOOK & RELAYER TYPES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SubmitResult:
    """Result of submitting an approved plan to the target chain."""
    success: bool
    tx_hash: str | None = None
    error: str | None = None
    chain_id: int = 0
    block_number: int | None = None
    gas_used: int = 0


@dataclass
class TickResult:
    """Summary of a single block loop tick."""
    tick_number: int
    timestamp: float
    orders_processed: int = 0
    orders_approved: int = 0
    orders_rejected: int = 0
    orders_expired: int = 0
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
#                       CONSENSUS TYPES (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SignedApproval:
    """A single validator's signed approval for an execution plan."""
    validator_id: str
    order_id: str
    plan_hash: str
    score: float
    signature: str
    timestamp: float = 0.0


@dataclass
class ConsensusResult:
    """Result of consensus round for an order's execution plan."""
    reached: bool
    approvals: list[SignedApproval] = field(default_factory=list)
    quorum: int = 1
    collected: int = 0
    combined_score: float = 0.0
