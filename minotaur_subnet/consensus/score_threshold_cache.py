"""Per-(chain_id, contract_address) cache of an App's on-chain scoreThreshold.

Why this exists: `ConsensusManager.sign_approval` previously hard-coded
`score_bps=5000` in the EIP-712 PlanApproval payload. The on-chain verifier
(`AppIntentBase.verifyValidatorSignatures`) reconstructs the digest using the
App's own `scoreThreshold()` value, which the App developer chose at deploy
time (constructor enforces a floor of 5000 but they can set it higher). When
the App sets a threshold > 5000, every follower's signature is silently
invalid on-chain — the chain rejects the quorum and no execution happens.

Behaviour: contract.scoreThreshold() is `view` and the value is immutable
after deploy (per AppIntentBase). So we cache forever within a process. The
cache key is `(chain_id, contract_address.lower())`. On read failure we fall
back to the legacy constant (5000) and log a warning — better to attempt a
sig with a guess than to silently refuse to participate in consensus.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from minotaur_subnet.chains import registry

logger = logging.getLogger(__name__)


_DEFAULT_THRESHOLD_BPS = 5000

# Per (chain_id, contract_address.lower()) -> threshold_bps
_CACHE: dict[tuple[int, str], int] = {}
_CACHE_LOCK = threading.Lock()


def _chain_rpc_env(chain_id: int) -> str:
    """Resolve the LIVE RPC URL for chain_id (the shared registry ladder)."""
    return registry.live_rpc(chain_id)


def score_threshold_for(
    contract_address: str,
    chain_id: int,
    fallback_bps: int = _DEFAULT_THRESHOLD_BPS,
) -> int:
    """Return the App's on-chain `scoreThreshold()` in basis points.

    Returns `fallback_bps` (default 5000) when the contract is unreachable
    or the call reverts. Cached forever after the first successful read,
    since scoreThreshold is immutable in AppIntentBase.
    """
    if not contract_address or not contract_address.startswith("0x"):
        return fallback_bps

    key = (int(chain_id), contract_address.lower())
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    rpc = _chain_rpc_env(int(chain_id))
    if not rpc:
        logger.warning(
            "score-threshold read skipped for chain %d (no RPC env); "
            "falling back to %d bps",
            chain_id, fallback_bps,
        )
        return fallback_bps

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        abi: list[Any] = [{
            "inputs": [],
            "name": "scoreThreshold",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=abi,
        )
        value = int(contract.functions.scoreThreshold().call())
    except Exception as exc:
        logger.warning(
            "score-threshold read failed for %s on chain %d: %s; "
            "falling back to %d bps (NOT caching the failure)",
            contract_address, chain_id, exc, fallback_bps,
        )
        return fallback_bps

    if value < _DEFAULT_THRESHOLD_BPS:
        logger.warning(
            "scoreThreshold for %s on chain %d returned %d (< floor %d); "
            "treating as floor",
            contract_address, chain_id, value, _DEFAULT_THRESHOLD_BPS,
        )
        value = _DEFAULT_THRESHOLD_BPS

    with _CACHE_LOCK:
        _CACHE[key] = value
    logger.info(
        "cached on-chain scoreThreshold for %s on chain %d: %d bps",
        contract_address, chain_id, value,
    )
    return value


def invalidate(contract_address: str | None = None, chain_id: int | None = None) -> None:
    """Test helper / operator escape hatch. Clears all entries when both args
    are None; clears one entry when both are given."""
    with _CACHE_LOCK:
        if contract_address is None and chain_id is None:
            _CACHE.clear()
            return
        if contract_address is not None and chain_id is not None:
            _CACHE.pop((int(chain_id), contract_address.lower()), None)
