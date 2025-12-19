"""OIF v1 compatible solver with Uniswap V3 price simulation on Base chain.

This solver implements the OIF v1 API specification and provides quotes
for token swaps using Uniswap V3 pools on Base (chain ID 8453).
It can run in standalone mode or be managed by a miner.
"""

import json
import math
import os
import random
import secrets
import time
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Dict, List, Optional, Tuple

import requests
from eth_abi.abi import encode
from eth_utils import keccak, to_canonical_address
from flask import Flask, jsonify, request
from itertools import chain

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip .env loading

try:
    from web3 import Web3
    from web3.exceptions import BadFunctionCallOutput, ContractLogicError
    WEB3_AVAILABLE = True
except ImportError:
    Web3 = None  # type: ignore
    BadFunctionCallOutput = Exception  # type: ignore
    ContractLogicError = Exception  # type: ignore
    WEB3_AVAILABLE = False


def create_interop_address(chain_id: int, eth_address: str) -> str:
    """Create an ERC-7930 interoperable address in hex format."""
    if eth_address.startswith("0x"):
        eth_address = eth_address[2:]
    
    address_bytes = bytes.fromhex(eth_address)
    if len(address_bytes) != 20:
        raise ValueError(f"Invalid Ethereum address length: {len(address_bytes)} (expected 20)")
    
    chain_id_bytes = chain_id.to_bytes(8, 'big')
    first_nonzero = next((i for i, b in enumerate(chain_id_bytes) if b != 0), 7)
    chain_reference = chain_id_bytes[first_nonzero:]
    
    result = bytes([
        0x01,  # Version
        0x00, 0x00,  # ChainType (EIP-155)
        len(chain_reference),  # ChainReferenceLength
        0x14,  # AddressLength (20 bytes for Ethereum)
    ])
    result += chain_reference
    result += address_bytes
    
    return "0x" + result.hex()


# Base chain ID
CHAIN_ID = 8453

# USDT on Base (requires two-step approval: reset to 0, then set amount)
# Note: USDT has limited liquidity on Base, USDbC and USDC are more common
USDT_ADDRESS = "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"

# Uniswap V3 contract addresses on Base
UNISWAP_ROUTER_ADDRESS = "0x2626664c2603336E57B271c5C0b26F421741e481"  # SwapRouter02
UNISWAP_ROUTER_INTEROP = create_interop_address(CHAIN_ID, UNISWAP_ROUTER_ADDRESS)
ZERO_INTEROP_ADDRESS = create_interop_address(
    CHAIN_ID, "0x0000000000000000000000000000000000000000"
)
UNISWAP_V3_FACTORY_ADDRESS = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_ADDRESS = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"  # QuoterV2

# WETH on Base (the native wrapped token)
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

UNISWAP_TOKEN_LIST_URL = os.environ.get(
    "UNISWAP_TOKEN_LIST_URL", "https://tokens.uniswap.org"
)
# CoinGecko comprehensive token list for additional token discovery
COINGECKO_TOKEN_LIST_URL = os.environ.get(
    "COINGECKO_TOKEN_LIST_URL", "https://tokens.coingecko.com/base/all.json"
)
DEFAULT_TOKEN_ADVERTISE_LIMIT = int(os.environ.get("MOCK_SOLVER_TOKEN_LIMIT", "10000"))
DEFAULT_POOL_FEE_TIERS = [100, 500, 3000, 10000]
SETTLEMENT_CONTRACT_ADDRESS = os.environ.get(
    "SETTLEMENT_CONTRACT_ADDRESS_BASE",
    os.environ.get("SETTLEMENT_CONTRACT_ADDRESS", "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"),
)

