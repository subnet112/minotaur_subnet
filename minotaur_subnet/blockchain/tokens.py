"""
Token address registry and ERC-20 helper utilities.

Provides well-known token addresses for each supported chain and a small
set of functions for common ERC-20 queries (balance, decimals, symbol).
"""

from __future__ import annotations

import asyncio
from typing import Any

from web3 import Web3
from web3.contract import Contract

from minotaur_subnet.blockchain.chains import get_web3


# ---------------------------------------------------------------------------
# Minimal ERC-20 ABI (read-only functions used in this module)
# ---------------------------------------------------------------------------

ERC20_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


# ---------------------------------------------------------------------------
# Well-known token addresses (checksummed)
# ---------------------------------------------------------------------------

TOKENS: dict[int, dict[str, str]] = {
    # Ethereum mainnet (chain 1)
    1: {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        "wTAO": "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44",
        "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
        "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
        "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
        "MKR": "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
        "SNX": "0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F",
        "COMP": "0xc00e94Cb662C3520282E6f5717214004A7f26888",
        "CRV": "0xD533a949740bb3306d119CC777fa900bA034cd52",
        "LDO": "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",
        "RPL": "0xD33526068D116cE69F19A9ee46F0bd304F21A51f",
        "APE": "0x4d224452801ACEd8B2F0aebE155379bb5D594381",
        "SHIB": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
        "PEPE": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
        "FXS": "0x3432B6A60D23Ca0dFCa7761B7ab56459D9C964D0",
        "FRAX": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
        "stETH": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
        "rETH": "0xae78736Cd615f374D3085123A210448E74Fc6393",
    },
    # Base (chain 8453)
    8453: {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDbC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    },
    # Arbitrum (chain 42161)
    42161: {
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
        "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "DAI": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    },
    # Optimism (chain 10)
    10: {
        "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "USDC.e": "0x7F5c764cBc14f9669B88837ca1490cCa17c31607",
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        "WBTC": "0x68f180fcCe6836688e9084f035309E29Bf0A2095",
        "DAI": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    },
    # Bittensor EVM (chain 964) — TAO is native gas, WTAO is the wrapped version.
    # Astrid Bridge (formerly TaoFi) deployed Uniswap V3 with TAO/USDC pool. USDC bridged via Hyperlane.
    964: {
        "WTAO": "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81",  # WETH9-style wrapper
        "TAO": "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81",   # alias → WTAO
        "USDC": "0xB833E8137FEDf80de7E908dc6fea43a029142F20",  # Hyperlane-bridged
    },
}

# Anvil (local mainnet fork) — shares mainnet addresses
TOKENS[31337] = dict(TOKENS[1])


# ---------------------------------------------------------------------------
# Wrapped native token per chain (for platform fee collection)
# ---------------------------------------------------------------------------

WRAPPED_NATIVE_TOKEN: dict[int, str] = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",       # WETH (Ethereum)
    8453: "0x4200000000000000000000000000000000000006",      # WETH (Base)
    42161: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",    # WETH (Arbitrum)
    10: "0x4200000000000000000000000000000000000006",        # WETH (Optimism)
    964: "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81",     # WTAO (BT EVM)
    31337: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH (Anvil fork)
}

WRAPPED_NATIVE_SYMBOL: dict[int, str] = {
    1: "ETH",
    8453: "ETH",
    42161: "ETH",
    10: "ETH",
    964: "TAO",
    31337: "ETH",
}

# Reverse lookup: address (lower) -> (chain_id, symbol)
_ADDRESS_TO_SYMBOL: dict[str, tuple[int, str]] = {}
for _cid, _toks in TOKENS.items():
    for _sym, _addr in _toks.items():
        _ADDRESS_TO_SYMBOL[_addr.lower()] = (_cid, _sym)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def resolve_token(
    token: str,
    fallback_chain_id: int = 1,
) -> tuple[str, int]:
    """Resolve a token identifier to ``(checksummed_address, chain_id)``.

    Accepted formats:

    - **CAIP-10**: ``eip155:1:0xA0b...`` → address + embedded chain_id
    - **Plain 0x** (42 chars): ``0xA0b...`` → address + *fallback_chain_id*
    - **Symbol**: ``USDC`` → registry lookup on *fallback_chain_id*
    - **Chain-qualified symbol**: ``USDC@8453`` → registry lookup on 8453

    Raises ``ValueError`` when the token cannot be resolved.
    """
    if not token:
        raise ValueError("token cannot be empty")

    token = token.strip()

    # Chain-qualified symbol:  USDC@8453
    if "@" in token and not token.startswith("0x"):
        parts = token.split("@", 1)
        symbol = parts[0]
        try:
            chain_id = int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid chain_id in token qualifier: {parts[1]!r}")
        return get_token_address(symbol, chain_id), chain_id

    # CAIP-10 or plain 0x or ERC-7930
    if token.startswith("0x") or token.startswith("eip155:"):
        from minotaur_subnet.shared.interop_address import parse_address
        ia = parse_address(token, default_chain_id=fallback_chain_id)
        chain_id = ia.chain_id if ia.chain_id is not None else fallback_chain_id
        return ia.address, chain_id

    # Bare symbol: USDC, WETH, etc.
    return get_token_address(token, fallback_chain_id), fallback_chain_id


