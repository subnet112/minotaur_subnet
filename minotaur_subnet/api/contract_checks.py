"""Startup contract-presence verification.

On API boot, we check every configured on-chain contract address actually
has bytecode at the referenced chain. A configured address that points at
an empty account means something is broken (wrong chain, wrong address,
redeploy in progress, or an operator typo) and the API must refuse to
boot rather than run until the first real request trips on a cryptic
revert.

Env vars that are *unset* are treated as the corresponding feature being
disabled — no check is run. Only set-but-wrong is an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Per-chain RPC env var fallback chain. First non-empty wins.
_CHAIN_RPC_ENV: dict[int, tuple[str, ...]] = {
    1: ("ETH_RPC_URL", "ANVIL_RPC_URL"),
    8453: ("BASE_RPC_URL",),
    964: ("BITTENSOR_EVM_RPC_URL", "BITTENSOR_EVM_FORK_RPC_URL"),
}


@dataclass(frozen=True)
class _Check:
    label: str
    env_var: str
    chain_id: int


# Contracts we actively verify. Keep this list tight — it gates boot time.
_CHECKS: tuple[_Check, ...] = (
    _Check("ValidatorRegistry (Base)", "VALIDATOR_REGISTRY_8453", 8453),
    _Check("ValidatorRegistry (Ethereum)", "VALIDATOR_REGISTRY_1", 1),
    _Check("ValidatorRegistry (BT EVM)", "VALIDATOR_REGISTRY_964", 964),
    _Check("ChampionRegistry (BT EVM)", "CHAMPION_REGISTRY_964", 964),
    # AppRegistry is the source of truth for per-app contract resolution:
    # consensus/app_registry_cache.py gates every order against
    # APP_REGISTRY_{chain}, and apps are resolved per-order from the
    # AppIntentStore deployment record. We verify the registry, NOT a
    # single app contract — there is no global "AppIntentBase" address
    # anymore. The old APP_INTENT_BASE_<chain> checks pinned one app,
    # could not detect a retired-but-still-deployed address (a stale
    # value still has bytecode, so the check passed), and were never
    # consulted on the execution path.
    _Check("AppRegistry (Base)", "APP_REGISTRY_8453", 8453),
    _Check("AppRegistry (BT EVM)", "APP_REGISTRY_964", 964),
)


class ContractPresenceError(RuntimeError):
    """Raised when a configured contract address has no bytecode on chain."""


def _resolve_rpc(chain_id: int) -> str | None:
    for var in _CHAIN_RPC_ENV.get(chain_id, ()):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def verify_required_contracts(*, timeout_s: float = 10.0) -> list[str]:
    """Read env-configured addresses; fail boot if any is not a deployed contract.

    Returns the list of verified "label @ address" strings. Raises
    ContractPresenceError with every problem concatenated (not just the first)
    so the operator sees the full picture in one shot.
    """
    from web3 import Web3

    errors: list[str] = []
    verified: list[str] = []

    for check in _CHECKS:
        address = os.environ.get(check.env_var, "").strip()
        if not address:
            continue  # unset = feature disabled; nothing to verify

        if not Web3.is_address(address):
            errors.append(
                f"{check.label}: {check.env_var}={address!r} is not a valid EVM address"
            )
            continue

        rpc = _resolve_rpc(check.chain_id)
        if not rpc:
            fallback_names = _CHAIN_RPC_ENV.get(check.chain_id, ())
            errors.append(
                f"{check.label}: {check.env_var} is set but no RPC is configured for "
                f"chain {check.chain_id} (set one of: {', '.join(fallback_names) or 'n/a'})"
            )
            continue

        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": timeout_s}))
            code = w3.eth.get_code(Web3.to_checksum_address(address))
        except Exception as exc:
            errors.append(
                f"{check.label}: could not reach {rpc} to verify {address}: {exc}"
            )
            continue

        if len(code) == 0:
            errors.append(
                f"{check.label}: {check.env_var}={address} on chain {check.chain_id} "
                f"has no bytecode — is the contract deployed?"
            )
            continue

        verified.append(f"{check.label} @ {address}")

    if errors:
        raise ContractPresenceError(
            "Startup contract-presence check failed:\n  - "
            + "\n  - ".join(errors)
        )

    return verified
