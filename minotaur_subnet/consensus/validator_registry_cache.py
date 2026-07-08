"""Cache for on-chain `ValidatorRegistry.isValidator()` lookups.

The leader's in-memory validator set is seeded at boot (from env or
metagraph) and never refreshed in the current design. If the on-chain
ValidatorRegistry changes — e.g., an operator rotates a compromised key
— the leader keeps accepting approvals from the old set.

This module adds a lazy, TTL-bounded cross-check: when the leader
verifies an approval, it can also ask the on-chain registry whether the
signer is currently registered. A 5-second cache keeps the RPC overhead
negligible (at most one call per 5s per signer per chain).

The cross-check is *opt-in via env*: set ``CONSENSUS_ENFORCE_ONCHAIN_REGISTRY=1``
to turn it on. When unset, ``is_on_chain_validator`` returns ``True``
unconditionally so callers can hard-wire the lookup without breaking
single-chain setups that have no registry deployed.
"""

from __future__ import annotations

import logging
import os
import time
from threading import RLock
from typing import Any

from minotaur_subnet.chains import registry

logger = logging.getLogger(__name__)

# Per (chain_id, signer.lower()) -> (expires_at, is_validator)
_CACHE: dict[tuple[int, str], tuple[float, bool]] = {}
_CACHE_LOCK = RLock()
_CACHE_TTL_SECONDS = 5.0


def _chain_registry_env(chain_id: int) -> str:
    return registry.validator_registry_address(chain_id)


def _chain_rpc_env(chain_id: int) -> str:
    # Live-chain reads must hit the LIVE chain, never the sim fork. The
    # upstream-preferred ladder lives once in the chain registry.
    return registry.live_rpc(chain_id)


def enforce_enabled() -> bool:
    """Check if on-chain enforcement is explicitly enabled."""
    return os.environ.get(
        "CONSENSUS_ENFORCE_ONCHAIN_REGISTRY", "0",
    ).strip().lower() in ("1", "true", "yes", "on")


def is_on_chain_validator(signer: str, chain_id: int) -> bool:
    """Return True if *signer* is a current validator on the registry for *chain_id*.

    Semantics when enforcement is off (default): always True — callers can
    use this unconditionally and it becomes a no-op without a config flag.

    When enforcement is on:
      - Missing registry address or RPC → True (can't enforce what we can't
        reach; failing closed on a transient RPC outage would deadlock the
        whole consensus loop — we log loudly instead).
      - Cache hit within 5s → return cached.
      - Cache miss → web3 call to registry.isValidator(signer); cache and return.
    """
    if not enforce_enabled():
        return True

    key = (int(chain_id), signer.lower())
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]

    registry = _chain_registry_env(int(chain_id))
    rpc = _chain_rpc_env(int(chain_id))
    if not registry or not rpc:
        logger.warning(
            "onchain-registry check skipped for chain %d (registry=%s rpc=%s); "
            "failing open",
            chain_id,
            "set" if registry else "unset",
            "set" if rpc else "unset",
        )
        # Cache the permissive result briefly so we don't log-spam per approval.
        with _CACHE_LOCK:
            _CACHE[key] = (now + _CACHE_TTL_SECONDS, True)
        return True

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        abi: list[Any] = [{
            "inputs": [{"name": "", "type": "address"}],
            "name": "isValidator",
            "outputs": [{"name": "", "type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        }]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(registry),
            abi=abi,
        )
        result = bool(contract.functions.isValidator(
            Web3.to_checksum_address(signer),
        ).call())
    except Exception as exc:
        # Same fail-open rationale as above: a dead RPC shouldn't halt the
        # whole network. We log and let the in-memory check below decide.
        logger.warning(
            "onchain-registry check failed for %s on chain %d: %s; "
            "failing open",
            signer, chain_id, exc,
        )
        with _CACHE_LOCK:
            _CACHE[key] = (now + _CACHE_TTL_SECONDS, True)
        return True

    with _CACHE_LOCK:
        _CACHE[key] = (now + _CACHE_TTL_SECONDS, result)
    return result


def clear_cache() -> None:
    """Wipe the cache. Used in tests; operators can also call this via health."""
    with _CACHE_LOCK:
        _CACHE.clear()