def to_interop(address: str, chain_id: int) -> str:
    """Format a token address as CAIP-10: eip155:chain_id:0xaddress."""
    return f"eip155:{chain_id}:{address}"


# Native → wrapped symbol mapping. Smart contracts use the wrapped ERC-20
# version (e.g. WETH) but users often refer to the native coin (ETH).
NATIVE_TO_WRAPPED: dict[int, dict[str, str]] = {
    1: {"ETH": "WETH"},
    8453: {"ETH": "WETH"},
    42161: {"ETH": "WETH"},
    10: {"ETH": "WETH"},
    137: {"MATIC": "WMATIC"},
    56: {"BNB": "WBNB"},
    43114: {"AVAX": "WAVAX"},
}

NATIVE_TO_WRAPPED[31337] = NATIVE_TO_WRAPPED[1]
NATIVE_TO_WRAPPED[964] = {"TAO": "WTAO"}


def get_token_address(symbol: str, chain_id: int) -> str:
    """
    Return the checksummed token address for *symbol* on *chain_id*.

    Automatically resolves native symbols to their wrapped ERC-20
    equivalent (e.g. ETH → WETH, MATIC → WMATIC).

    Raises ``ValueError`` if the token/chain combination is unknown.
    """
    # TAO/wTAO alias: resolve to the chain's preferred naming
    if symbol.upper() in ("TAO", "WTAO"):
        chain_tokens = TOKENS.get(chain_id, {})
        if "WTAO" in chain_tokens:
            symbol = "WTAO"
        elif "wTAO" in chain_tokens:
            symbol = "wTAO"

    chain_tokens = TOKENS.get(chain_id)
    if chain_tokens is None:
        raise ValueError(f"No tokens registered for chain_id {chain_id}")
    address = chain_tokens.get(symbol)
    if address is None:
        # Try native → wrapped resolution (ETH → WETH, etc.)
        wrapped = NATIVE_TO_WRAPPED.get(chain_id, {}).get(symbol)
        if wrapped:
            address = chain_tokens.get(wrapped)
    if address is None:
        raise ValueError(
            f"Token {symbol!r} not found on chain {chain_id}. "
            f"Known tokens: {list(chain_tokens.keys())}"
        )
    return address


def get_token_symbol(address: str, chain_id: int | None = None) -> str | None:
    """
    Return the symbol for a well-known *address*.

    If *chain_id* is given, only return a match on that chain.
    Returns ``None`` when the address is not in the registry.
    """
    key = address.lower()
    entry = _ADDRESS_TO_SYMBOL.get(key)
    if entry is None:
        return None
    cid, sym = entry
    if chain_id is not None and cid != chain_id:
        return None
    return sym


# ---------------------------------------------------------------------------
# WAL-2: Token approval helpers (ERC-20 approve + ERC-2612 permit)
# ---------------------------------------------------------------------------

# ERC-2612 Permit EIP-712 typehash
_PERMIT_TYPEHASH_STR = b"Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"

# ERC-2612 domain typehash
_EIP712_DOMAIN_TYPEHASH_STR = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)


def build_approve_calldata(spender: str, amount: int) -> bytes:
    """Build ERC-20 ``approve(spender, amount)`` calldata (WAL-2).

    Returns raw bytes ready to be used as the ``data`` field of a transaction
    to the token contract.

    Args:
        spender: The address being approved to spend tokens (e.g., AppIntentBase contract).
        amount: The amount to approve in the token's smallest unit (wei-equivalent).
               Use ``2**256 - 1`` for unlimited approval.

    Returns:
        ABI-encoded calldata bytes.
    """
    from eth_abi import encode as abi_encode
    from eth_hash.auto import keccak

    selector = keccak(b"approve(address,uint256)")[:4]
    params = abi_encode(
        ["address", "uint256"],
        [Web3.to_checksum_address(spender), amount],
    )
    return selector + params


