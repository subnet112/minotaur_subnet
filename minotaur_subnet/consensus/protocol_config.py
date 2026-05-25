"""ProtocolConfig — single source of truth for off-chain network parameters.

Holds two things that every off-chain component used to maintain its own
copy of:

  - ``quorum_bps``: read from ``ValidatorRegistry.quorumBps()`` on chain.
  - ``peers``: discovered via the Bittensor metagraph axon list + on-chain
    ``ValidatorRegistry.getValidators()`` + each peer's ``GET /identity``
    EIP-712 attestation (see ``consensus.peer_discovery``).

A background ``refresh_loop`` re-reads both once per epoch and mutates the
values in place. Consumers (``ConsensusManager``, ``ValidatorPeerNetwork``)
hold a reference and read through the attributes on each operation, so
on-chain ``setQuorumBps``, on-chain ``updateValidators``, and new peers
joining the metagraph all propagate automatically without restart.

Why: previously the daemon, relayer, consensus manager and deployer each
held an independent default for the same parameter (10000 / 8000 / 6666) and
peer lists were duplicated across env vars on every box. Both classes of
config drift are eliminated by making this module the canonical reader.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

import aiohttp
from web3 import Web3

from .peer_discovery import MetagraphPeer, PeerInfo, discover_peers

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


# Async provider type: the daemon wires this to a function that returns the
# current metagraph view. ProtocolConfig doesn't import bittensor itself.
MetagraphPeerProvider = Callable[[], Awaitable[Sequence[MetagraphPeer]]]


@dataclass
class ProtocolConfig:
    """Network parameters shared by all off-chain components."""

    quorum_bps: int
    rpc_url: str
    registry_address: str
    refresh_interval_seconds: int = 60

    # Populated by the refresh loop when a metagraph provider + my_evm_address
    # are configured. List is mutated in place; callers should hold a reference
    # and re-read on each operation.
    peers: list[PeerInfo] = field(default_factory=list)

    # ── Discovery wiring (optional) ──────────────────────────────────────
    # When these are set, refresh_loop runs peer discovery on each tick.
    # When unset (e.g. single-validator test setups), peers stays empty.
    my_evm_address: str = ""
    metagraph_provider: MetagraphPeerProvider | None = None
    probe_timeout_seconds: float = 3.0

    # ── Quorum source override (optional) ────────────────────────────────
    # Champion-consensus reads its validator set from BT EVM ValidatorRegistry
    # (same as order-consensus uses on Base) but its quorum threshold from
    # ChampionRegistry, which keeps an independent ``quorumBps``. When set,
    # ``_read_quorum_bps`` reads from this address instead of registry_address.
    # When unset (the default), the same contract serves both reads — matches
    # the order-consensus topology where ValidatorRegistry holds both.
    quorum_address: str = ""

    @classmethod
    def from_validator_registry(
        cls,
        rpc_url: str,
        registry_address: str,
        refresh_interval_seconds: int = 60,
        *,
        my_evm_address: str = "",
        metagraph_provider: MetagraphPeerProvider | None = None,
        quorum_address: str = "",
    ) -> "ProtocolConfig":
        """Read protocol parameters from the on-chain ValidatorRegistry once.

        Honours ``QUORUM_BPS_OVERRIDE`` if set (for local testnet and
        emergency overrides).

        Discovery is wired only when ``my_evm_address`` and
        ``metagraph_provider`` are both supplied. Without them, ``peers``
        stays empty and ``refresh_loop`` only updates ``quorum_bps``.

        Raises if the override is unset AND the RPC call fails. Failing fast
        at startup is the right behaviour: a misconfigured registry address or
        unreachable RPC should be loud, not silently fall back to a default.
        """
        quorum_source = quorum_address or registry_address
        override = _read_override()
        if override is not None:
            logger.warning(
                "ProtocolConfig: using %s=%d (env override), skipping on-chain "
                "read from registry %s",
                _OVERRIDE_ENV, override, quorum_source,
            )
            return cls(
                quorum_bps=override,
                rpc_url=rpc_url,
                registry_address=registry_address,
                refresh_interval_seconds=refresh_interval_seconds,
                my_evm_address=my_evm_address,
                metagraph_provider=metagraph_provider,
                quorum_address=quorum_address,
            )

        value = _read_quorum_bps(rpc_url, quorum_source)
        if quorum_address and quorum_address != registry_address:
            logger.info(
                "ProtocolConfig: loaded quorum_bps=%d from quorum source %s "
                "(validator set from %s)",
                value, quorum_source, registry_address,
            )
        else:
            logger.info(
                "ProtocolConfig: loaded quorum_bps=%d from ValidatorRegistry %s",
                value, registry_address,
            )
        return cls(
            quorum_bps=value,
            rpc_url=rpc_url,
            registry_address=registry_address,
            refresh_interval_seconds=refresh_interval_seconds,
            my_evm_address=my_evm_address,
            metagraph_provider=metagraph_provider,
            quorum_address=quorum_address,
        )

    async def refresh_loop(self) -> None:
        """Background task — re-read quorum + peers every refresh interval.

        RPC / discovery failures during refresh keep the cached values and
        retry on the next tick. They do not crash the daemon.

        If the override env var is set, the quorum refresh is a no-op (peer
        discovery still runs if wired).
        """
        override_active = _read_override() is not None
        if override_active:
            logger.info(
                "ProtocolConfig: quorum refresh skipped (%s set); peer "
                "discovery still active if wired",
                _OVERRIDE_ENV,
            )

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.probe_timeout_seconds),
        ) as session:
            while True:
                try:
                    await asyncio.sleep(self.refresh_interval_seconds)

                    if not override_active:
                        await self._refresh_quorum()

                    if self.metagraph_provider is not None and self.my_evm_address:
                        await self._refresh_peers(session)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "ProtocolConfig: refresh tick failed (keeping cached "
                        "values): %s",
                        exc,
                    )

    async def _refresh_quorum(self) -> None:
        quorum_source = self.quorum_address or self.registry_address
        new_value = _read_quorum_bps(self.rpc_url, quorum_source)
        if new_value != self.quorum_bps:
            logger.warning(
                "ProtocolConfig: quorum_bps changed %d -> %d on %s — "
                "consumers pick up the new value on their next tick",
                self.quorum_bps, new_value, quorum_source,
            )
            self.quorum_bps = new_value

    async def _refresh_peers(self, session: aiohttp.ClientSession) -> None:
        assert self.metagraph_provider is not None
        try:
            metagraph_peers = await self.metagraph_provider()
        except Exception as exc:
            logger.warning(
                "ProtocolConfig: metagraph provider failed (keeping cached "
                "%d peers): %s",
                len(self.peers), exc,
            )
            return

        authorized = _read_validators(self.rpc_url, self.registry_address)

        new_peers = await discover_peers(
            metagraph_peers=metagraph_peers,
            authorized_evm_addresses=authorized,
            my_evm_address=self.my_evm_address,
            probe_timeout_seconds=self.probe_timeout_seconds,
            session=session,
        )

        # Mutate in place — consumers hold a reference to self.peers.
        before = {p.evm_address.lower() for p in self.peers}
        after = {p.evm_address.lower() for p in new_peers}
        if before != after:
            added = after - before
            removed = before - after
            logger.warning(
                "ProtocolConfig: peer set changed (added=%d removed=%d "
                "total=%d)",
                len(added), len(removed), len(new_peers),
            )
        self.peers[:] = new_peers


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


def _read_validators(rpc_url: str, registry_address: str) -> list[str]:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_VALIDATOR_REGISTRY_ABI,
    )
    return [str(a) for a in registry.functions.getValidators().call()]
