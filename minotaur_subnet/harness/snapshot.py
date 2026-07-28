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
    ├── intents.json        # Active intents + states
    └── prices.json         # Cross-chain price feeds

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

# Uniswap V3 factory and router per chain
UNISWAP_V3_CONFIG: dict[int, dict[str, str]] = {
    1: {
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "quoter": "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6",
    },
    8453: {
        "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    },
}

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

# Well-known Uniswap V3 pools to monitor (address -> description)
MONITORED_POOLS: dict[int, dict[str, str]] = {
    1: {
        "0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8": "USDC/WETH 0.3%",
        "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640": "USDC/WETH 0.05%",
        "0x4e68Ccd3E89f51C3074ca5072bbAC773960dFa36": "WETH/USDT 0.3%",
        "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD": "WBTC/WETH 0.3%",
    },
    8453: {
        "0xd0b53D9277642d899DF5C87A3966A349A798F224": "WETH/USDC 0.05%",
    },
}

# Minimal Uniswap V3 pool ABI for slot0 + liquidity queries
POOL_ABI_SLOT0 = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"internalType": "uint24", "name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

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

    Queries Uniswap V3 pool states, token balances, and block data
    at a specific block number for deterministic benchmarking.
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
                balances for. If None, only captures pool states.

        Returns:
            MarketSnapshot with pool states, balances, and prices.

        Raises:
            ImportError: If web3 is not available.
            ConnectionError: If RPC is unreachable.
        """
        from minotaur_subnet.blockchain.chains import get_web3

        w3 = get_web3(chain_id)
        block = w3.eth.get_block(block_number)
        timestamp = block["timestamp"]

        # Fetch pool states
        pools = MONITORED_POOLS.get(chain_id, {})
        pool_states = {}
        for pool_addr, description in pools.items():
            try:
                state = await self._query_pool_state(w3, pool_addr, block_number)
                state["description"] = description
                pool_states[pool_addr] = state
            except Exception as exc:
                logger.warning(
                    "Failed to query pool %s (%s): %s",
                    pool_addr, description, exc,
                )

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

        # Build prices from pool states
        prices = self._derive_prices(pool_states, chain_id)

        # DEX config
        dex_config = UNISWAP_V3_CONFIG.get(chain_id, {})

        return MarketSnapshot(
            chain_id=chain_id,
            block_number=block_number,
            timestamp=timestamp,
            prices=prices,
            pool_states=pool_states,
            balances=balances,
            dex_config=dex_config,
        )

    async def _query_pool_state(
        self, w3: Any, pool_address: str, block_number: int,
    ) -> dict[str, Any]:
        """Query a Uniswap V3 pool's state at a specific block."""
        pool = w3.eth.contract(
            address=w3.to_checksum_address(pool_address),
            abi=POOL_ABI_SLOT0,
        )

        slot0 = pool.functions.slot0().call(block_identifier=block_number)
        liquidity = pool.functions.liquidity().call(block_identifier=block_number)
        fee = pool.functions.fee().call(block_identifier=block_number)
        token0 = pool.functions.token0().call(block_identifier=block_number)
        token1 = pool.functions.token1().call(block_identifier=block_number)

        return {
            "token0": token0,
            "token1": token1,
            "fee": fee,
            "sqrtPriceX96": str(slot0[0]),
            "tick": slot0[1],
            "liquidity": str(liquidity),
            "observationIndex": slot0[2],
        }

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

    def _derive_prices(
        self, pool_states: dict[str, dict[str, Any]], chain_id: int,
    ) -> dict[str, float]:
        """Derive token prices from pool sqrtPriceX96 values.

        Uses USDC-paired pools to get USD prices. This is a simplified
        price derivation — production would use multiple sources.
        """
        prices: dict[str, float] = {"USDC/USD": 1.0}
        tokens = MONITORED_TOKENS.get(chain_id, {})
        usdc_addr = tokens.get("USDC", "").lower()

        if not usdc_addr:
            return prices

        for pool_addr, state in pool_states.items():
            token0 = state.get("token0", "").lower()
            token1 = state.get("token1", "").lower()
            sqrt_price_raw = state.get("sqrtPriceX96")

            if not sqrt_price_raw:
                continue

            sqrt_price = int(sqrt_price_raw)
            if sqrt_price == 0:
                continue

            # price = (sqrtPriceX96 / 2^96)^2 = sqrtPriceX96^2 / 2^192
            # This gives price of token0 in terms of token1
            price_ratio = (sqrt_price ** 2) / (2 ** 192)

            # Determine which token is USDC to derive USD price
            if token0 == usdc_addr:
                # price_ratio = token0(USDC) per token1
                # So token1 price in USDC = 1/price_ratio
                other_token = token1
                usd_price = 1.0 / price_ratio if price_ratio > 0 else 0
            elif token1 == usdc_addr:
                # price_ratio = token0 per token1(USDC)
                # So token0 price in USDC = price_ratio
                other_token = token0
                usd_price = price_ratio
            else:
                continue

            # Find the token name
            for name, addr in tokens.items():
                if addr.lower() == other_token:
                    prices[f"{name}/USD"] = usd_price
                    break

        return prices


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
        ├── intents.json
        └── prices.json

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
            "prices": snapshot.prices,
            "pool_states": snapshot.pool_states,
            "balances": snapshot.balances,
            "dex_config": snapshot.dex_config,
            "raw_state": snapshot.raw_state,
        })

    # Aggregated prices from all chains
    all_prices: dict[str, float] = {}
    for snapshot in chain_snapshots.values():
        all_prices.update(snapshot.prices)
    _write_json(path / "prices.json", all_prices)

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
                prices=data.get("prices", {}),
                pool_states=data.get("pool_states", {}),
                balances=data.get("balances", {}),
                dex_config=data.get("dex_config", {}),
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
        prices=data.get("prices", {}),
        pool_states=data.get("pool_states", {}),
        balances=data.get("balances", {}),
        dex_config=data.get("dex_config", {}),
        raw_state=data.get("raw_state", {}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          SYNTHETIC SNAPSHOTS (TESTING)
# ═══════════════════════════════════════════════════════════════════════════════

# Token decimals for synthetic pool generation
TOKEN_DECIMALS: dict[str, int] = {
    "WETH": 18, "USDC": 6, "USDT": 6, "WBTC": 8, "DAI": 18, "USDbC": 6,
}

# Native → wrapped token mapping per chain. Smart contracts use the wrapped
# ERC-20 version; users and price feeds often use the native symbol.
NATIVE_TO_WRAPPED: dict[int, dict[str, str]] = {
    1: {"ETH": "WETH"},
    8453: {"ETH": "WETH"},
    42161: {"ETH": "WETH"},
    10: {"ETH": "WETH"},
    137: {"MATIC": "WMATIC"},
    56: {"BNB": "WBNB"},
    43114: {"AVAX": "WAVAX"},
}

# Synthetic USD prices used to compute relative token prices for pools
SYNTHETIC_PRICES: dict[str, float] = {
    "WETH": 1850.0,
    "USDC": 1.0,
    "USDT": 1.0,
    "WBTC": 43000.0,
    "DAI": 1.0,
    "USDbC": 1.0,
}

# Default synthetic liquidity and fee tier for auto-generated pools
_DEFAULT_LIQUIDITY = "20000000000000000000"  # 2e19
_DEFAULT_FEE = 3000  # 0.3%


def _generate_synthetic_pools(chain_id: int) -> dict[str, dict[str, Any]]:
    """Generate synthetic pool states for all token pairs on a chain.

    **BENCHMARK/TEST ONLY** — Production code should NOT use this.
    Solvers query real pool states via RPC instead.

    For each pair of tokens in MONITORED_TOKENS with known USD prices,
    creates a synthetic Uniswap V3 pool with correct sqrtPriceX96 derived
    from relative prices. Uses real pool addresses from MONITORED_POOLS
    where available, otherwise generates deterministic addresses.
    """
    import hashlib

    _Q96 = 1 << 96

    def price_to_sqrt_price_x96(
        token0_per_token1: float,
        token0_decimals: int,
        token1_decimals: int,
    ) -> int:
        price_raw = 10**token1_decimals / (token0_per_token1 * 10**token0_decimals)
        return int(math.sqrt(price_raw) * _Q96)

    def price_to_tick(
        token0_per_token1: float,
        token0_decimals: int,
        token1_decimals: int,
    ) -> int:
        price_raw = 10**token1_decimals / (token0_per_token1 * 10**token0_decimals)
        if price_raw <= 0:
            return 0
        return int(math.log(price_raw) / math.log(1.0001))

    tokens = MONITORED_TOKENS.get(chain_id, MONITORED_TOKENS.get(1, {}))
    known_pools = MONITORED_POOLS.get(chain_id, {})

    # Collect all token symbols with known prices and addresses
    available = [
        (sym, tokens[sym])
        for sym in tokens
        if sym in SYNTHETIC_PRICES and sym in TOKEN_DECIMALS
    ]

    pool_states: dict[str, dict[str, Any]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for i, (sym_a, addr_a) in enumerate(available):
        for sym_b, addr_b in available[i + 1:]:
            # Uniswap V3: token0 < token1 by address
            if addr_a.lower() < addr_b.lower():
                t0_sym, t1_sym = sym_a, sym_b
                t0_addr, t1_addr = addr_a, addr_b
            else:
                t0_sym, t1_sym = sym_b, sym_a
                t0_addr, t1_addr = addr_b, addr_a

            pair_key = (t0_addr.lower(), t1_addr.lower())
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            t0_dec = TOKEN_DECIMALS[t0_sym]
            t1_dec = TOKEN_DECIMALS[t1_sym]
            t0_usd = SYNTHETIC_PRICES[t0_sym]
            t1_usd = SYNTHETIC_PRICES[t1_sym]

            # token0_per_token1: how many token0 per one token1
            # e.g. USDC/WETH: 1 WETH ($1850) = 1850 USDC ($1)
            token0_per_token1 = t1_usd / t0_usd

            sqrt_price = price_to_sqrt_price_x96(token0_per_token1, t0_dec, t1_dec)
            tick = price_to_tick(token0_per_token1, t0_dec, t1_dec)

            # Look for a real pool address, otherwise generate one
            pool_addr = None
            for known_addr, desc in known_pools.items():
                # Match by checking the description contains both token symbols
                if t0_sym in desc and t1_sym in desc:
                    if known_addr not in pool_states:
                        pool_addr = known_addr
                        break

            if pool_addr is None:
                # Deterministic synthetic address from token pair
                h = hashlib.sha256(f"{t0_addr}:{t1_addr}:{_DEFAULT_FEE}".encode())
                pool_addr = "0x" + h.hexdigest()[:40]

            pool_states[pool_addr] = {
                "token0": t0_addr,
                "token1": t1_addr,
                "fee": _DEFAULT_FEE,
                "sqrtPriceX96": str(sqrt_price),
                "tick": tick,
                "liquidity": _DEFAULT_LIQUIDITY,
                "description": f"{t0_sym}/{t1_sym} {_DEFAULT_FEE / 1_000_000:.2%} (synthetic)",
            }

    # Add extra fee tiers for high-volume pairs from MONITORED_POOLS
    # that weren't covered by the default 0.3% generation above
    for known_addr, desc in known_pools.items():
        if known_addr in pool_states:
            continue
        # Parse description to find tokens and fee
        # Descriptions look like "USDC/WETH 0.05%" or "WBTC/WETH 0.3%"
        parts = desc.split()
        if len(parts) < 2 or "/" not in parts[0]:
            continue
        syms = parts[0].split("/")
        if len(syms) != 2:
            continue
        sym_a, sym_b = syms
        if sym_a not in tokens or sym_b not in tokens:
            continue
        if sym_a not in SYNTHETIC_PRICES or sym_b not in SYNTHETIC_PRICES:
            continue

        addr_a, addr_b = tokens[sym_a], tokens[sym_b]
        if addr_a.lower() < addr_b.lower():
            t0_sym, t1_sym = sym_a, sym_b
            t0_addr, t1_addr = addr_a, addr_b
        else:
            t0_sym, t1_sym = sym_b, sym_a
            t0_addr, t1_addr = addr_b, addr_a

        # Parse fee from description (e.g. "0.05%" → 500, "0.3%" → 3000)
        try:
            pct_str = parts[1].rstrip("%")
            fee = int(float(pct_str) * 10_000)
        except (ValueError, IndexError):
            fee = _DEFAULT_FEE

        t0_dec = TOKEN_DECIMALS[t0_sym]
        t1_dec = TOKEN_DECIMALS[t1_sym]
        token0_per_token1 = SYNTHETIC_PRICES[t1_sym] / SYNTHETIC_PRICES[t0_sym]
        sqrt_price = price_to_sqrt_price_x96(token0_per_token1, t0_dec, t1_dec)
        tick = price_to_tick(token0_per_token1, t0_dec, t1_dec)

        pool_states[known_addr] = {
            "token0": t0_addr,
            "token1": t1_addr,
            "fee": fee,
            "sqrtPriceX96": str(sqrt_price),
            "tick": tick,
            "liquidity": _DEFAULT_LIQUIDITY,
            "description": f"{t0_sym}/{t1_sym} {fee / 1_000_000:.2%} (synthetic)",
        }

    return pool_states


def _build_synthetic_prices(chain_id: int) -> dict[str, float]:
    """Build price dict with both native and wrapped symbol keys.

    **BENCHMARK/TEST ONLY** — uses hardcoded SYNTHETIC_PRICES.
    E.g. on Ethereum: includes both "ETH/USD" and "WETH/USD" → 1850.0
    """
    prices = {f"{sym}/USD": price for sym, price in SYNTHETIC_PRICES.items()}
    # Add native symbol aliases (ETH/USD → same as WETH/USD, etc.)
    for native, wrapped in NATIVE_TO_WRAPPED.get(chain_id, {}).items():
        if wrapped in SYNTHETIC_PRICES:
            prices[f"{native}/USD"] = SYNTHETIC_PRICES[wrapped]
    return prices


def build_synthetic_snapshot(chain_id: int = 1) -> MarketSnapshot:
    """Build a synthetic snapshot for screening smoke tests.

    **BENCHMARK/TEST ONLY** — Production code should NOT call this.
    Solvers query real pool states via RPC. This function exists for:
    - Benchmark harness (deterministic solver comparison)
    - Screening smoke tests (Stage 3)
    - Unit tests that don't need real on-chain data

    Generates pool states for all known token pairs using hardcoded USD
    prices. Supports direct swaps between any pair and multi-hop routing
    via common intermediaries (WETH, USDC).
    """
    tokens = MONITORED_TOKENS.get(chain_id, MONITORED_TOKENS[1])
    dex_config = UNISWAP_V3_CONFIG.get(chain_id, UNISWAP_V3_CONFIG[1])

    import time as _time
    return MarketSnapshot(
        chain_id=chain_id,
        block_number=18500000,
        timestamp=int(_time.time()),
        prices=_build_synthetic_prices(chain_id),
        pool_states=_generate_synthetic_pools(chain_id),
        balances={
            tokens.get("USDC", ""): "10000000000",          # 10,000 USDC
            tokens.get("WETH", ""): "5000000000000000000",   # 5 WETH
            tokens.get("WBTC", ""): "50000000",              # 0.5 WBTC
            tokens.get("USDT", ""): "10000000000",           # 10,000 USDT
            tokens.get("DAI", ""): "10000000000000000000000", # 10,000 DAI
        },
        dex_config=dex_config,
    )


def build_synthetic_intents() -> list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]]:
    """Build the 3 synthetic intents used for Stage 3 screening.

    Returns:
        List of (intent, state, snapshot) tuples for:
        1. Simple swap (ETH→USDC, user-triggered)
        2. Limit order (USDC→ETH, auto-triggered)
        3. Multi-token (WBTC→USDC on Base, user-triggered)
    """
    eth_tokens = MONITORED_TOKENS[1]
    contract = "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"

    intents: list[tuple[AppIntentDefinition, IntentState, MarketSnapshot]] = []

    # 1. Simple swap: USDC → WETH on Ethereum
    intents.append((
        AppIntentDefinition(
            app_id="synthetic-swap-001",
            name="Synthetic USDC→WETH Swap",
            version="1.0.0",
            intent_type="swap",
            js_code="// synthetic",
            config=AppIntentConfig(
                supported_chains=[1],
                trigger_type=TriggerType.USER_TRIGGERED,
            ),
        ),
        IntentState(
            contract_address=contract,
            chain_id=1,
            nonce=1,
            owner="0x0000000000000000000000000000000000000001",
            raw_params={
                "input_token": eth_tokens["USDC"],
                "output_token": eth_tokens["WETH"],
                "input_amount": "1000000000",
                "min_output_amount": "500000000000000000",
            },
        ),
        build_synthetic_snapshot(chain_id=1),
    ))

    # 2. Limit order: USDC → WETH when price drops 5% (auto-triggered)
    intents.append((
        AppIntentDefinition(
            app_id="synthetic-limit-001",
            name="Synthetic Limit Order",
            version="1.0.0",
            intent_type="limit_order",
            js_code="// synthetic",
            config=AppIntentConfig(
                supported_chains=[1],
                trigger_type=TriggerType.AUTO_TRIGGERED,
            ),
        ),
        IntentState(
            contract_address=contract,
            chain_id=1,
            nonce=2,
            owner="0x0000000000000000000000000000000000000001",
            raw_params={
                "input_token": eth_tokens["USDC"],
                "output_token": eth_tokens["WETH"],
                "input_amount": "5000000000",
                "target_price": "1757.5",
            },
        ),
        build_synthetic_snapshot(chain_id=1),
    ))

    # 3. Multi-token: WBTC → USDC on Ethereum
    intents.append((
        AppIntentDefinition(
            app_id="synthetic-multi-001",
            name="Synthetic WBTC→USDC Swap",
            version="1.0.0",
            intent_type="swap",
            js_code="// synthetic",
            config=AppIntentConfig(
                supported_chains=[1],
                trigger_type=TriggerType.USER_TRIGGERED,
            ),
        ),
        IntentState(
            contract_address=contract,
            chain_id=1,
            nonce=3,
            owner="0x0000000000000000000000000000000000000001",
            raw_params={
                "input_token": eth_tokens["WBTC"],
                "output_token": eth_tokens["USDC"],
                "input_amount": "10000000",
                "min_output_amount": "4000000000",
            },
        ),
        build_synthetic_snapshot(chain_id=1),
    ))

    return intents


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
