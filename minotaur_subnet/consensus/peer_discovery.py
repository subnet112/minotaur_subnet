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
from urllib.parse import urlparse

import aiohttp

from .identity import ValidatorIdentity, verify_identity

logger = logging.getLogger(__name__)


_DEFAULT_PROBE_TIMEOUT_S = 3.0
_IDENTITY_PATH = "/identity"
_DNS_RESOLVE_TIMEOUT_S = 2.0


def _parse_axon_url(url: str) -> tuple[str, int] | None:
    """Extract (host, port) from an axon URL. Returns None on parse failure.

    Accepts ``http://host:port`` and ``http://host:port/``. Ports default
    only when scheme is explicit (http=80, https=443) — we want explicit
    ports for the axon comparison since validator daemons publish ip+port
    to the metagraph.
    """
    try:
        parsed = urlparse(url.rstrip("/"))
    except Exception:
        return None
    host = (parsed.hostname or "").strip().lower()
    port = parsed.port
    if not host or port is None:
        return None
    return host, port


async def _axon_urls_equivalent(
    metagraph_url: str,
    signed_url: str,
) -> bool:
    """Whether the two axon URLs point to the same network endpoint.

    The metagraph URL is always ``http://<ip>:<port>`` (Bittensor stores
    ip+port, not hostnames). The signed URL is whatever the operator put
    in ``VALIDATOR_AXON_URL`` — often a hostname behind a load balancer.

    Equivalence rules:
      - Ports must match exactly.
      - Hosts match if byte-equal (fast path), or
      - The signed host (typically a hostname) resolves to an IP that
        equals the metagraph host (always an IP). Resolves any DNS alias
        / load-balancer hostname to its set of A/AAAA records and checks
        membership.

    A signed URL that can't be parsed or whose host can't be resolved
    rejects (returns False) rather than crashing — same posture as the
    pre-fix byte-equal check.
    """
    mg = _parse_axon_url(metagraph_url)
    signed = _parse_axon_url(signed_url)
    if mg is None or signed is None:
        return False
    mg_host, mg_port = mg
    signed_host, signed_port = signed
    if mg_port != signed_port:
        return False
    if mg_host == signed_host:
        return True

    # Different host strings — try DNS resolution. The metagraph host is
    # always an IP literal; resolving it is a no-op (returns itself). The
    # signed host is the one we expect to be a hostname.
    try:
        loop = asyncio.get_event_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(signed_host, signed_port, type=0, proto=0),
            timeout=_DNS_RESOLVE_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug(
            "DNS resolution of signed axon host %s failed: %s",
            signed_host, exc,
        )
        return False

    resolved_ips = {info[4][0] for info in infos if info and info[4]}
    return mg_host in resolved_ips


@dataclass(frozen=True)
class PeerInfo:
    """A validator peer discovered via the identity probe.

    Frozen so the dataclass is hashable / safe to compare across refresh
    ticks; consumers that need the list per tick do their own dedup.
    """

    evm_address: str
    hotkey: str
    axon_url: str
    # Advisory public API base advertised by the peer's /identity (unsigned).
    # When set, peers route order-book pulls here instead of the axon→:8080
    # port transform. None on peers that don't advertise it (older images).
    api_url: str | None = None


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
    EVM, hotkey/axon mismatch).

    Retries network-level failures (TimeoutError, ClientError) ONCE before
    giving up. Pre-fix, a single transient hang on a peer's /identity
    handler would drop them from the discovered set for a full refresh
    cycle (60s by default) — and signatures arriving from that peer
    during the dropout window would be rejected as "non-validator" by the
    pre-#100 auth path. Per-attempt timeout is set by the caller via
    ``aiohttp.ClientSession.timeout``; the retry gives us roughly double
    the budget for a peer that's just slow this tick. Permanent failures
    (peer offline, peer not on chain, signature invalid) still reject on
    the first attempt — only transient flakes get retried.

    Failure reasons now log at INFO with the peer EVM + axon URL so
    operators can correlate discovery jitter with specific peers. Pre-fix
    these were all DEBUG and silently dropped, making diagnosis require
    code reading (caught live 2026-05-27 when discovery went 5→3 mid-
    order with zero visibility into which peers dropped or why).
    """
    url = metagraph_peer.axon_url.rstrip("/") + _IDENTITY_PATH
    body = None
    last_transient_err: str | None = None
    for attempt in range(2):
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.info(
                        "Identity probe %s returned HTTP %d — rejecting",
                        url, resp.status,
                    )
                    return None
                body = await resp.json()
                break
        except asyncio.TimeoutError:
            last_transient_err = "timeout"
            if attempt == 0:
                logger.debug(
                    "Identity probe %s timed out — retrying once", url,
                )
                continue
        except aiohttp.ClientError as exc:
            last_transient_err = f"client error: {type(exc).__name__}: {exc}"
            if attempt == 0:
                logger.debug(
                    "Identity probe %s client error %s — retrying once",
                    url, exc,
                )
                continue
        except Exception as exc:  # pragma: no cover — defensive
            logger.info(
                "Identity probe %s raised %s: %s — rejecting",
                url, type(exc).__name__, str(exc)[:120],
            )
            return None
        # Loop ended without ``break`` (we got an exception on this
        # iteration). Falls through to the next iteration of the for
        # loop, which is either the retry attempt OR the final fall-out.
    if body is None:
        logger.info(
            "Identity probe %s failed after 2 attempts (%s) — rejecting",
            url, last_transient_err or "unknown",
        )
        return None

    try:
        identity = ValidatorIdentity.from_dict(body)
    except (KeyError, TypeError, ValueError) as exc:
        logger.info(
            "Identity probe %s malformed payload (%s) — rejecting",
            url, exc,
        )
        return None

    recovered = verify_identity(identity)
    if recovered is None:
        logger.info(
            "Identity probe %s signature or expiry invalid — rejecting",
            url,
        )
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

    # Cross-check 3: the axon URL in the signed payload must point to the
    # same endpoint as the URL we actually probed. Pins the binding to a
    # specific host so a MitM can't redirect us to a different one.
    #
    # The metagraph URL is always ``http://<ip>:<port>`` (Bittensor stores
    # ip+port, not hostnames). The signed URL is whatever the operator set
    # in ``VALIDATOR_AXON_URL`` — typically the public hostname that points
    # to their load balancer. So byte-equal comparison rejected every
    # operator running behind a CDN / ELB / DNS alias, even though both
    # URLs resolve to the same place. We now compare host+port after DNS
    # resolution: signed host must resolve to (one of) the metagraph IP(s),
    # and ports must match.
    if not await _axon_urls_equivalent(metagraph_peer.axon_url, identity.axon_url):
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
        # Advisory, from the (verified) /identity payload. Not part of the
        # signed binding, so it's a best-effort routing hint only.
        api_url=getattr(identity, "api_url", None) or None,
    )