def build_permit_signature(
    private_key: str,
    token_address: str,
    token_name: str,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
    chain_id: int,
    token_version: str = "1",
) -> tuple[int, bytes, bytes]:
    """Build an ERC-2612 permit EIP-712 signature (WAL-2).

    Generates the EIP-712 typed-data signature that can be passed to
    ``token.permit(owner, spender, value, deadline, v, r, s)`` for
    gas-free token approval.

    Args:
        private_key: Hex-encoded private key of the token owner.
        token_address: The ERC-2612 token contract address.
        token_name: The token's ``name()`` (used in EIP-712 domain).
        owner: The token owner's address.
        spender: The address being approved.
        value: Amount to approve.
        nonce: The owner's current permit nonce (from ``token.nonces(owner)``).
        deadline: Unix timestamp after which the permit expires.
        chain_id: Target chain ID.
        token_version: Token EIP-712 version (usually "1").

    Returns:
        Tuple of (v, r, s) where v is an int and r, s are 32-byte values.
    """
    from eth_abi import encode as abi_encode
    from eth_account import Account
    from eth_hash.auto import keccak

    # Domain separator (token's own domain)
    domain_typehash = keccak(_EIP712_DOMAIN_TYPEHASH_STR)
    domain_sep = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [
            domain_typehash,
            keccak(token_name.encode()),
            keccak(token_version.encode()),
            chain_id,
            Web3.to_checksum_address(token_address),
        ],
    ))

    # Permit struct hash
    permit_typehash = keccak(_PERMIT_TYPEHASH_STR)
    struct_hash = keccak(abi_encode(
        ["bytes32", "address", "address", "uint256", "uint256", "uint256"],
        [
            permit_typehash,
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
            value,
            nonce,
            deadline,
        ],
    ))

    # EIP-712 digest
    digest = keccak(b"\x19\x01" + domain_sep + struct_hash)

    # Sign
    signed = Account.unsafe_sign_hash(digest, private_key=private_key)
    v = signed.v
    r = signed.r.to_bytes(32, "big")
    s = signed.s.to_bytes(32, "big")
    return v, r, s


def _get_erc20_contract(token_address: str, chain_id: int) -> tuple[Web3, Contract]:
    """Internal: build a Web3 contract object for an ERC-20 token."""
    w3 = get_web3(chain_id)
    checksum = Web3.to_checksum_address(token_address)
    contract = w3.eth.contract(address=checksum, abi=ERC20_ABI)
    return w3, contract


async def get_erc20_balance(
    token_address: str,
    owner: str,
    chain_id: int,
) -> str:
    """
    Return the ERC-20 balance of *owner* for the token at *token_address*
    on *chain_id*, as a decimal string in the token's smallest unit (wei
    equivalent).

    The RPC call is executed in a thread so this function can be awaited
    in an async context without blocking the event loop.
    """
    _, contract = _get_erc20_contract(token_address, chain_id)
    owner_checksum = Web3.to_checksum_address(owner)

    loop = asyncio.get_running_loop()
    balance: int = await loop.run_in_executor(
        None,
        contract.functions.balanceOf(owner_checksum).call,
    )
    return str(balance)


async def get_erc20_decimals(token_address: str, chain_id: int) -> int:
    """Return the ``decimals()`` value for the token."""
    _, contract = _get_erc20_contract(token_address, chain_id)
    loop = asyncio.get_running_loop()
    decimals: int = await loop.run_in_executor(
        None,
        contract.functions.decimals().call,
    )
    return decimals


async def get_erc20_symbol(token_address: str, chain_id: int) -> str:
    """Return the ``symbol()`` value for the token."""
    _, contract = _get_erc20_contract(token_address, chain_id)
    loop = asyncio.get_running_loop()
    symbol: str = await loop.run_in_executor(
        None,
        contract.functions.symbol().call,
    )
    return symbol


async def get_native_balance(owner: str, chain_id: int) -> str:
    """
    Return the native (ETH) balance of *owner* on *chain_id*,
    as a decimal string in wei.
    """
    w3 = get_web3(chain_id)
    owner_checksum = Web3.to_checksum_address(owner)

    loop = asyncio.get_running_loop()
    balance: int = await loop.run_in_executor(
        None,
        w3.eth.get_balance,
        owner_checksum,
    )
    return str(balance)
