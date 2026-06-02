"""Cache for on-chain `AppRegistry.appByContract()` lookups.

Mirror of [[validator_registry_cache]] but for the AppRegistry deployed by
the platform (see ``subnet112/minotaur_contracts/src/AppRegistry.sol``).
While the on-chain ``AppIntentBase._requireRegistered()`` is the real
security gate, hitting an unregistered-app revert from on-chain is wasteful:
we want to refuse orders at ingestion, refuse to sign them at validation,
and refuse to submit them at the relayer so that the only way to even
reach the contract is via a registered App.

Enforcement is per-chain and *implicit*: configure ``APP_REGISTRY_{chain_id}``
with the deployed registry address to turn the check on; leave it unset
to mirror the contract's ``address(0)`` escape hatch (no check). This
matches the contract semantic and avoids a separate global feature flag.

The cache is short-lived (5s TTL, per ``(chain_id, contract_address)`` key)
so the RPC overhead is negligible while still letting freshly-registered
apps go live within seconds.

Failure modes:
- Registry env unset for chain → fail open (return True). The contract
  was deployed without a registry, so there's nothing to enforce against.
- RPC URL unset or RPC call fails → fail open with a loud WARN log. The
  on-chain ``_requireRegistered`` check is still the real gate; halting
  the whole order pipeline on a transient RPC outage would be worse than
  letting the on-chain revert do its job.
- Registry says contract is not registered → return False. Caller decides
  the user-visible response (HTTPException, rejection code, SubmitResult).
"""

from __future__ import annotations

import logging
import os
import time
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)

# Per (chain_id, contract_address.lower()) -> (expires_at, is_registered)
_CACHE: dict[tuple[int, str], tuple[float, bool]] = {}
_CACHE_LOCK = RLock()
_CACHE_TTL_SECONDS = 5.0


def _chain_registry_env(chain_id: int) -> str:
    return os.environ.get(f"APP_REGISTRY_{chain_id}", "").strip()


def _chain_rpc_env(chain_id: int) -> str:
    # Live-chain reads (registry / validator set / score) must hit the LIVE
    # chain, never the sim fork. Prefer the operator's *_UPSTREAM_RPC_URL — the
    # live RPC they already supply for Anvil's --fork-url — then fall back to
    # the plain RPC (direct-live or local-dev, where "live" IS the local node).
    # No hardcoded URLs; if none is set the caller fails open with a WARN.
    if chain_id == 8453:
        return (os.environ.get("BASE_UPSTREAM_RPC_URL", "").strip()
                or os.environ.get("BASE_RPC_URL", "").strip())
    if chain_id == 1:
        return (os.environ.get("ETH_UPSTREAM_RPC_URL", "").strip()
                or os.environ.get("ETH_RPC_URL", "").strip()
                or os.environ.get("ANVIL_RPC_URL", "").strip())
    if chain_id == 964:
        return (os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL", "").strip()
                or os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
                or os.environ.get("BITTENSOR_EVM_FORK_RPC_URL", "").strip())
    return ""


def enforce_enabled(chain_id: int) -> bool:
    """True when an AppRegistry address is configured for *chain_id*.

    Per-chain so an operator can run beta on one chain (registry deployed,
    enforcement on) while leaving another chain unconfigured.
    """
    return bool(_chain_registry_env(int(chain_id)))


def is_registered_app(contract_address: str, chain_id: int) -> bool:
    """Return True if the App contract is currently registered.

    Semantics when enforcement is off for this chain (registry env unset):
    always True — callers can hard-wire the lookup without breaking setups
    that have no registry deployed.

    When enforcement is on:
    - Cache hit within 5s → return cached.
    - Cache miss → ``registry.appByContract(contract_address) != bytes32(0)``
      via the chain's RPC; cache and return.
    - RPC unreachable or call fails → fail open with WARN; the on-chain
      ``_requireRegistered`` check remains the security guarantee.
    """
    if not contract_address:
        return True
    if not enforce_enabled(chain_id):
        return True

    key = (int(chain_id), contract_address.lower())
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]

    registry = _chain_registry_env(int(chain_id))
    rpc = _chain_rpc_env(int(chain_id))
    if not rpc:
        logger.warning(
            "app-registry check skipped for chain %d (registry=set, rpc=unset); "
            "failing open",
            chain_id,
        )
        with _CACHE_LOCK:
            _CACHE[key] = (now + _CACHE_TTL_SECONDS, True)
        return True

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        abi: list[Any] = [{
            "inputs": [{"name": "contractAddr", "type": "address"}],
            "name": "appByContract",
            "outputs": [{"name": "", "type": "bytes32"}],
            "stateMutability": "view",
            "type": "function",
        }]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(registry),
            abi=abi,
        )
        app_id_bytes = contract.functions.appByContract(
            Web3.to_checksum_address(contract_address),
        ).call()
        # bytes32(0) means "not registered (or revoked)"
        result = any(b != 0 for b in app_id_bytes)
    except Exception as exc:
        logger.warning(
            "app-registry check failed for %s on chain %d: %s; failing open",
            contract_address, chain_id, exc,
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
