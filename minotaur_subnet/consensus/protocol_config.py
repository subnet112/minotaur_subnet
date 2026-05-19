"""ProtocolConfig — single source of truth for network parameters held on
the ValidatorRegistry contract.

Off-chain components (validator daemon, relayer, API) import quorum_bps from
a ProtocolConfig instance rather than carrying their own copies. The
canonical value lives on-chain in ValidatorRegistry.quorumBps(); this module
reads it once at startup and refreshes periodically so a single owner tx on
the registry reconfigures the entire off-chain stack within one refresh
interval.

Why: previously the daemon, relayer, consensus manager and deployer each held
an independent default for the same parameter (10000 / 8000 / 6666). They
only agreed by operator coordination via env vars, and contract enforcement
was a fourth copy stored per-App. The consolidation removes the four-defaults
problem; this module is its off-chain entry point.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from web3 import Web3

logger = logging.getLogger(__name__)

# Minimal ABI for the views ProtocolConfig needs. Kept in-file rather than
# imported from a generated artifact so this module has no build-step coupling.
_VALIDATOR_REGISTRY_ABI = [
    {
        "name": "quorumBps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getValidators",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getValidatorCount",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

_OVERRIDE_ENV = "QUORUM_BPS_OVERRIDE"


@dataclass
class ProtocolConfig:
    """Network parameters shared by all off-chain components.

    quorum_bps is a plain int mutated in place by ``refresh_loop`` — consumers
    that need the current value should hold a reference to the ProtocolConfig
    instance and read ``cfg.quorum_bps`` at the start of each operation, not
    cache it.
    """

    quorum_bps: int
    rpc_url: str
    registry_address: str
    refresh_interval_seconds: int = 60

    @classmethod
    def from_validator_registry(
        cls,
        rpc_url: str,
        registry_address: str,
        refresh_interval_seconds: int = 60,
    ) -> "ProtocolConfig":
        """Read protocol parameters from the on-chain ValidatorRegistry once.

        Honours ``QUORUM_BPS_OVERRIDE`` if set — for local testnet and
        emergency overrides. Production deployments should leave it unset so
        the on-chain value remains authoritative.

        Raises if the override is unset AND the RPC call fails. Failing fast
        at startup is the right behaviour: a misconfigured registry address or
        unreachable RPC should be loud, not silently fall back to a hardcoded
        default that may or may not match the on-chain enforcement.
        """
        override = _read_override()
        if override is not None:
            logger.warning(
                "ProtocolConfig: using %s=%d (env override), skipping on-chain "
                "read from registry %s",
                _OVERRIDE_ENV, override, registry_address,
            )
            return cls(
                quorum_bps=override,
                rpc_url=rpc_url,
                registry_address=registry_address,
                refresh_interval_seconds=refresh_interval_seconds,
            )

        value = _read_quorum_bps(rpc_url, registry_address)
        logger.info(
            "ProtocolConfig: loaded quorum_bps=%d from ValidatorRegistry %s",
            value, registry_address,
        )
        return cls(
            quorum_bps=value,
            rpc_url=rpc_url,
            registry_address=registry_address,
            refresh_interval_seconds=refresh_interval_seconds,
        )

    async def refresh_loop(self) -> None:
        """Background task — re-read quorum_bps every refresh interval.

        Mutates ``self.quorum_bps`` in place when the on-chain value changes
        and logs at WARNING so the change is visible in operator logs. RPC
        failures during refresh keep the cached value and retry next tick;
        they do not crash the daemon.

        If the override env var is set, this is a no-op — no point polling
        when the value is forced.
        """
        if _read_override() is not None:
            logger.info(
                "ProtocolConfig: refresh_loop is a no-op while %s is set",
                _OVERRIDE_ENV,
            )
            return

        while True:
            try:
                await asyncio.sleep(self.refresh_interval_seconds)
                new_value = _read_quorum_bps(self.rpc_url, self.registry_address)
                if new_value != self.quorum_bps:
                    logger.warning(
                        "ProtocolConfig: quorum_bps changed %d -> %d on "
                        "ValidatorRegistry %s — consumers pick up the new "
                        "value on their next tick",
                        self.quorum_bps, new_value, self.registry_address,
                    )
                    self.quorum_bps = new_value
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "ProtocolConfig: refresh failed (keeping cached value %d): %s",
                    self.quorum_bps, exc,
                )


def _read_override() -> int | None:
    raw = os.environ.get(_OVERRIDE_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "ProtocolConfig: %s=%r is not an integer; ignoring",
            _OVERRIDE_ENV, raw,
        )
        return None


def _read_quorum_bps(rpc_url: str, registry_address: str) -> int:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_VALIDATOR_REGISTRY_ABI,
    )
    return int(registry.functions.quorumBps().call())