ERC20_ABI = [
    {"name": "decimals", "outputs": [{"type": "uint8"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

UNISWAP_V3_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# QuoterV2 ABI - uses struct-based parameters instead of individual params
# This is different from the original Quoter on Ethereum mainnet
UNISWAP_V3_QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Base chain token metadata - popular tokens on Base
BASE_TOKEN_METADATA: Dict[str, Dict[str, object]] = {
    # Native WETH
    "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "decimals": 18},
    # Zero address for ETH
    "0x0000000000000000000000000000000000000000": {"symbol": "ETH", "decimals": 18},
    # USDC (native, bridged from Ethereum via Circle)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "decimals": 6},
    # USDbC (Bridged USDC via Base Bridge - legacy)
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": {"symbol": "USDbC", "decimals": 6},
    # DAI
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": {"symbol": "DAI", "decimals": 18},
    # cbETH (Coinbase Wrapped Staked ETH)
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": {"symbol": "cbETH", "decimals": 18},
    # COMP (Compound)
    "0x9e1028f5f1d5ede59748ffcee5532509976840e0": {"symbol": "COMP", "decimals": 18},
    # AERO (Aerodrome)
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631": {"symbol": "AERO", "decimals": 18},
    # BRETT (popular memecoin on Base)
    "0x532f27101965dd16442e59d40670faf5ebb142e4": {"symbol": "BRETT", "decimals": 18},
    # DEGEN
    "0x4ed4e862860bed51a9570b96d89af5e1b0efefed": {"symbol": "DEGEN", "decimals": 18},
    # TOSHI
    "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4": {"symbol": "TOSHI", "decimals": 18},
    # HIGHER
    "0x0578d8a44db98b23bf096a382e016e29a5ce0ffe": {"symbol": "HIGHER", "decimals": 18},
    # USDT on Base
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": {"symbol": "USDT", "decimals": 6},
}

getcontext().prec = 64


class SolverBase:
    """OIF v1 compatible solver with Uniswap V3 integration on Base chain."""
    
    def __init__(
        self,
        solver_id: str,
        port: int,
        latency_ms: int = 100,
        quality: float = 1.0,
        logger=None
    ):
        self.solver_id = solver_id
        self.port = port
        self.latency_ms = latency_ms
        self.quality = quality
        self.logger = logger
        self.request_count = 0
        self.advertised_token_limit = DEFAULT_TOKEN_ADVERTISE_LIMIT
        self.token_metadata: Dict[str, Dict[str, Any]] = dict(BASE_TOKEN_METADATA)
        self._load_token_list()
        if os.environ.get("ENABLE_POOL_TOKEN_DISCOVERY", "true").lower() in ("1", "true", "yes"):
            self._discover_tokens_from_pools()  # Discover additional tokens from Uniswap V3 pools
        self.web3 = self._init_web3()
        if self.web3 and not self.web3.is_connected():
            self.web3 = None
        
        if self.web3 is None:
            if self.logger:
                self.logger.error(
                    f"❌ CRITICAL: Failed to connect to Base RPC! "
                    f"All quote requests will fail. "
                    f"Set BASE_RPC_URL environment variable to a valid Base RPC endpoint."
                )
        else:
            if self.logger:
                self.logger.success(f"✅ Connected to Base RPC - quoter ready")
        
        self.factory_contract = (
            self.web3.eth.contract(
                address=self.web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS),
                abi=UNISWAP_V3_FACTORY_ABI,
            )
            if self.web3
            else None
        )
        self.quoter_contract = (
            self.web3.eth.contract(
                address=self.web3.to_checksum_address(UNISWAP_V3_QUOTER_ADDRESS),
                abi=UNISWAP_V3_QUOTER_V2_ABI,
            )
            if self.web3
            else None
        )
        self.pool_cache: Dict[Tuple[str, str, int], Optional[str]] = {}
        self.supported_tokens: List[Dict[str, Any]] = self._build_supported_tokens()
        self.app = Flask(f"solver-base-{solver_id}")
        self.setup_routes()
        self.orders = {}
        self.quote_cache: Dict[str, dict] = {}
    
    def _init_web3(self) -> Optional["Web3"]:
        if not WEB3_AVAILABLE:
            if self.logger:
                self.logger.warning("Web3 library not available - install web3 package")
            return None

        # Build list of RPC URLs to try
        rpc_urls = []
        
        # Priority 1: Explicit BASE_RPC_URL
        base_rpc = os.getenv("BASE_RPC_URL")
        if base_rpc:
            rpc_urls.append(("BASE_RPC_URL", base_rpc))
        
        # Priority 2: Alchemy with API key
        alchemy_key = os.getenv("ALCHEMY_API_KEY")
        if alchemy_key:
            rpc_urls.append(("Alchemy", f"https://base-mainnet.g.alchemy.com/v2/{alchemy_key}"))
        
        # Priority 3: Public RPCs (try multiple)
        public_rpcs = [
            ("Base Official", "https://mainnet.base.org"),
            ("Ankr", "https://rpc.ankr.com/base"),
            ("PublicNode", "https://base.publicnode.com"),
        ]
        rpc_urls.extend(public_rpcs)

        for rpc_name, rpc_url in rpc_urls:
            try:
                if self.logger:
                    self.logger.debug(f"Trying Base RPC: {rpc_name} ({rpc_url[:50]}...)")
                
                provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10})
                web3 = Web3(provider)
                
                if web3.is_connected():
                    chain_id = web3.eth.chain_id
                    if chain_id != CHAIN_ID:
                        if self.logger:
                            self.logger.warning(f"RPC {rpc_name} returned wrong chain (expected {CHAIN_ID}, got {chain_id})")
                        continue
                    if self.logger:
                        self.logger.info(f"Connected to Base RPC via {rpc_name} (chain ID {chain_id})")
                    return web3
                else:
                    if self.logger:
                        self.logger.debug(f"RPC {rpc_name} not connected")
            except Exception as exc:
                if self.logger:
                    self.logger.debug(f"Failed to connect to {rpc_name}: {exc}")
                continue
        
        if self.logger:
            self.logger.error(f"Failed to connect to any Base RPC endpoint. Tried {len(rpc_urls)} endpoints.")
        return None

    def _load_token_list(self):
        """Load token list from token lists (optional, non-blocking)."""
        if os.environ.get("DISABLE_UNISWAP_TOKEN_LIST", "").lower() in ("1", "true", "yes"):
            return

        # Try multiple token sources in order of preference
        token_sources = [
            ("Uniswap tokens", UNISWAP_TOKEN_LIST_URL),
            ("CoinGecko Base tokens", COINGECKO_TOKEN_LIST_URL),
        ]

        total_added = 0
        for source_name, url in token_sources:
            if total_added >= self.advertised_token_limit:
                break

            try:
                response = requests.get(
                    url,
                    timeout=5,
                    headers={"User-Agent": "OIF-Aggregator-Solver/1.0"}
                )
                response.raise_for_status()
                data = response.json()
                tokens = data.get("tokens", [])

                # Filter to Base chain only for Uniswap list
                if "coingecko" not in url.lower():
                    tokens = [t for t in tokens if t.get("chainId") == CHAIN_ID]

                added = 0
                for token in tokens:
                    address = token["address"].lower()
                    if address in self.token_metadata:
                        continue
                    self.token_metadata[address] = {
                        "symbol": token.get("symbol", address[-4:].upper()),
                        "decimals": int(token.get("decimals", 18)),
                    }
                    added += 1
                    total_added += 1
                    if total_added >= self.advertised_token_limit:
                        break
                if added and self.logger:
                    self.logger.info(f"Loaded {added} additional tokens from {source_name} list")
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Failed to load tokens from {source_name} list: {e}")
                continue

        if total_added and self.logger:
            self.logger.info(f"Total tokens loaded from token lists: {total_added}")

    def _discover_tokens_from_pools(self):
        """Discover additional tokens by scanning recent Uniswap V3 pools on Base."""
        if not WEB3_AVAILABLE or os.environ.get("DISABLE_TOKEN_DISCOVERY", "").lower() in ("1", "true", "yes"):
            return

        try:
            # Initialize Web3 for token discovery
            web3 = self._init_web3()
            if not web3:
                return

            # We'll scan recent blocks for PoolCreated events from the Uniswap V3 factory
            factory_address = web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS)

            # Get current block and scan last N blocks (adjustable via env var)
            current_block = web3.eth.block_number
            blocks_to_scan = int(os.environ.get("POOL_DISCOVERY_BLOCKS", "10000"))  # Default: ~2-3 days on Base
            from_block = max(0, current_block - blocks_to_scan)

            if self.logger:
                self.logger.info(f"Scanning Uniswap V3 pools on Base from block {from_block} to {current_block}")

            # PoolCreated event signature
            pool_created_topic = web3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()

            # Query for PoolCreated events
            logs = web3.eth.get_logs({
                "address": factory_address,
                "topics": [pool_created_topic],
                "fromBlock": from_block,
                "toBlock": current_block
            })

            discovered_tokens = set()
            for log in logs:
                try:
                    # Decode the PoolCreated event
                    # Indexed parameters appear in topics: topics[1] = token0, topics[2] = token1, topics[3] = fee
                    topics = log["topics"]

                    if len(topics) >= 4:  # Make sure we have all indexed parameters
                        # Topics contain 32-byte values, extract last 20 bytes for addresses
                        token0_hex = topics[1].hex()[-40:]  # Last 20 bytes (40 hex chars) of the topic
                        token1_hex = topics[2].hex()[-40:]  # Last 20 bytes (40 hex chars) of the topic

                        token0 = web3.to_checksum_address("0x" + token0_hex)
                        token1 = web3.to_checksum_address("0x" + token1_hex)

                        # Add tokens to discovery set
                        discovered_tokens.add(token0.lower())
                        discovered_tokens.add(token1.lower())
                except Exception as e:
                    if self.logger:
                        self.logger.debug(f"Failed to decode pool creation log: {e}")

            # Fetch metadata for discovered tokens
            added = 0
            for token_address in discovered_tokens:
                if token_address in self.token_metadata:
                    continue  # Already known

                # Fetch on-chain metadata
                metadata = self._fetch_onchain_token_metadata(token_address)
                if metadata:
                    self.token_metadata[token_address] = metadata
                    added += 1

                    # Stop if we hit the limit
                    if len(self.token_metadata) >= self.advertised_token_limit:
                        break

            if added and self.logger:
                self.logger.info(f"Discovered {added} additional tokens from Uniswap V3 pools on Base")

        except Exception as e:
            if self.logger:
                self.logger.warning(f"Token discovery from pools failed: {e}")

    def _build_supported_tokens(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for address, meta in list(self.token_metadata.items())[: self.advertised_token_limit]:
            entries.append(
                {
                    "address": self._checksum_address(address),
                    "symbol": meta.get("symbol", address[-4:].upper()),
                    "decimals": meta.get("decimals", 18),
                }
            )
        return entries

    def _checksum_address(self, address: str) -> str:
        if self.web3:
            try:
                return self.web3.to_checksum_address(address)
            except Exception:
                pass
        return address if address.startswith("0x") else f"0x{address}"

    def _update_supported_tokens(self, address: str, metadata: Dict[str, Any]):
        """Add token to supported_tokens list if not already present."""
        checksum = self._checksum_address(address)
        for token in self.supported_tokens:
            if token["address"].lower() == checksum.lower():
                return
        self.supported_tokens.insert(0, {
            "address": checksum,
            "symbol": metadata.get("symbol", checksum[-4:].upper()),
            "decimals": metadata.get("decimals", 18),
        })

    def _fetch_onchain_token_metadata(self, address: str) -> Optional[Dict[str, Any]]:
        if not self.web3:
            return None
        try:
            contract = self.web3.eth.contract(
                address=self.web3.to_checksum_address(address), abi=ERC20_ABI
            )
            decimals = contract.functions.decimals().call()
            symbol = contract.functions.symbol().call()
            if isinstance(symbol, bytes):
                symbol = symbol.decode("utf-8", errors="ignore").strip("\x00")
            return {"symbol": symbol or address[-4:].upper(), "decimals": int(decimals)}
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Unable to fetch metadata for {address}: {exc}")
        return None

    def _interop_to_components(self, interop_address: str) -> Tuple[int, str]:
        """Decode ERC-7930 interop address into (chain_id, 0x-address)"""
        try:
            raw = interop_address[2:] if interop_address.startswith("0x") else interop_address
            data = bytes.fromhex(raw)

            if len(data) < 5:
                raise ValueError("Interop address too short")

            chain_ref_len = data[3]
            address_len = data[4]

            offset = 5
            chain_ref = data[offset : offset + chain_ref_len]
            offset += chain_ref_len
            address_bytes = data[offset : offset + address_len]

            chain_id = int.from_bytes(chain_ref, "big") if chain_ref else 0
            if chain_id == 0:
                chain_id = CHAIN_ID

            address = "0x" + address_bytes.hex()
            return chain_id, address.lower()
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to parse interop address {interop_address}: {exc}")
            return CHAIN_ID, "0x0000000000000000000000000000000000000000"

    def _get_token_metadata(self, interop_address: str) -> Dict[str, object]:
        chain_id, eth_address = self._interop_to_components(interop_address)
        key = eth_address.lower()
        meta = self.token_metadata.get(key)
        if meta is None:
            onchain_meta = self._fetch_onchain_token_metadata(eth_address)
            if onchain_meta:
                meta = onchain_meta
                self.token_metadata[key] = meta
                self._update_supported_tokens(key, meta)
            else:
                meta = {"symbol": eth_address[-4:].upper(), "decimals": 18}
                self.token_metadata[key] = meta
                self._update_supported_tokens(key, meta)

        symbol = meta.get("symbol", eth_address[-4:].upper())
        decimals = meta.get("decimals", 18)

        return {
            "chain_id": chain_id,
            "address": eth_address,
            "symbol": symbol,
            "decimals": decimals,
        }

    def _to_wei(self, amount: Decimal, decimals: int) -> int:
        scale = Decimal(10) ** decimals
        return int((amount * scale).to_integral_value(rounding=ROUND_DOWN))

    def _from_wei(self, amount_wei: int, decimals: int) -> Decimal:
        scale = Decimal(10) ** decimals
        return Decimal(amount_wei) / scale

    def _select_pool_fee(self, token_in: str, token_out: str, preferred_fee: Optional[int]) -> int:
        if preferred_fee:
            return preferred_fee

        stablecoins = {"USDC", "USDbC", "USDT", "DAI"}

        if token_in in stablecoins and token_out in stablecoins:
            return 100

        if (token_in in stablecoins and token_out == "WETH") or (
            token_out in stablecoins and token_in == "WETH"
        ):
            return 500

        if {token_in, token_out} == {"cbETH", "WETH"}:
            return 500

        return 3000

    def _find_pool_address(self, token_in: str, token_out: str, fee_tier: int) -> Optional[str]:
        key = (token_in.lower(), token_out.lower(), fee_tier)
        if key in self.pool_cache:
            return self.pool_cache[key]

        if not self.factory_contract:
            self.pool_cache[key] = None
            return None

        try:
            pool = self.factory_contract.functions.getPool(
                self.web3.to_checksum_address(token_in),
                self.web3.to_checksum_address(token_out),
                fee_tier,
            ).call()
            if pool and int(pool, 16) != 0:
                self.pool_cache[key] = pool
                return pool
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to lookup pool for {token_in}/{token_out} (fee {fee_tier}): {exc}")

        self.pool_cache[key] = None
        return None

    def _quote_via_uniswap(self, token_in: str, token_out: str, fee_tier: int, amount_in_wei: int) -> Optional[int]:
        """Get quote from Uniswap V3 QuoterV2 on Base."""
        if not self.quoter_contract or amount_in_wei == 0:
            return None
        try:
            # QuoterV2 uses struct-based parameters
            params = (
                self.web3.to_checksum_address(token_in),  # tokenIn
                self.web3.to_checksum_address(token_out),  # tokenOut
                amount_in_wei,  # amountIn
                fee_tier,  # fee
                0,  # sqrtPriceLimitX96
            )
            # QuoterV2 returns (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
            result = self.quoter_contract.functions.quoteExactInputSingle(params).call()
            amount_out = result[0]  # First return value is amountOut
            return int(amount_out)
        except ContractLogicError:
            return None
        except Exception as exc:
            if self.logger:
                error_type = type(exc).__name__
                self.logger.warning(f"Unexpected QuoterV2 error for {token_in}/{token_out} (fee {fee_tier}): {error_type}: {exc}")
            return None

    def _estimate_gas_units(self, token_in: str, token_out: str, fee_tier: int) -> int:
        # Base has lower gas costs but we still estimate conservatively
        base = 120_000
        if fee_tier == 100:
            base = 90_000
        if {token_in, token_out} == {"cbETH", "WETH"}:
            base = 150_000
        jitter = random.randint(-10_000, 10_000)
        return max(80_000, base + jitter)

    def _generate_nonce(self) -> str:
        return "0x" + secrets.token_hex(16)

    def _int_to_hex(self, value: int) -> str:
        return hex(int(value))

    def _safe_float(self, value) -> Optional[float]:
        """Return a JSON-safe float or None if the value cannot be represented."""
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if math.isfinite(numeric):
            return numeric
        return None

    def _address_to_bytes32(self, address: str) -> str:
        addr = address.lower().replace("0x", "")
        return "0x" + addr.rjust(64, "0")

    def _encode_erc20_approve_calldata(self, spender: str, amount: int) -> str:
        """Encode ERC20 approve(address spender, uint256 amount) call."""
        encoded_params = encode(
            ['address', 'uint256'],
            [spender, amount],
        )
        selector = keccak(text='approve(address,uint256)')[:4]
        return '0x' + (selector + encoded_params).hex()

    def _encode_exact_input_single_calldata(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        recipient: str,
        amount_in: int,
        amount_out_min: int,
    ) -> str:
        """Encode exactInputSingle call for SwapRouter02 (no deadline in params).
        
        SwapRouter02 on Base uses 7 parameters (no deadline):
        - tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96
        
        Deadline is handled via multicall wrapper if needed.
        """
        params_tuple = (
            token_in,
            token_out,
            fee,
            recipient,
            amount_in,
            amount_out_min,
            0,  # sqrtPriceLimitX96
        )

        # SwapRouter02 signature: 7 params, no deadline
        encoded_params = encode(
            ['(address,address,uint24,address,uint256,uint256,uint160)'],
            [params_tuple],
        )

        selector = keccak(
            text='exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))'
        )[:4]

        return '0x' + (selector + encoded_params).hex()

    def _build_route_details(
        self,
        token_in_meta: Dict[str, object],
        token_out_meta: Dict[str, object],
        input_amount_wei: int,
        expected_output_wei: int,
        min_output_wei: int,
        fee_tier: int,
        pool_address: Optional[str],
    ) -> Dict[str, object]:
        return {
            "engine": "uniswap-v3-base",
            "chainId": CHAIN_ID,
            "poolAddress": pool_address,
            "feeTier": fee_tier,
            "path": [
                {
                    "tokenIn": token_in_meta["address"],
                    "tokenOut": token_out_meta["address"],
                    "tokenInSymbol": token_in_meta["symbol"],
                    "tokenOutSymbol": token_out_meta["symbol"],
                    "tokenInDecimals": token_in_meta["decimals"],
                    "tokenOutDecimals": token_out_meta["decimals"],
                    "feeTier": fee_tier,
                    "amountInWei": str(input_amount_wei),
                    "expectedAmountOutWei": str(expected_output_wei),
                    "minAmountOutWei": str(min_output_wei),
                }
            ],
            "estimatedGasUnits": self._estimate_gas_units(
                token_in_meta["symbol"], token_out_meta["symbol"], fee_tier
            ),
        }

    def _build_order_components(
        self,
        request: dict,
        quote_id: str,
        token_in_meta: Dict[str, object],
        token_out_meta: Dict[str, object],
        input_amount_wei: int,
        expected_output_wei: int,
        min_output_wei: int,
        fee_tier: int,
        pool_address: Optional[str],
        block_number: Optional[str] = None,
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        user_interop = request.get("user")
        user_chain, user_address = (
            self._interop_to_components(user_interop)
            if user_interop
            else (CHAIN_ID, "0x0000000000000000000000000000000000000000")
        )

        route = self._build_route_details(
            token_in_meta,
            token_out_meta,
            input_amount_wei,
            expected_output_wei,
            min_output_wei,
            fee_tier,
            pool_address,
        )

        deadline = int(time.time()) + 600

        receiver_interop = request["requestedOutputs"][0].get("receiver")
        _, receiver_address = (
            self._interop_to_components(receiver_interop)
            if receiver_interop
            else (user_chain, user_address)
        )

        settlement_recipient = SETTLEMENT_CONTRACT_ADDRESS

        calldata = self._encode_exact_input_single_calldata(
            token_in_meta["address"],
            token_out_meta["address"],
            fee_tier,
            settlement_recipient,
            input_amount_wei,
            min_output_wei,
        )

        nonce = self._generate_nonce()

        # Two-level approval chain:
        # 1. User -> Settlement: Handled by permit field (standard_approval)
        #    This allows Settlement to transfer tokens from the user
        # 2. Settlement -> Uniswap router: Handled by preInteraction
        #    PreInteractions are executed BY Settlement contract, so when Settlement
        #    executes approve(UniswapRouter), it's Settlement (not the user) approving
        #    Uniswap router. Settlement must have tokens before approving.
        
        token_address = token_in_meta["address"]
        
        # Build preInteractions for approval
        # Always use two-step approval (reset to 0, then set amount) for all tokens.
        # This is safe for all ERC-20 tokens and prevents issues with tokens like USDT
        # that revert if approve() is called with a non-zero amount when allowance is already non-zero.
        pre_interactions = []
        
        # Step 1: Reset approval to 0 (safe for all tokens, prevents allowance issues)
        reset_approval_calldata = self._encode_erc20_approve_calldata(
            UNISWAP_ROUTER_ADDRESS,
            0,  # Reset to zero
        )
        pre_interactions.append({
            "target": token_address,
            "value": "0",
            "callData": reset_approval_calldata,
        })
        
        # Step 2: Set approval to desired amount
        approve_uniswap_calldata = self._encode_erc20_approve_calldata(
            UNISWAP_ROUTER_ADDRESS,
            input_amount_wei,
        )
        pre_interactions.append({
            "target": token_address,
            "value": "0",
            "callData": approve_uniswap_calldata,  # Settlement approves Uniswap router
        })
        
        if self.logger:
            self.logger.debug(
                f"Built 2 preInteraction(s) for {token_in_meta.get('symbol', 'UNKNOWN')} "
                f"(reset to 0, then approve {input_amount_wei})"
            )

        interactions = [
            {
                "target": UNISWAP_ROUTER_ADDRESS,
                "value": "0",
                "callData": calldata,
            }
        ]

        execution_plan = {
            "preInteractions": pre_interactions,
            "interactions": interactions,
            "postInteractions": [],
        }

        # Use the block number passed in (from when quote was computed)
        # If not provided, try to fetch current block number as fallback
        if block_number is None and self.web3:
            try:
                block_number = str(self.web3.eth.block_number)
            except Exception:
                pass

        if block_number:
            execution_plan["blockNumber"] = block_number

        interactions_hash = self._compute_interactions_hash(execution_plan)
        all_interactions = (
            execution_plan.get("preInteractions", [])
            + execution_plan.get("interactions", [])
            + execution_plan.get("postInteractions", [])
        )
        call_value_int = sum(int(inter.get("value", "0"), 10) for inter in all_interactions)
        call_value_str = str(call_value_int)

        settlement_plan = {
            "contractAddress": SETTLEMENT_CONTRACT_ADDRESS,
            "deadline": deadline,
            "nonce": nonce,
            "callValue": call_value_str,
            "gasEstimate": route["estimatedGasUnits"],
            "interactionsHash": interactions_hash,
            "permit": {
                "permitType": "standard_approval",
                "permitCall": "0x",
                "amount": str(input_amount_wei),
                "deadline": deadline,
            },
            "executionPlan": execution_plan,
        }

        order_message = {
            "target": SETTLEMENT_CONTRACT_ADDRESS,
            "calldata": "0x",
            "callValue": "0x0",
            "deadline": deadline,
            "nonce": nonce,
            "gasEstimate": route["estimatedGasUnits"],
        }

        context = {
            "solver": self.solver_id,
            "quoteId": quote_id,
            "originChainId": token_in_meta["chain_id"],
            "user": {
                "interop": user_interop,
                "address": user_address,
                "chainId": user_chain,
            },
            "input": {
                "asset": token_in_meta["address"],
                "symbol": token_in_meta["symbol"],
                "interop": request["availableInputs"][0]["asset"],
                "amountWei": str(input_amount_wei),
                "amount": str(self._from_wei(input_amount_wei, token_in_meta["decimals"])),
                "decimals": token_in_meta["decimals"],
                "chainId": token_in_meta["chain_id"],
            },
            "output": {
                "asset": token_out_meta["address"],
                "symbol": token_out_meta["symbol"],
                "interop": request["requestedOutputs"][0]["asset"],
                "estimatedAmountWei": str(expected_output_wei),
                "minimumAmountWei": str(min_output_wei),
                "estimatedAmount": str(
                    self._from_wei(expected_output_wei, token_out_meta["decimals"])
                ),
                "decimals": token_out_meta["decimals"],
                "chainId": token_out_meta["chain_id"],
            },
            "deadline": deadline,
            "nonce": nonce,
            "receiver": receiver_address,
            "input_amount_wei": input_amount_wei,
            "expected_output_wei": expected_output_wei,
            "min_output_wei": min_output_wei,
            "gasEstimate": route["estimatedGasUnits"],
            "settlement": settlement_plan,
        }

        return order_message, context

    def _compute_interactions_hash(self, plan):
        """Compute canonical keccak256 hash of the execution plan."""
        encoded = bytearray()
        for interaction in chain(
            plan.get("preInteractions", []),
            plan.get("interactions", []),
            plan.get("postInteractions", []),
        ):
            target = interaction.get("target", "")
            if target:
                target_bytes = to_canonical_address(target)
                encoded.extend(target_bytes)
            else:
                encoded.extend(bytes(20))

            value_str = interaction.get("value", "0")
            try:
                value_int = int(value_str, 10)
            except (ValueError, TypeError):
                value_int = 0
            value_bytes = value_int.to_bytes(32, byteorder="big")
            encoded.extend(value_bytes)

            call_data_hex = interaction.get("callData", "0x")
            if call_data_hex.startswith("0x"):
                call_data_hex = call_data_hex[2:]
            call_data_bytes = bytes.fromhex(call_data_hex) if call_data_hex else b""
            call_hash = keccak(call_data_bytes)
            encoded.extend(call_hash)

        final_hash = keccak(bytes(encoded))
        return "0x" + final_hash.hex()

    def setup_routes(self):
        """Setup Flask routes for OIF v1 API"""
        
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "healthy",
                "solver_id": self.solver_id,
                "chain": "base",
                "chainId": CHAIN_ID,
                "engine": "uniswap-v3"
            }), 200
        
        @self.app.route('/quotes', methods=['POST'])
        def get_quotes():
            """Handle quote requests - OIF v1 format"""
            try:
                if self.logger:
                    self.logger.debug(f"Received quote request from {request.remote_addr}")

                req = request.json

                if req is None:
                    if self.logger:
                        self.logger.warning(f"Received empty or invalid JSON body from {request.remote_addr}")
                    return jsonify({"error": "Invalid request: empty or invalid JSON"}), 400

                if self.logger:
                    self.logger.debug(f"Request content: availableInputs={len(req.get('availableInputs', []))} requestedOutputs={len(req.get('requestedOutputs', []))}")

                available_inputs = req.get('availableInputs')
                requested_outputs = req.get('requestedOutputs')

                if not available_inputs or not requested_outputs:
                    error_msg = f"Invalid request: missing required fields (availableInputs={available_inputs is not None}, requestedOutputs={requested_outputs is not None})"
                    if self.logger:
                        self.logger.warning(f"{error_msg}")
                    return jsonify({"error": error_msg}), 400

                if not isinstance(available_inputs, list) or len(available_inputs) == 0:
                    error_msg = f"Invalid request: availableInputs must be a non-empty array (got {type(available_inputs)})"
                    if self.logger:
                        self.logger.warning(f"{error_msg}")
                    return jsonify({"error": error_msg}), 400

                if not isinstance(requested_outputs, list) or len(requested_outputs) == 0:
                    error_msg = f"Invalid request: requestedOutputs must be a non-empty array (got {type(requested_outputs)})"
                    if self.logger:
                        self.logger.warning(f"{error_msg}")
                    return jsonify({"error": error_msg}), 400
                
                input_asset = req['availableInputs'][0]
                output_asset = req['requestedOutputs'][0]

                token_in_meta = self._get_token_metadata(input_asset['asset'])
                token_out_meta = self._get_token_metadata(output_asset['asset'])

                if self.logger:
                    self.logger.debug(
                        f"Quote request: {token_in_meta['symbol']} (chain {token_in_meta['chain_id']}, {token_in_meta['address']}) "
                        f"-> {token_out_meta['symbol']} (chain {token_out_meta['chain_id']}, {token_out_meta['address']})"
                    )

                # Verify tokens are on Base chain
                if token_in_meta["chain_id"] != CHAIN_ID:
                    if self.logger:
                        self.logger.info(
                            f"⚠️  Skipping: Input token {token_in_meta['symbol']} not on Base chain "
                            f"(chain_id={token_in_meta['chain_id']}, expected {CHAIN_ID}). "
                            f"Interop: {input_asset['asset'][:30]}..."
                        )
                    return jsonify({"quotes": []}), 200
                
                if token_out_meta["chain_id"] != CHAIN_ID:
                    if self.logger:
                        self.logger.info(
                            f"⚠️  Skipping: Output token {token_out_meta['symbol']} not on Base chain "
                            f"(chain_id={token_out_meta['chain_id']}, expected {CHAIN_ID}). "
                            f"Interop: {output_asset['asset'][:30]}..."
                        )
                    return jsonify({"quotes": []}), 200

                input_amount_wei = int(input_asset['amount'])

                self.request_count += 1
                quote_id = f"{self.solver_id}-base-q-{int(time.time() * 1000)}-{self.request_count}"
                
                # Fetch block number BEFORE computing quote to ensure consistency
                # The execution plan will use this same block number
                quote_block_number = None
                if self.web3:
                    try:
                        quote_block_number = str(self.web3.eth.block_number)
                    except Exception:
                        pass
                
                fee_tier = None
                onchain_output = None
                
                for fee in DEFAULT_POOL_FEE_TIERS:
                    output = self._quote_via_uniswap(
                        token_in_meta["address"],
                        token_out_meta["address"],
                        fee,
                        input_amount_wei,
                    )
                    if output and output > 0:
                        fee_tier = fee
                        onchain_output = output
                        break
                
                if not onchain_output or not fee_tier:
                    if self.logger:
                        self.logger.info(
                            f"No liquidity found for {token_in_meta['symbol']}/{token_out_meta['symbol']} on Uniswap V3 Base | "
                            f"tokenIn={token_in_meta['address']} tokenOut={token_out_meta['address']} | "
                            f"amount={input_amount_wei} | web3_connected={self.web3 is not None and self.web3.is_connected()}"
                        )
                    # Return empty quotes array to indicate no quotes available for this pair
                    # This is the proper OIF response for unsupported pairs
                    return jsonify({"quotes": []}), 200
                
                pool_address = self._find_pool_address(
                    token_in_meta["address"],
                    token_out_meta["address"],
                    fee_tier,
                )
                
                expected_output_wei = onchain_output
                expected_output_amount = self._from_wei(
                    expected_output_wei, token_out_meta["decimals"]
                )

                # Apply 1 wei slippage tolerance to account for rounding errors
                # This prevents failures when the swap returns exactly 1 wei less than expected
                min_output_wei = max(0, expected_output_wei - 1)

                order_message, order_context = self._build_order_components(
                    req,
                    quote_id,
                    token_in_meta,
                    token_out_meta,
                    input_amount_wei,
                    expected_output_wei,
                    min_output_wei,
                    fee_tier,
                    pool_address,
                    block_number=quote_block_number,  # Use the block number from when quote was computed
                )

                input_user_interop = input_asset.get("user") or req.get("user")

                input_entry = {
                    "asset": input_asset["asset"],
                    "amount": str(input_amount_wei),
                    "user": input_user_interop,
                }

                output_receiver_interop = (
                    output_asset.get("receiver")
                    or req.get("requestedOutputs", [{}])[0].get("receiver")
                    or input_user_interop
                )

                output_entry = {
                    "asset": output_asset["asset"],
                    "amount": str(expected_output_wei),
                    "receiver": output_receiver_interop,
                }

                quote_details = {
                    "requestedOutputs": [output_entry],
                    "availableInputs": [input_entry],
                }

                settlement_plan = order_context["settlement"]

                response_quote = {
                    "quoteId": quote_id,
                    "provider": self.solver_id,
                    "orders": [],
                    "details": quote_details,
                    "validUntil": settlement_plan["deadline"],
                    "eta": random.randint(5, 30),  # Base is faster than mainnet
                    "settlement": settlement_plan,
                }

                self.quote_cache[quote_id] = {
                    "message": order_message,
                    "context": order_context,
                    "details": quote_details,
                    "created_at": time.time(),
                }

                if self.logger:
                    input_amount = self._from_wei(input_amount_wei, token_in_meta["decimals"])
                    self.logger.info(
                        f"Quote generated (Base): {quote_id} | "
                        f"{token_in_meta['symbol']} -> {token_out_meta['symbol']} | "
                        f"{input_amount} -> {expected_output_amount} | "
                        f"fee: {fee_tier}bps"
                    )

                if self.latency_ms > 0:
                    time.sleep(self.latency_ms / 1000.0)

                return jsonify({"quotes": [response_quote]}), 200
                
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error processing quote request: {e}")
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/orders', methods=['POST'])
        def submit_order():
            """Handle order submissions - OIF v1 format"""
            try:
                req = request.json
                order_id = f"{self.solver_id}-base-order-{int(time.time() * 1000)}"
                quote_id = req.get('quoteId')
                cached_quote = self.quote_cache.get(quote_id)

                now = int(time.time())

                order_ctx = None

                if cached_quote:
                    order_msg = cached_quote['message']
                    order_ctx = cached_quote['context']
                    input_amount_wei = int(order_ctx['input']['amountWei'])
                    min_output_wei = int(order_ctx['output']['minimumAmountWei'])
                    expected_output_wei = int(order_ctx['output']['estimatedAmountWei'])

                    standard_order = {
                        "expires": order_msg['deadline'],
                        "fillDeadline": order_msg['deadline'] + 300,
                        "inputOracle": "0x0000000000000000000000000000000000000000",
                        "inputs": [
                            [
                                self._int_to_hex(input_amount_wei),
                                self._int_to_hex(min_output_wei),
                            ]
                        ],
                        "nonce": order_msg['nonce'],
                        "originChainId": self._int_to_hex(order_ctx['originChainId']),
                        "outputs": [
                            {
                                "amount": self._int_to_hex(expected_output_wei),
                                "call": "0x",
                                "chainId": self._int_to_hex(order_ctx['output']['chainId']),
                                "context": "0x",
                                "oracle": self._address_to_bytes32(UNISWAP_ROUTER_ADDRESS),
                                "recipient": self._address_to_bytes32(order_ctx['output']['asset']),
                                "settler": self._address_to_bytes32(UNISWAP_ROUTER_ADDRESS),
                                "token": self._address_to_bytes32(order_ctx['output']['asset']),
                            }
                        ],
                        "user": order_ctx['user']['address'],
                    }
                else:
                    standard_order = {
                        "expires": now + 3600,
                        "fillDeadline": now + 3600,
                        "inputOracle": "0x0000000000000000000000000000000000000000",
                        "inputs": [],
                        "nonce": self._generate_nonce(),
                        "originChainId": self._int_to_hex(CHAIN_ID),
                        "outputs": [],
                        "user": "0x0000000000000000000000000000000000000000",
                    }

                self.orders[order_id] = {
                    "id": order_id,
                    "quote_id": quote_id,
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                    "standard_order": standard_order,
                    "context": order_ctx,
                    "fill_tx_hash": None,
                }
                
                import threading
                def finalize_order():
                    # Base is faster, so simulate quicker finalization
                    time.sleep(random.uniform(2, 8))
                    if order_id in self.orders:
                        self.orders[order_id]['status'] = 'finalized'
                        self.orders[order_id]['updated_at'] = int(time.time())
                        self.orders[order_id]['fill_tx_hash'] = "0x" + secrets.token_hex(32)
                
                threading.Thread(target=finalize_order, daemon=True).start()
                
                if self.logger:
                    self.logger.info(
                        f"Order accepted (Base): {order_id} | quote: {quote_id} | "
                        f"status: pending"
                    )
                
                response = {
                    "status": "success",
                    "orderId": order_id,
                    "order": standard_order,
                    "message": "Order accepted"
                }
                
                return jsonify(response), 200
                
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error processing order submission: {e}")
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/orders/<order_id>', methods=['GET'])
        def get_order(order_id):
            """Get order status - OIF v1 GetOrderResponse format"""
            order_data = self.orders.get(order_id)
            if not order_data:
                return jsonify({"error": "Order not found"}), 404
            
            cached_quote = self.quote_cache.get(order_data.get("quote_id"))
            context = order_data.get("context") or (cached_quote.get("context") if cached_quote else None)
            details = cached_quote['details'] if cached_quote else None
            order_msg = cached_quote['message'] if cached_quote else None
            input_amount = details['availableInputs'][0] if details else None
            output_amount = details['requestedOutputs'][0] if details else None

            def sanitize_asset_amount(
                entry: Optional[Dict[str, Any]],
                fallback_asset: Optional[str],
            ) -> Dict[str, Any]:
                asset_value = (entry or {}).get("asset") or fallback_asset or ZERO_INTEROP_ADDRESS
                amount_value = (entry or {}).get("amount", "0")
                return {
                    "asset": asset_value,
                    "amount": amount_value,
                }

            fallback_input_asset = (
                context.get("input", {}).get("interop") if context else None
            )
            fallback_output_asset = (
                context.get("output", {}).get("interop") if context else None
            )

            sanitized_input_amount = sanitize_asset_amount(input_amount, fallback_input_asset)
            sanitized_output_amount = sanitize_asset_amount(
                output_amount, fallback_output_asset
            )

            settlement_data = {
                "estimatedGasUnits": context.get("gasEstimate") if context else None,
            }

            order_payload = {
                "id": order_data["id"],
                "status": order_data["status"],
                "createdAt": order_data["created_at"],
                "updatedAt": order_data["updated_at"],
                "quoteId": order_data.get("quote_id"),
                "inputAmount": sanitized_input_amount,
                "outputAmount": sanitized_output_amount,
                "settlement": {
                    "type": "escrow",
                    "data": settlement_data,
                },
                "fillTransaction": None,
            }

            if order_data.get("fill_tx_hash"):
                order_payload["fillTransaction"] = {
                    "txHash": order_data["fill_tx_hash"],
                    "chainId": context['output']['chainId'] if context else CHAIN_ID,
                    "executor": UNISWAP_ROUTER_ADDRESS,
                }

            return jsonify({"order": order_payload}), 200
        
        @self.app.route('/tokens', methods=['GET'])
        def get_tokens():
            """Return supported tokens - OIF v1 format"""
            tokens_to_return = self.supported_tokens[: self.advertised_token_limit]
            tokens_payload = [
                {
                    "address": token["address"],
                    "symbol": token["symbol"],
                    "decimals": token["decimals"],
                }
                for token in tokens_to_return
            ]
            return jsonify({
                "networks": {
                    str(CHAIN_ID): {
                        "chain_id": CHAIN_ID,
                        "name": "Base",
                        "input_settler": SETTLEMENT_CONTRACT_ADDRESS,
                        "output_settler": SETTLEMENT_CONTRACT_ADDRESS,
                        "tokens": tokens_payload,
                    }
                }
            }), 200
    
    def run(self, debug=False):
        """Start the solver server"""
        if self.logger:
            self.logger.info(f"Starting solver {self.solver_id} (Uniswap V3 on Base) on port {self.port}")
        
        if not debug:
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)
        
        self.app.run(host='0.0.0.0', port=self.port, debug=debug, use_reloader=False)


# Backward compatibility alias
Solver = SolverBase

