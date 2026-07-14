"""
Chain configuration for supported EVM networks.

Each chain entry includes the RPC environment variable name, a human-readable
name, and a block explorer URL. The ``get_web3`` helper constructs a Web3
instance for a given chain ID using the corresponding environment variable.
"""

import os
from typing import Any

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from minotaur_subnet.chains import registry as _registry


# ---------------------------------------------------------------------------
# Chain registry
# ---------------------------------------------------------------------------

# Projected from the canonical chain registry (minotaur_subnet.chains.registry)
# so the name / rpc_env / explorer / is_poa metadata lives in exactly one place.
# get_web3 below reads ``rpc_env`` from here exactly as before.
CHAIN_CONFIG: dict[int, dict[str, Any]] = {
    cid: {
        "name": s.name,
        "rpc_env": s.rpc_env,
        "explorer": s.explorer,
        "is_poa": s.is_poa,
    }
    for cid, s in _registry.CHAINS.items()
}

# Cache instantiated Web3 objects so we don't reconnect on every call.
_web3_cache: dict[int, Web3] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_chain_name(chain_id: int) -> str:
    """Return the human-readable name for *chain_id*, or ``"Unknown"``."""
    cfg = CHAIN_CONFIG.get(chain_id)
    if cfg is None:
        return "Unknown"
    return cfg["name"]


def get_explorer_url(chain_id: int) -> str:
    """Return the block-explorer base URL for *chain_id*."""
    cfg = CHAIN_CONFIG.get(chain_id)
    if cfg is None:
        raise ValueError(f"Unsupported chain_id: {chain_id}")
    return cfg["explorer"]


def get_tx_url(chain_id: int, tx_hash: str) -> str:
    """Return a full block-explorer link for a transaction hash."""
    base = get_explorer_url(chain_id)
    return f"{base}/tx/{tx_hash}"


def get_web3(chain_id: int, *, install_retry: bool = True) -> Web3:
    """
    Return a ``Web3`` instance connected to the RPC for *chain_id*.

    The RPC URL is read from the environment variable specified in
    ``CHAIN_CONFIG[chain_id]["rpc_env"]``.  Instances are cached so
    subsequent calls with the same *chain_id* return the same object.

    ``install_retry`` (default True) installs the read-scoped transient-retry
    middleware. Pass ``install_retry=False`` from a caller that ALREADY wraps its
    calls in its own retry (e.g. ``ContractManager._retry_rpc``) so reads aren't
    double-retried (attempts × the caller's attempts) and the provider isn't
    hammered during a throttle. The cache is keyed on the flag, so a retry and a
    no-retry client for the same chain coexist without clobbering each other.

    Raises ``ValueError`` if the chain is unsupported or the environment
    variable is not set.
    """
    cache_key = (chain_id, install_retry)
    if cache_key in _web3_cache:
        return _web3_cache[cache_key]

    cfg = CHAIN_CONFIG.get(chain_id)
    if cfg is None:
        raise ValueError(
            f"Unsupported chain_id: {chain_id}. "
            f"Supported chains: {list(CHAIN_CONFIG.keys())}"
        )

    rpc_url = os.environ.get(cfg["rpc_env"])
    if not rpc_url:
        raise ValueError(
            f"Environment variable {cfg['rpc_env']} is not set. "
            f"Set it to a valid RPC URL for {cfg['name']}."
        )

    w3 = Web3(Web3.HTTPProvider(rpc_url))

    # L2s / PoA chains may include extra header fields that the default
    # middleware rejects.  Inject the PoA middleware so those blocks can
    # be decoded.
    if cfg.get("is_poa"):
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    # Transient-read backoff: retry idempotent reads on 429/-32005/5xx/timeout
    # (writes/mutations pass through untouched). Injected outermost so it wraps
    # the PoA middleware + provider. LIGHT budget (see DEFAULT_WEB3_*) so it never
    # nests badly with a caller's own retry.
    if install_retry:
        from minotaur_subnet.blockchain.web3_retry import install_rpc_retry
        install_rpc_retry(w3)

    _web3_cache[cache_key] = w3
    return w3


def clear_web3_cache() -> None:
    """Clear the cached Web3 instances (useful for tests)."""
    _web3_cache.clear()
