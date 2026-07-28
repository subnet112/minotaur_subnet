"""Snapshot builder and serializer for deterministic benchmarking.

Builds MarketSnapshot objects at a specific block number, capturing all
market data needed for solver plan generation. Snapshots are serialized
to JSON files and mounted into solver containers as read-only volumes.

Two modes:
1. Live mode: Fetches data from RPC endpoints (production)
2. Static mode: Loads from pre-built JSON files (testing, replay)

Snapshot directory layout (mounted at /data/snapshot/ in containers):
    /data/snapshot/
    ├── meta.json           # {epoch, block_number, timestamp, chains}
    ├── chain_1.json        # Ethereum snapshot (MarketSnapshot fields)
    ├── chain_8453.json     # Base snapshot
    └── intents.json        # Active intents + states

Usage (live):
    builder = SnapshotBuilder()
    snapshot = await builder.build_chain_snapshot(chain_id=1, block_number=18500000)

Usage (static / testing):
    # Save
    save_snapshot("/tmp/snapshot", meta, chain_snapshots, intents)

    # Load
    meta, chain_snapshots, intents = load_snapshot("/tmp/snapshot")
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    IntentState,
    TriggerType,
)
from minotaur_subnet.sdk.intent_solver import MarketSnapshot

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#                          SNAPSHOT METADATA
# ═══════════════════════════════════════════════════════════════════════════════

# Blocks after epoch start to snapshot (allows finalization)
SNAPSHOT_OFFSET = 100


@dataclass
class SnapshotMeta:
    """Metadata for a benchmark snapshot."""
    epoch: int
    timestamp: int
    chains: list[int] = field(default_factory=lambda: [1])
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


# ═══════════════════════════════════════════════════════════════════════════════
#                          WELL-KNOWN ADDRESSES
# ═══════════════════════════════════════════════════════════════════════════════

# Common tokens per chain
MONITORED_TOKENS: dict[int, dict[str, str]] = {
    1: {
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    },
    8453: {
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDbC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
    },
}

# ERC-20 balanceOf ABI
ERC20_BALANCE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
#                          SNAPSHOT BUILDER (LIVE)
# ═══════════════════════════════════════════════════════════════════════════════


class SnapshotBuilder:
    """Builds MarketSnapshots from live RPC data.

    Captures block context and token balances at a specific block for
    deterministic benchmarking. App-agnostic: it does not model any
    protocol's state (this used to query Uniswap V3 pools — see
    MarketSnapshot for why that left).
    """

    async def build_chain_snapshot(
        self,
        chain_id: int,
        block_number: int,
        contract_addresses: list[str] | None = None,
    ) -> MarketSnapshot:
        """Build a snapshot for a single chain at a specific block.

        Args:
            chain_id: Target chain ID.
            block_number: Block number to snapshot at.
            contract_addresses: Intent contract addresses to query
                balances for. If None, no balances are captured.

        Returns:
            MarketSnapshot with block context and token balances.

        Raises:
            ImportError: If web3 is not available.
            ConnectionError: If RPC is unreachable.
        """
        from minotaur_subnet.blockchain.chains import get_web3

        w3 = get_web3(chain_id)
        block = w3.eth.get_block(block_number)
        timestamp = block["timestamp"]

        # Fetch token balances for contract addresses
        balances: dict[str, str] = {}
        if contract_addresses:
            tokens = MONITORED_TOKENS.get(chain_id, {})
            for contract_addr in contract_addresses:
                for token_name, token_addr in tokens.items():
                    try:
                        bal = await self._query_balance(
                            w3, token_addr, contract_addr, block_number,
                        )
                        balances[token_addr] = str(bal)
                    except Exception as exc:
                        logger.warning(
                            "Failed to query balance of %s for %s: %s",
                            token_name, contract_addr, exc,
                        )

        return MarketSnapshot(
            chain_id=chain_id,
            block_number=block_number,
            timestamp=timestamp,
            balances=balances,
        )

    async def _query_balance(
        self, w3: Any, token_address: str, account: str, block_number: int,
    ) -> int:
        """Query an ERC-20 token balance at a specific block."""
        token = w3.eth.contract(
            address=w3.to_checksum_address(token_address),
            abi=ERC20_BALANCE_ABI,
        )
        return token.functions.balanceOf(
            w3.to_checksum_address(account),
        ).call(block_identifier=block_number)

# ═══════════════════════════════════════════════════════════════════════════════
#                          SERIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════


def save_snapshot(
    output_dir: str,
    meta: SnapshotMeta,
    chain_snapshots: dict[int, MarketSnapshot],
    intents: list[tuple[AppIntentDefinition, IntentState]] | None = None,
) -> None:
    """Serialize a complete benchmark snapshot to a directory.

    Creates the directory structure expected by solver containers:
        output_dir/
        ├── meta.json
        ├── chain_1.json
        ├── chain_8453.json
        └── intents.json

    Args:
        output_dir: Directory to write snapshot files to.
        meta: Snapshot metadata (epoch, timestamp, chains).
        chain_snapshots: MarketSnapshot per chain ID.
        intents: Optional list of (intent, state) tuples.
    """
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    # meta.json
    _write_json(path / "meta.json", {
        "epoch": meta.epoch,
        "timestamp": meta.timestamp,
        "chains": meta.chains,
        "created_at": meta.created_at,
    })

    # Per-chain snapshots
    for chain_id, snapshot in chain_snapshots.items():
        _write_json(path / f"chain_{chain_id}.json", {
            "chain_id": snapshot.chain_id,
            "block_number": snapshot.block_number,
            "timestamp": snapshot.timestamp,

            "balances": snapshot.balances,
            "app_data": snapshot.app_data,
            "raw_state": snapshot.raw_state,
        })

    # Intents
    intent_list: list[dict[str, Any]] = []
    if intents:
        for intent_def, intent_state in intents:
            intent_list.append({
                "intent": _intent_to_dict(intent_def),
                "state": _state_to_dict(intent_state),
            })
    _write_json(path / "intents.json", intent_list)

    logger.info(
        "Snapshot saved to %s (%d chains, %d intents)",
        output_dir, len(chain_snapshots), len(intent_list),
    )


def load_snapshot(
    snapshot_dir: str,
) -> tuple[
    SnapshotMeta,
    dict[int, MarketSnapshot],
    list[tuple[AppIntentDefinition, IntentState]],
]:
    """Load a snapshot from a directory.

    Args:
        snapshot_dir: Directory containing snapshot JSON files.

    Returns:
        Tuple of (meta, chain_snapshots, intents).

    Raises:
        FileNotFoundError: If snapshot_dir or required files don't exist.
    """
    path = Path(snapshot_dir)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot directory not found: {snapshot_dir}")

    # meta.json
    meta_data = _read_json(path / "meta.json")
    meta = SnapshotMeta(
        epoch=meta_data["epoch"],
        timestamp=meta_data["timestamp"],
        chains=meta_data.get("chains", [1]),
        created_at=meta_data.get("created_at", 0.0),
    )

    # Per-chain snapshots
    chain_snapshots: dict[int, MarketSnapshot] = {}
    for chain_id in meta.chains:
        chain_file = path / f"chain_{chain_id}.json"
        if chain_file.exists():
            data = _read_json(chain_file)
            chain_snapshots[chain_id] = MarketSnapshot(
                chain_id=data["chain_id"],
                block_number=data["block_number"],
                timestamp=data["timestamp"],

                balances=data.get("balances", {}),
                app_data=data.get("app_data", {}),
                raw_state=data.get("raw_state", {}),
            )

    # Intents
    intents: list[tuple[AppIntentDefinition, IntentState]] = []
    intents_file = path / "intents.json"
    if intents_file.exists():
        intent_list = _read_json(intents_file)
        for entry in intent_list:
            intent_def = _dict_to_intent(entry["intent"])
            intent_state = _dict_to_state(entry["state"])
            intents.append((intent_def, intent_state))

    logger.info(
        "Snapshot loaded from %s (%d chains, %d intents)",
        snapshot_dir, len(chain_snapshots), len(intents),
    )

    return meta, chain_snapshots, intents


def load_chain_snapshot(snapshot_dir: str, chain_id: int) -> MarketSnapshot:
    """Load a single chain snapshot from a directory.

    Convenience method for the harness runner — loads just the chain
    file it needs without parsing everything.

    Args:
        snapshot_dir: Snapshot directory.
        chain_id: Chain ID to load.

    Returns:
        MarketSnapshot for the requested chain.
    """
    path = Path(snapshot_dir) / f"chain_{chain_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Chain snapshot not found: {path}")

    data = _read_json(path)
    return MarketSnapshot(
        chain_id=data["chain_id"],
        block_number=data["block_number"],
        timestamp=data["timestamp"],

        balances=data.get("balances", {}),
        app_data=data.get("app_data", {}),
        raw_state=data.get("raw_state", {}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          SYNTHETIC (BENCHMARK/TEST)
# ═══════════════════════════════════════════════════════════════════════════════


def build_synthetic_snapshot(chain_id: int = 1) -> MarketSnapshot:
    """Build a synthetic snapshot for benchmark/screening runs.

    **BENCHMARK/TEST ONLY** — production solvers query live state via RPC.

    Carries block context only. This used to synthesise a full set of
    Uniswap V3 pool states (sqrtPriceX96 derived from a hardcoded USD price
    table, plus fee tiers and multi-hop intermediaries) and a ``dex_config``
    of router addresses — hundreds of lines modelling one protocol, for
    fields no part of the platform and no reference solver ever read. An app
    that needs app-specific state populates ``MarketSnapshot.app_data``.
    """
    import time as _time

    return MarketSnapshot(
        chain_id=chain_id,
        block_number=18500000,
        timestamp=int(_time.time()),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _write_json(path: Path, data: Any) -> None:
    """Write data as formatted JSON."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file."""
    with open(path) as f:
        return json.load(f)


