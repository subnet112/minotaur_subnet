"""Peer discovery — combines Bittensor metagraph axon URLs with the on-chain
ValidatorRegistry to build the off-chain peer list automatically.

Replaces the previous VALIDATOR_PEERS env-driven manual config. New
validators joining the network are picked up automatically:
  - They register on Bittensor (gets them into the metagraph with an axon URL)
  - Their EVM signing address gets added to ValidatorRegistry by the owner
    (the permissioning gate — see validator quickstart Step 4)
  - On the next discovery tick (~60s default), every other validator finds
    them, probes their /identity endpoint, verifies the binding, and adds
    them to the peer list

The discovery is best-effort: offline validators are silently skipped,
verification failures are logged at debug level. The peer list always
reflects who's currently reachable AND authorized — not the union of
"ever-known" peers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

import aiohttp

from .identity import ValidatorIdentity, verify_identity

logger = logging.getLogger(__name__)


_DEFAULT_PROBE_TIMEOUT_S = 3.0
_IDENTITY_PATH = "/identity"


@dataclass(frozen=True)
class PeerInfo:
    """A validator peer discovered via the identity probe.

    Frozen so the dataclass is hashable / safe to compare across refresh
    ticks; consumers that need the list per tick do their own dedup.
    """

    evm_address: str
    hotkey: str
    axon_url: str


@dataclass(frozen=True)
class MetagraphPeer:
    """Minimal metagraph projection that discovery needs.

    Decoupled from ``validator.metagraph_sync.PeerInfo`` so this module
    is unit-testable without spinning up bittensor.
    """

    hotkey: str
    axon_url: str


async def discover_peers(
    metagraph_peers: Sequence[MetagraphPeer],
    authorized_evm_addresses: Iterable[str],
    my_evm_address: str,
    *,
    probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_S,
    session: aiohttp.ClientSession | None = None,
) -> list[PeerInfo]:
    """Probe every metagraph peer's /identity endpoint and return verified peers.

    Args:
        metagraph_peers: All validators currently in the metagraph with a
            non-empty axon URL. The caller (typically MetagraphSync) is
            responsible for the metagraph lookup.
        authorized_evm_addresses: EVM addresses currently authorized to sign,
            read from ``ValidatorRegistry.getValidators()``. Identities whose
            recovered EVM is not in this set are rejected.
        my_evm_address: This validator's own EVM. Excluded from the result.
        probe_timeout_seconds: Per-peer HTTP timeout. Discovery is best-effort,
            so a tight timeout is fine — offline peers re-appear on the next
            refresh tick.
        session: Optional existing aiohttp session. If None, one is created
            and closed inside this call (per-discovery-cycle session is fine
            for the small N-of-validators scale we have).

    Returns:
        Deduplicated list of verified peers (excluding self).
    """
    authorized_lower = {a.lower() for a in authorized_evm_addresses}
    my_evm_lower = my_evm_address.lower()

    candidates = [
        p for p in metagraph_peers
        if p.axon_url and p.axon_url.startswith(("http://", "https://"))
    ]
    if not candidates:
        logger.info("Peer discovery: no metagraph peers with axon URLs")
        return []

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=probe_timeout_seconds),
        )

    try:
        results = await asyncio.gather(
            *(
                _probe_one(session, p, authorized_lower, my_evm_lower)
                for p in candidates
            ),
            return_exceptions=False,  # _probe_one catches its own errors
        )
    finally:
        if own_session:
            await session.close()

    verified: dict[str, PeerInfo] = {}
    failures = 0
    for r in results:
        if r is None:
            failures += 1
            continue
        # Dedup by EVM address — first-seen wins. If two metagraph entries
        # claim the same EVM, the second is suspicious anyway (one EVM
        # should correspond to one bittensor hotkey under our model).
        if r.evm_address.lower() not in verified:
            verified[r.evm_address.lower()] = r

    logger.info(
        "Peer discovery: probed %d candidates → %d verified, %d failed/skipped",
        len(candidates), len(verified), failures,
    )
    return list(verified.values())


async def _probe_one(
    session: aiohttp.ClientSession,
    metagraph_peer: MetagraphPeer,
    authorized_lower: set[str],
    my_evm_lower: str,
) -> PeerInfo | None:
    """Probe a single peer's /identity endpoint and verify the binding.

    Returns None on any failure (network error, bad signature, unauthorized
    EVM, hotkey/axon mismatch). Logs at debug to avoid spam from offline
    peers.
    """
    url = metagraph_peer.axon_url.rstrip("/") + _IDENTITY_PATH
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug(
                    "Identity probe %s returned HTTP %d",
                    url, resp.status,
                )
                return None
            body = await resp.json()
    except asyncio.TimeoutError:
        logger.debug("Identity probe %s timed out", url)
        return None
    except aiohttp.ClientError as exc:
        logger.debug("Identity probe %s failed: %s", url, exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Identity probe %s raised: %s", url, exc)
        return None

    try:
        identity = ValidatorIdentity.from_dict(body)
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("Identity probe %s malformed payload: %s", url, exc)
        return None

    recovered = verify_identity(identity)
    if recovered is None:
        logger.debug("Identity probe %s signature/expiry invalid", url)
        return None

    # Cross-check 1: recovered EVM must be authorized on-chain
    if recovered.lower() not in authorized_lower:
        logger.warning(
            "Identity probe %s recovered EVM %s but it is not in "
            "ValidatorRegistry.getValidators() — rejecting",
            url, recovered,
        )
        return None

    # Cross-check 2: the hotkey signed in /identity must match what the
    # metagraph reports for the axon URL we hit. Otherwise an attacker
    # who controls a registered EVM could publish a /identity claiming
    # someone else's hotkey to redirect traffic.
    if identity.hotkey != metagraph_peer.hotkey:
        logger.warning(
            "Identity probe %s hotkey mismatch: signed=%s metagraph=%s",
            url, identity.hotkey, metagraph_peer.hotkey,
        )
        return None

    # Cross-check 3: the axon URL in the signed payload must match the URL
    # we actually probed. Pins the binding to the specific endpoint so a
    # MitM can't redirect us to a different host.
    if identity.axon_url.rstrip("/") != metagraph_peer.axon_url.rstrip("/"):
        logger.warning(
            "Identity probe %s axon mismatch: signed=%s metagraph=%s",
            url, identity.axon_url, metagraph_peer.axon_url,
        )
        return None

    # Exclude self
    if recovered.lower() == my_evm_lower:
        return None

    return PeerInfo(
        evm_address=recovered,
        hotkey=identity.hotkey,
        axon_url=metagraph_peer.axon_url,
    )
