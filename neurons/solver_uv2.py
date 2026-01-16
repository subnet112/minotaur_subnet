"""OIF v1 compatible solver with Uniswap V2 price simulation.

This solver implements the OIF v1 API specification and provides quotes
for token swaps using Uniswap V2 pools. It can run in standalone mode or
be managed by a miner.
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


CHAIN_ID = 1

# USDT token address (requires two-step approval: reset to 0, then set amount)
USDT_ADDRESS = "0xdac17f958d2ee523a2206206994597c13d831ec7"

# Uniswap V2 contract addresses on Ethereum mainnet
UNISWAP_V2_ROUTER_ADDRESS = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_V2_ROUTER_INTEROP = create_interop_address(CHAIN_ID, UNISWAP_V2_ROUTER_ADDRESS)
ZERO_INTEROP_ADDRESS = create_interop_address(
    CHAIN_ID, "0x0000000000000000000000000000000000000000"
)
UNISWAP_V2_FACTORY_ADDRESS = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"

# WETH address for V2 router path routing
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

UNISWAP_TOKEN_LIST_URL = os.environ.get(
    "UNISWAP_TOKEN_LIST_URL", "https://extendedtokens.uniswap.org"
)
# Fallback to more comprehensive token list if extended list is insufficient
COMPREHENSIVE_TOKEN_LIST_URL = os.environ.get(
    "COMPREHENSIVE_TOKEN_LIST_URL", "https://tokens.uniswap.org"
)
# CoinGecko comprehensive token list for additional token discovery
COINGECKO_TOKEN_LIST_URL = os.environ.get(
    "COINGECKO_TOKEN_LIST_URL", "https://tokens.coingecko.com/ethereum/all.json"
)
DEFAULT_TOKEN_ADVERTISE_LIMIT = int(os.environ.get("MOCK_SOLVER_TOKEN_LIMIT", "10000"))
SETTLEMENT_CONTRACT_ADDRESS = os.environ.get(
    "SETTLEMENT_CONTRACT_ADDRESS",
    "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3",
)

ERC20_ABI = [
    {"name": "decimals", "outputs": [{"type": "uint8"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

# Uniswap V2 Factory ABI - getPair returns the pair address for two tokens
UNISWAP_V2_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
        ],
        "name": "getPair",
        "outputs": [{"internalType": "address", "name": "pair", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Uniswap V2 Router ABI - for quoting and swapping
UNISWAP_V2_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

BASE_TOKEN_METADATA: Dict[str, Dict[str, object]] = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {"symbol": "WETH", "decimals": 18},
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {"symbol": "USDC", "decimals": 6},
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {"symbol": "USDT", "decimals": 6},
    "0x6b175474e89094c44da98b954eedeac495271d0f": {"symbol": "DAI", "decimals": 18},
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {"symbol": "WBTC", "decimals": 8},
    "0x0000000000000000000000000000000000000000": {"symbol": "ETH", "decimals": 18},
    # Popular tokenized stocks and additional tokens not in Uniswap extended list
    "0x2d1f7226bd1f780af6b9a49dcc0ae00e8df4bdee": {"symbol": "NVDAon", "decimals": 18},  # NVIDIA tokenized stock
    "0x3632DEa96A953C11dac2f00b4A05a32CD1063fAE": {"symbol": "CRCLon", "decimals": 18},
}

getcontext().prec = 64


class SolverV2:
    """OIF v1 compatible solver with Uniswap V2 integration.
    
    Supports two execution modes:
    - simulation (default): Simulates order execution for testing
    - live: Executes orders on-chain via the Settlement contract
    """
    
    def __init__(
        self,
        solver_id: str,
        port: int,
        latency_ms: int = 100,
        quality: float = 1.0,
        logger=None,
        execution_mode: str = "simulation",
        executor_private_key: Optional[str] = None,
        dry_run: bool = False
    ):
        """
        Initialize the solver.
        
        Args:
            solver_id: Unique identifier for this solver
            port: Port to run the HTTP server on
            latency_ms: Artificial latency to add to responses (for testing)
            quality: Quality factor for quotes (1.0 = best)
            logger: Logger instance
            execution_mode: "simulation" or "live"
            executor_private_key: Private key for executing transactions (required for live mode)
            dry_run: In live mode, simulate but don't submit transactions
        """
        self.solver_id = solver_id
        self.port = port
        self.latency_ms = latency_ms
        self.quality = quality
        self.logger = logger
        self.execution_mode = execution_mode
        self.executor_private_key = executor_private_key
        self.dry_run = dry_run
        self.request_count = 0
        self.advertised_token_limit = DEFAULT_TOKEN_ADVERTISE_LIMIT
        self.token_metadata: Dict[str, Dict[str, Any]] = dict(BASE_TOKEN_METADATA)
        self._load_token_list()
        # Initialize web3 BEFORE token discovery (needed for pair discovery)
        self.web3 = self._init_web3()
        if self.web3 and not self.web3.is_connected():
            self.web3 = None
        if os.environ.get("ENABLE_POOL_TOKEN_DISCOVERY", "true").lower() in ("1", "true", "yes"):
            self._discover_tokens_from_pairs()  # Discover additional tokens from Uniswap V2 pairs
        self.factory_contract = (
            self.web3.eth.contract(
                address=self.web3.to_checksum_address(UNISWAP_V2_FACTORY_ADDRESS),
                abi=UNISWAP_V2_FACTORY_ABI,
            )
            if self.web3
            else None
        )
        self.router_contract = (
            self.web3.eth.contract(
                address=self.web3.to_checksum_address(UNISWAP_V2_ROUTER_ADDRESS),
                abi=UNISWAP_V2_ROUTER_ABI,
            )
            if self.web3
            else None
        )
        self.pair_cache: Dict[Tuple[str, str], Optional[str]] = {}
        self.supported_tokens: List[Dict[str, Any]] = self._build_supported_tokens()
        self.app = Flask(f"solver-v2-{solver_id}")
        self.setup_routes()
        self.orders = {}
        self.quote_cache: Dict[str, dict] = {}
        
        # Initialize on-chain executor for live mode
        self.onchain_executor = None
        if self.execution_mode == "live" and self.web3:
            try:
                from neurons.onchain_executor import OnchainExecutor
                self.onchain_executor = OnchainExecutor(
                    web3=self.web3,
                    settlement_address=SETTLEMENT_CONTRACT_ADDRESS,
                    chain_id=CHAIN_ID,
                    private_key=executor_private_key,
                    logger=self.logger,
                    dry_run=dry_run
                )
                if self.logger:
                    mode_str = "DRY RUN" if dry_run else "LIVE"
                    self.logger.info(f"🔗 On-chain executor initialized ({mode_str} mode)")
            except Exception as e:
                if self.logger:
                    self.logger.error(f"❌ Failed to initialize on-chain executor: {e}")
                self.execution_mode = "simulation"
        
        if self.logger:
            self.logger.info(f"Solver execution mode: {self.execution_mode}")
    
    def _init_web3(self) -> Optional["Web3"]:
        if not WEB3_AVAILABLE:
            return None

        rpc_url = os.getenv("ETHEREUM_RPC_URL")
        if not rpc_url:
            alchemy_key = os.getenv("ALCHEMY_API_KEY")
            if alchemy_key:
                rpc_url = f"https://eth-mainnet.g.alchemy.com/v2/{alchemy_key}"
            else:
                rpc_url = "https://eth.llamarpc.com"

        try:
            provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10})
            web3 = Web3(provider)
            if web3.is_connected():
                if self.logger:
                    self.logger.info(f"Connected to Ethereum RPC for token discovery")
                return web3
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to initialize Web3 provider ({rpc_url}): {exc}")
        return None

    def _load_token_list(self):
        """Load token list from Uniswap (optional, non-blocking)."""
        if os.environ.get("DISABLE_UNISWAP_TOKEN_LIST", "").lower() in ("1", "true", "yes"):
            return

        # Try multiple token sources in order of preference
        token_sources = [
            ("extended tokens", UNISWAP_TOKEN_LIST_URL),
            ("comprehensive tokens", COMPREHENSIVE_TOKEN_LIST_URL),
            ("CoinGecko tokens", COINGECKO_TOKEN_LIST_URL),
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

                # Handle different token list formats
                if source_name == "CoinGecko tokens":
                    # CoinGecko format is already filtered to Ethereum
                    pass
                else:
                    # Uniswap format has chainId field
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
                    self.logger.info(f"Loaded {added} additional tokens from Uniswap {source_name} list")
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Failed to load tokens from {source_name} list: {e}")
                continue

        if total_added and self.logger:
            self.logger.info(f"Total tokens loaded from token lists: {total_added}")

    def _discover_tokens_from_pairs(self):
        """Discover additional tokens by scanning recent Uniswap V2 pair creations."""
        if not WEB3_AVAILABLE or os.environ.get("DISABLE_TOKEN_DISCOVERY", "").lower() in ("1", "true", "yes"):
            return

        try:
            # Initialize Web3 for token discovery
            web3 = self._init_web3()
            if not web3:
                return

            # We'll scan recent blocks for PairCreated events from the Uniswap V2 factory
            factory_address = web3.to_checksum_address(UNISWAP_V2_FACTORY_ADDRESS)

            # Get current block and scan last N blocks (adjustable via env var)
            current_block = web3.eth.block_number
            blocks_to_scan = int(os.environ.get("POOL_DISCOVERY_BLOCKS", "10000"))  # Default: ~2-3 days
            from_block = max(0, current_block - blocks_to_scan)

            if self.logger:
                self.logger.info(f"Scanning Uniswap V2 pairs from block {from_block} to {current_block}")

            # PairCreated event signature: PairCreated(address indexed token0, address indexed token1, address pair, uint)
            pair_created_topic = web3.keccak(text="PairCreated(address,address,address,uint256)").hex()

            # Query for PairCreated events
            logs = web3.eth.get_logs({
                "address": factory_address,
                "topics": [pair_created_topic],
                "fromBlock": from_block,
                "toBlock": current_block
            })

            discovered_tokens = set()
            for log in logs:
                try:
                    # Decode the PairCreated event
                    # Indexed parameters appear in topics: topics[1] = token0, topics[2] = token1
                    topics = log["topics"]

                    if len(topics) >= 3:  # Make sure we have indexed parameters
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
                        self.logger.debug(f"Failed to decode pair creation log: {e}")

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
                self.logger.info(f"Discovered {added} additional tokens from Uniswap V2 pairs")

        except Exception as e:
            if self.logger:
                self.logger.warning(f"Token discovery from pairs failed: {e}")

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

    def _find_pair_address(self, token_in: str, token_out: str) -> Optional[str]:
        """Find the Uniswap V2 pair address for two tokens."""
        # Normalize addresses to lowercase for cache key
        key = (token_in.lower(), token_out.lower())
        reverse_key = (token_out.lower(), token_in.lower())
        
        # Check cache first
        if key in self.pair_cache:
            return self.pair_cache[key]
        if reverse_key in self.pair_cache:
            return self.pair_cache[reverse_key]

        if not self.factory_contract:
            self.pair_cache[key] = None
            return None

        try:
            pair = self.factory_contract.functions.getPair(
                self.web3.to_checksum_address(token_in),
                self.web3.to_checksum_address(token_out),
            ).call()
            if pair and int(pair, 16) != 0:
                self.pair_cache[key] = pair
                return pair
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to lookup pair for {token_in}/{token_out}: {exc}")

        self.pair_cache[key] = None
        return None

    def _quote_via_uniswap_v2(self, token_in: str, token_out: str, amount_in_wei: int) -> Optional[Tuple[int, List[str]]]:
        """
        Get quote from Uniswap V2 router using getAmountsOut.
        Returns (amount_out, path) or None if no route found.
        
        Uniswap V2 uses path-based routing. We try:
        1. Direct path: [tokenIn, tokenOut]
        2. Via WETH: [tokenIn, WETH, tokenOut] if direct fails
        """
        if not self.router_contract or amount_in_wei == 0:
            return None
        
        # Try direct path first
        direct_path = [
            self.web3.to_checksum_address(token_in),
            self.web3.to_checksum_address(token_out),
        ]
        
        try:
            amounts = self.router_contract.functions.getAmountsOut(
                amount_in_wei,
                direct_path,
            ).call()
            if amounts and len(amounts) >= 2 and amounts[-1] > 0:
                return int(amounts[-1]), direct_path
        except ContractLogicError:
            pass  # No direct pair, try via WETH
        except Exception as exc:
            if self.logger:
                self.logger.debug(f"Direct path quote failed for {token_in}/{token_out}: {exc}")
        
        # Try routing via WETH if direct path failed and tokens aren't already WETH
        weth_checksum = self.web3.to_checksum_address(WETH_ADDRESS)
        token_in_checksum = self.web3.to_checksum_address(token_in)
        token_out_checksum = self.web3.to_checksum_address(token_out)
        
        if token_in_checksum.lower() != weth_checksum.lower() and token_out_checksum.lower() != weth_checksum.lower():
            weth_path = [token_in_checksum, weth_checksum, token_out_checksum]
            
            try:
                amounts = self.router_contract.functions.getAmountsOut(
                    amount_in_wei,
                    weth_path,
                ).call()
                if amounts and len(amounts) >= 3 and amounts[-1] > 0:
                    return int(amounts[-1]), weth_path
            except ContractLogicError:
                pass
            except Exception as exc:
                if self.logger:
                    self.logger.debug(f"WETH path quote failed for {token_in}/{token_out}: {exc}")
        
        return None

    def _estimate_gas_units(self, path_length: int) -> int:
        """Estimate gas units for a V2 swap based on path length."""
        # Base gas for V2 swap is lower than V3
        # Single hop: ~100k gas, each additional hop adds ~60k
        base = 100_000
        extra_hops = max(0, path_length - 2)
        gas = base + (extra_hops * 60_000)
        jitter = random.randint(-5_000, 5_000)
        return max(80_000, gas + jitter)

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

    def _encode_swap_exact_tokens_for_tokens_calldata(
        self,
        amount_in: int,
        amount_out_min: int,
        path: List[str],
        recipient: str,
        deadline: int,
    ) -> str:
        """Encode swapExactTokensForTokens call for Uniswap V2 Router."""
        encoded_params = encode(
            ['uint256', 'uint256', 'address[]', 'address', 'uint256'],
            [amount_in, amount_out_min, path, recipient, deadline],
        )

        selector = keccak(
            text='swapExactTokensForTokens(uint256,uint256,address[],address,uint256)'
        )[:4]

        return '0x' + (selector + encoded_params).hex()

    def _build_route_details(
        self,
        token_in_meta: Dict[str, object],
        token_out_meta: Dict[str, object],
        input_amount_wei: int,
        expected_output_wei: int,
        min_output_wei: int,
        path: List[str],
        pair_address: Optional[str],
    ) -> Dict[str, object]:
        """Build route details for V2 swap."""
        # Build path info with intermediate tokens
        path_info = []
        for i in range(len(path) - 1):
            hop_in = path[i].lower()
            hop_out = path[i + 1].lower()
            hop_in_meta = self.token_metadata.get(hop_in, {"symbol": hop_in[-4:].upper(), "decimals": 18})
            hop_out_meta = self.token_metadata.get(hop_out, {"symbol": hop_out[-4:].upper(), "decimals": 18})
            
            path_info.append({
                "tokenIn": hop_in,
                "tokenOut": hop_out,
                "tokenInSymbol": hop_in_meta.get("symbol", hop_in[-4:].upper()),
                "tokenOutSymbol": hop_out_meta.get("symbol", hop_out[-4:].upper()),
                "tokenInDecimals": hop_in_meta.get("decimals", 18),
                "tokenOutDecimals": hop_out_meta.get("decimals", 18),
                "fee": 3000,  # V2 has fixed 0.3% fee (3000 basis points)
            })
        
        # Add amounts to first and last hops
        if path_info:
            path_info[0]["amountInWei"] = str(input_amount_wei)
            path_info[-1]["expectedAmountOutWei"] = str(expected_output_wei)
            path_info[-1]["minAmountOutWei"] = str(min_output_wei)
        
        return {
            "engine": "uniswap-v2",
            "pairAddress": pair_address,
            "fee": 3000,  # Fixed 0.3% fee for V2
            "path": path_info,
            "routePath": path,  # Full address path for swap
            "estimatedGasUnits": self._estimate_gas_units(len(path)),
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
        path: List[str],
        pair_address: Optional[str],
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
            path,
            pair_address,
        )

        deadline = int(time.time()) + 600

        receiver_interop = request["requestedOutputs"][0].get("receiver")
        _, receiver_address = (
            self._interop_to_components(receiver_interop)
            if receiver_interop
            else (user_chain, user_address)
        )

        settlement_recipient = SETTLEMENT_CONTRACT_ADDRESS

        calldata = self._encode_swap_exact_tokens_for_tokens_calldata(
            input_amount_wei,
            min_output_wei,
            path,
            settlement_recipient,
            deadline,
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
            UNISWAP_V2_ROUTER_ADDRESS,
            0,  # Reset to zero
        )
        pre_interactions.append({
            "target": token_address,
            "value": "0",
            "callData": reset_approval_calldata,
        })
        
        # Step 2: Set approval to desired amount
        approve_uniswap_calldata = self._encode_erc20_approve_calldata(
            UNISWAP_V2_ROUTER_ADDRESS,
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
                "target": UNISWAP_V2_ROUTER_ADDRESS,
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
            "routePath": path,  # Store the path for order execution
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
            return jsonify({"status": "healthy", "solver_id": self.solver_id, "engine": "uniswap-v2"}), 200
        
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

                input_amount_wei = int(input_asset['amount'])

                self.request_count += 1
                quote_id = f"{self.solver_id}-v2-q-{int(time.time() * 1000)}-{self.request_count}"
                
                # Fetch block number BEFORE computing quote to ensure consistency
                # The execution plan will use this same block number
                quote_block_number = None
                if self.web3:
                    try:
                        quote_block_number = str(self.web3.eth.block_number)
                    except Exception:
                        pass
                
                # Get quote from Uniswap V2 Router
                quote_result = self._quote_via_uniswap_v2(
                    token_in_meta["address"],
                    token_out_meta["address"],
                    input_amount_wei,
                )
                
                if not quote_result:
                    if self.logger:
                        self.logger.info(f"No liquidity found for {token_in_meta['symbol']}/{token_out_meta['symbol']} on Uniswap V2")
                    # Return empty quotes array to indicate no quotes available for this pair
                    # This is the proper OIF response for unsupported pairs
                    return jsonify({"quotes": []}), 200
                
                onchain_output, path = quote_result
                
                # Find pair address for the first hop (for route details)
                pair_address = self._find_pair_address(
                    path[0],
                    path[1],
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
                    path,
                    pair_address,
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
                    "eta": random.randint(20, 60),
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
                    path_symbols = " -> ".join([
                        self.token_metadata.get(p.lower(), {}).get("symbol", p[-4:].upper())
                        for p in path
                    ])
                    self.logger.info(
                        f"Quote generated: {quote_id} | "
                        f"{token_in_meta['symbol']} -> {token_out_meta['symbol']} | "
                        f"{input_amount} -> {expected_output_amount} | "
                        f"path: {path_symbols}"
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
            """Handle order submissions - OIF v1 format
            
            In simulation mode: Simulates order finalization
            In live mode: Executes order on-chain via Settlement contract
            """
            try:
                req = request.json
                order_id = f"{self.solver_id}-v2-order-{int(time.time() * 1000)}"
                quote_id = req.get('quoteId')
                signature = req.get('signature', '')
                cached_quote = self.quote_cache.get(quote_id)

                now = int(time.time())

                # Validate quote exists
                if not cached_quote:
                    if self.logger:
                        self.logger.warning(f"Order submission with unknown quote: {quote_id}")
                    return jsonify({
                        "status": "error",
                        "error": f"Unknown quoteId: {quote_id}"
                    }), 400

                order_ctx = cached_quote.get('context')
                order_msg = cached_quote.get('message')

                # Check if quote has expired
                quote_deadline = order_msg.get('deadline', 0) if order_msg else 0
                if quote_deadline and now > quote_deadline:
                    return jsonify({
                        "status": "error",
                        "error": "Quote has expired"
                    }), 400

                # Build quote data for execution
                quote_data = {
                    "quoteId": quote_id,
                    "details": cached_quote.get('details', {}),
                    "settlement": order_ctx.get('settlement') if order_ctx else {}
                }

                # Live execution mode
                if self.execution_mode == "live" and self.onchain_executor:
                    if not signature:
                        return jsonify({
                            "status": "error",
                            "error": "Signature required for live execution"
                        }), 400
                    
                    if self.logger:
                        self.logger.info(f"🔗 Executing order on-chain: {order_id}")
                    
                    executed_order = self.onchain_executor.execute_order_async(
                        order_id=order_id,
                        quote_data=quote_data,
                        signature=signature,
                        skip_verification=False
                    )
                    
                    self.orders[order_id] = {
                        "id": order_id,
                        "quote_id": quote_id,
                        "status": executed_order.status.value,
                        "created_at": executed_order.created_at,
                        "updated_at": executed_order.updated_at,
                        "context": order_ctx,
                        "fill_tx_hash": executed_order.tx_hash,
                        "execution_mode": "live"
                    }
                    
                    response = {
                        "status": "received",
                        "orderId": order_id,
                        "message": "Order received for on-chain execution"
                    }
                    
                    return jsonify(response), 200

                # Simulation mode (default)
                input_amount_wei = int(order_ctx['input']['amountWei']) if order_ctx else 0
                min_output_wei = int(order_ctx['output']['minimumAmountWei']) if order_ctx else 0
                expected_output_wei = int(order_ctx['output']['estimatedAmountWei']) if order_ctx else 0

                standard_order = {
                    "expires": order_msg['deadline'] if order_msg else now + 3600,
                    "fillDeadline": (order_msg['deadline'] + 300) if order_msg else now + 3900,
                    "inputOracle": "0x0000000000000000000000000000000000000000",
                    "inputs": [
                        [
                            self._int_to_hex(input_amount_wei),
                            self._int_to_hex(min_output_wei),
                        ]
                    ] if order_ctx else [],
                    "nonce": order_msg['nonce'] if order_msg else self._generate_nonce(),
                    "originChainId": self._int_to_hex(order_ctx['originChainId']) if order_ctx else self._int_to_hex(CHAIN_ID),
                    "outputs": [
                        {
                            "amount": self._int_to_hex(expected_output_wei),
                            "call": "0x",
                            "chainId": self._int_to_hex(order_ctx['output']['chainId']),
                            "context": "0x",
                            "oracle": self._address_to_bytes32(UNISWAP_V2_ROUTER_ADDRESS),
                            "recipient": self._address_to_bytes32(order_ctx['output']['asset']),
                            "settler": self._address_to_bytes32(UNISWAP_V2_ROUTER_ADDRESS),
                            "token": self._address_to_bytes32(order_ctx['output']['asset']),
                        }
                    ] if order_ctx else [],
                    "user": order_ctx['user']['address'] if order_ctx else "0x0000000000000000000000000000000000000000",
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
                    "execution_mode": "simulation"
                }
                
                import threading
                def finalize_order():
                    time.sleep(random.uniform(5, 15))
                    if order_id in self.orders:
                        self.orders[order_id]['status'] = 'finalized'
                        self.orders[order_id]['updated_at'] = int(time.time())
                        self.orders[order_id]['fill_tx_hash'] = "0x" + secrets.token_hex(32)
                        self.orders[order_id]['block_number'] = self.web3.eth.block_number if self.web3 else 0
                        self.orders[order_id]['gas_used'] = random.randint(100000, 200000)
                
                threading.Thread(target=finalize_order, daemon=True).start()
                
                if self.logger:
                    self.logger.info(
                        f"Order accepted (simulation): {order_id} | quote: {quote_id} | "
                        f"status: pending"
                    )
                
                response = {
                    "status": "success",
                    "orderId": order_id,
                    "order": standard_order,
                    "message": "Order accepted (simulation mode)"
                }
                
                return jsonify(response), 200
                
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error processing order submission: {e}")
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/orders/<order_id>', methods=['GET'])
        def get_order(order_id):
            """Get order status - OIF v1 GetOrderResponse format"""
            # First check if it's a live execution in the onchain_executor
            if self.onchain_executor:
                executed_order = self.onchain_executor.get_order(order_id)
                if executed_order:
                    self.orders[order_id] = {
                        "id": order_id,
                        "quote_id": executed_order.quote_id,
                        "status": executed_order.status.value,
                        "created_at": executed_order.created_at,
                        "updated_at": executed_order.updated_at,
                        "fill_tx_hash": executed_order.tx_hash,
                        "block_number": executed_order.block_number,
                        "gas_used": executed_order.gas_used,
                        "error_message": executed_order.error_message,
                        "execution_mode": "live"
                    }
                    return jsonify(self.onchain_executor.to_order_response(executed_order)), 200
            
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

            # Format timestamps as ISO strings
            created_at = order_data["created_at"]
            updated_at = order_data["updated_at"]
            created_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
            updated_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated_at))

            order_payload = {
                "id": order_data["id"],
                "status": order_data["status"],
                "createdAt": created_at_iso,
                "updatedAt": updated_at_iso,
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
                    "blockNumber": order_data.get("block_number"),
                    "gasUsed": order_data.get("gas_used"),
                    "executor": UNISWAP_V2_ROUTER_ADDRESS,
                }
            
            if order_data.get("error_message"):
                order_payload["error"] = order_data["error_message"]

            return jsonify({
                "orderId": order_data["id"],
                "status": order_data["status"],
                "order": order_payload
            }), 200
        
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
                    "1": {
                        "chain_id": 1,
                        "input_settler": SETTLEMENT_CONTRACT_ADDRESS,
                        "output_settler": SETTLEMENT_CONTRACT_ADDRESS,
                        "tokens": tokens_payload,
                    }
                }
            }), 200
    
    def run(self, debug=False):
        """Start the solver server"""
        if self.logger:
            self.logger.info(f"Starting solver {self.solver_id} (Uniswap V2) on port {self.port}")
        
        if not debug:
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)
        
        self.app.run(host='0.0.0.0', port=self.port, debug=debug, use_reloader=False)


# Backward compatibility: Also export as Solver for consistency
Solver = SolverV2