def _intent_to_dict(intent: AppIntentDefinition) -> dict[str, Any]:
    """Convert AppIntentDefinition to a JSON-safe dict."""
    return {
        "app_id": intent.app_id,
        "name": intent.name,
        "version": intent.version,
        "intent_type": intent.intent_type,
        "js_code": intent.js_code,
        "solidity_code": intent.solidity_code,
        "config": {
            "supported_chains": intent.config.supported_chains,
            "score_threshold": intent.config.score_threshold,
            "on_chain_threshold": intent.config.on_chain_threshold,
            "trigger_type": intent.config.trigger_type.value,
            "max_gas": intent.config.max_gas,
        },
        "deployer": intent.deployer,
        "description": intent.description,
    }


def _state_to_dict(state: IntentState) -> dict[str, Any]:
    """Convert IntentState to a JSON-safe dict."""
    from dataclasses import asdict, is_dataclass

    result = {
        "contract_address": state.contract_address,
        "chain_id": state.chain_id,
        "nonce": state.nonce,
        "owner": state.owner,
        "raw_params": state.raw_params_view(),
        "control": state.control_view(),
        "context_version": state.context_version,
        "policy_tier": state.policy_tier.value,
    }
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        if is_dataclass(typed):
            result["typed_context"] = asdict(typed)
        elif hasattr(typed, "__dict__"):
            result["typed_context"] = dict(typed.__dict__)
    return result


def _dict_to_intent(d: dict[str, Any]) -> AppIntentDefinition:
    """Reconstruct AppIntentDefinition from dict."""
    config_d = d.get("config", {})
    trigger_raw = config_d.get("trigger_type", "user_triggered")
    trigger_type = TriggerType(trigger_raw) if isinstance(trigger_raw, str) else TriggerType.USER_TRIGGERED

    return AppIntentDefinition(
        app_id=d["app_id"],
        name=d.get("name", ""),
        version=d.get("version", ""),
        intent_type=d.get("intent_type", ""),
        js_code=d.get("js_code", ""),
        solidity_code=d.get("solidity_code"),
        config=AppIntentConfig(
            supported_chains=config_d.get("supported_chains", [1]),
            score_threshold=config_d.get("score_threshold", 0.5),
            on_chain_threshold=config_d.get("on_chain_threshold", 5000),
            trigger_type=trigger_type,
            max_gas=config_d.get("max_gas", 500_000),
        ),
        deployer=d.get("deployer", ""),
        description=d.get("description", ""),
    )


def _dict_to_state(d: dict[str, Any]) -> IntentState:
    """Reconstruct IntentState from dict."""
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
