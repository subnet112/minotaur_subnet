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
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

import aiohttp
from web3 import Web3

from minotaur_subnet.blockchain.web3_retry import build_retrying_web3

from minotaur_subnet.chains import registry

from .peer_discovery import MetagraphPeer, PeerInfo, discover_peers

logger = logging.getLogger(__name__)


def consensus_chain_rpc_url(chain_id: int) -> str:
    """Pick the right RPC URL for *consensus chain reads* on ``chain_id``.

    Order-consensus + champion-consensus read on-chain state (validator
    set, quorum bps) from this URL. They MUST hit a live upstream, not
    a local Anvil fork: Anvil forks freeze upstream state at the fork
    point, so an on-chain ``updateValidators`` is invisible to forks
    until they're recycled — which can take up to the recycle-cron
    interval (6 h on prod). That blocks new validators from being
    recognized + delays removals.

    Resolution per chain:
      - **8453 (Base)**: ``BASE_UPSTREAM_RPC_URL`` if set.
      - **964 (BT EVM)**: ``BITTENSOR_EVM_UPSTREAM_RPC_URL`` if set,
        otherwise ``BITTENSOR_EVM_RPC_URL``, otherwise the public lite
        endpoint.
      - **1 (Ethereum mainnet)**: ``ETH_UPSTREAM_RPC_URL`` if set.
      - **31337 / 1337 (local Anvil)**: there is no upstream — fall
        back to ``ANVIL_RPC_URL`` / ``BASE_RPC_URL`` / localhost.

    Falls back to the local Anvil URL whenever the appropriate upstream
    env is not set, to keep local-testnet behaviour unchanged.

    The per-chain upstream ladder + public/local fallback are defined once in
    the chain registry (``registry.consensus_rpc``).
    """
    return registry.consensus_rpc(chain_id)


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

# Minimal ABI for the one ChampionRegistry view the nonce floor needs.
# ChampionRegistry is a DISTINCT contract from ValidatorRegistry (it lives at
# ``quorum_address``); it enforces ``require(nonce > lastNonce[signer])`` in
# ``certify()`` and exposes the per-signer high-water as a public mapping getter.
_CHAMPION_REGISTRY_ABI = [
    {
        "name": "lastNonce",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Bound the inline lastNonce read so a hung BT-EVM RPC can't stall the API event
# loop (the champion nonce floor is fail-open, but a hang isn't a catchable
# error without a timeout). A few seconds is ample for a single view call.
_NONCE_READ_TIMEOUT_SECONDS = 5.0

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

    # Canonical validator count from the on-chain ``ValidatorRegistry``
    # (``getValidatorCount()``). This is the DENOMINATOR for quorum
    # calculations — ``ConsensusManager.quorum_required`` reads this,
    # not ``len(peers)``.
    #
    # Why this is separate from ``peers``: ``peers`` is the OFF-CHAIN
    # discovered set (post-/identity-attestation) and jitters with peer
    # availability — a momentarily-attested peer that drops on the next
    # refresh, two attestations of the same address with different
    # case-folding, etc. We saw this bite live on prod 2026-05-27 when
    # ``len(self.validators)`` briefly went 6 → 7 → 6 across a single
    # order's consensus window, recording ``quorum=5`` instead of the
    # chain-truth ``quorum=4`` and rejecting an order that had every
    # legitimate signature it needed. The on-chain count doesn't jitter:
    # it changes only when the owner calls ``updateValidators``, and
    # that emits ``ValidatorsUpdated`` (auditable on chain).
    #
    # Defaults to ``0`` so existing tests that construct ProtocolConfig
    # directly (without ``from_validator_registry``) continue to work;
    # ``quorum_required`` treats ``0`` as a sentinel meaning "no on-chain
    # source configured" and falls back to len(validators) for backward
    # compat — see ``ConsensusManager.quorum_required``.
    on_chain_validator_count: int = 0

    # Canonical authorized signer set from the on-chain ValidatorRegistry
    # (``getValidators()``). The authoritative answer to "is this address
    # allowed to sign?" — used by ``ConsensusManager._receive_approval`` to
    # authorize incoming approvals.
    #
    # Why this is separate from ``peers``: ``peers`` is the DISCOVERED set
    # (post-/identity-attestation, gated by network reachability). It can
    # shrink transiently if a peer's /identity probe times out mid-refresh
    # — even though that peer is still authorized on chain. Caught live
    # 2026-05-27: discovery went 5→3 mid-order, and incoming approvals
    # from the two dropped peers were rejected as "non-validator" even
    # though they were on the chain registry and had returned valid
    # signatures to the leader's own broadcast.
    #
    # The on-chain list doesn't depend on reachability — it changes only
    # when the owner calls ``updateValidators`` (emits ``ValidatorsUpdated``).
    # Authorization checks read THIS; broadcast targets read ``peers``.
    #
    # Defaults to empty for backward compat with tests that build
    # ProtocolConfig manually without ``from_validator_registry``;
    # ``ConsensusManager`` falls back to the in-memory union when empty.
    on_chain_validators: list[str] = field(default_factory=list)

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

    # ── Peer-eviction hysteresis ─────────────────────────────────────────
    # A previously-verified peer is only evicted from ``peers`` after this
    # many CONSECUTIVE refresh cycles of failed identity probes (unless it
    # is de-authorized on-chain, which evicts immediately). Probes are
    # unreliable under local load: 2026-07-16 a CPU-stalled leader timed
    # out probing every peer — including ITSELF — zeroed the peer set, and
    # the order proposed one second later was broadcast to nobody and
    # terminally rejected ("Consensus not reached"). One bad probe round
    # must not empty the set. Env: PEER_EVICTION_CONSECUTIVE_MISSES.
    peer_eviction_misses: int = field(
        default_factory=lambda: int(
            os.environ.get("PEER_EVICTION_CONSECUTIVE_MISSES", "").strip() or 3
        )
    )
    # evm_address_lower -> consecutive refresh cycles the peer failed its
    # probe. Reset on any successful probe; pruned on eviction.
    _peer_missing_streaks: dict[str, int] = field(default_factory=dict)

    # ── Quorum source override (optional) ────────────────────────────────
    # Champion-consensus reads its validator set from BT EVM ValidatorRegistry
    # (same as order-consensus uses on Base) but its quorum threshold from
    # ChampionRegistry, which keeps an independent ``quorumBps``. When set,
    # ``_read_quorum_bps`` reads from this address instead of registry_address.
    # When unset (the default), the same contract serves both reads — matches
    # the order-consensus topology where ValidatorRegistry holds both.
    quorum_address: str = ""

    # ── Observability: last on-chain read snapshot ───────────────────────
    # Stamped at construction and on every successful refresh tick, and
    # surfaced verbatim via ``observability_snapshot()`` under
    # ``/health.champion_consensus.registry_view``. The point: a stale
    # registry view (the cause of an impossible-looking quorum like
    # ``5-of-5`` on a 6-validator network) is then DIRECTLY visible — the
    # exact count + validator set + block height + refresh freshness the
    # node is acting on — instead of being inferred backwards from
    # ``quorum_required``. The validator-health workflow diffs these
    # against chain truth to raise a ``stale_registry_view`` finding.
    #
    # chain_id: read once at construction (never changes for a given RPC).
    chain_id: int = 0
    # last_refresh_block: ``eth_blockNumber`` seen on the most recent read.
    # A height that lags the chain head ⇒ the RPC node is out of sync and
    # likely serving stale contract reads.
    last_refresh_block: int = 0
    # last_successful_refresh_at: wall-clock of the last FULLY-successful
    # read (count + validators both fetched). Stays put when reads fail, so
    # an old value ⇒ the refresh loop is wedged and the cached count/set are
    # frozen. Mirrors the ``last_successful_emit`` weight-health pattern.
    last_successful_refresh_at: float | None = None
    # last_refresh_error: short string of the most recent refresh failure,
    # cleared on the next success. None ⇒ last read was clean.
    last_refresh_error: str | None = None

    def observability_snapshot(self) -> dict:
        """In-memory snapshot of the last on-chain registry read.

        Pure attribute read — performs NO RPC, so it is safe to call on the
        hot ``/health`` path. See the ``chain_id`` / ``last_refresh_*``
        field docs above for why each value is surfaced.
        """
        return {
            "on_chain_validator_count": self.on_chain_validator_count,
            "on_chain_validators": sorted(a.lower() for a in self.on_chain_validators),
            "quorum_bps": self.quorum_bps,
            "registry_address": self.registry_address,
            "chain_id": self.chain_id,
            "rpc_block_number": self.last_refresh_block,
            "last_successful_refresh": self.last_successful_refresh_at,
            "last_refresh_error": self.last_refresh_error,
        }

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

        ``quorum_address`` is REQUIRED and must be passed explicitly by every
        caller — it names the registry whose ``quorumBps()`` defines the
        quorum threshold. There is no silent fallback to ``registry_address``:
        for order consensus the two happen to be the same ValidatorRegistry,
        but the champion path uses a distinct ChampionRegistry, and silently
        defaulting to the wrong contract would target the wrong chain/registry
        without any error. Callers must be explicit so the misconfiguration is
        loud, not silent.
        """
        if not quorum_address:
            raise ValueError(
                "from_validator_registry requires an explicit quorum_address "
                "(the registry whose quorumBps() defines the quorum); callers must "
                "pass it — no silent fallback to registry_address."
            )
        quorum_source = quorum_address
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
        # Read the on-chain validator count alongside quorum_bps so the
        # off-chain quorum denominator matches what the on-chain verifier
        # would use. Best-effort: if this read fails (e.g. older deployed
        # registry without ``getValidatorCount``), we record 0 and the
        # consensus manager falls back to its in-memory len(validators).
        read_error: str | None = None
        try:
            on_chain_count = _read_validator_count(rpc_url, registry_address)
        except Exception as exc:
            logger.warning(
                "ProtocolConfig: getValidatorCount() failed on %s (%s); "
                "quorum will fall back to in-memory validator count "
                "(may be sensitive to peer-discovery jitter)",
                registry_address, exc,
            )
            on_chain_count = 0
            read_error = f"getValidatorCount: {exc}"
        # Also load the on-chain validator addresses. Used by the
        # approval-authorization check (see ``ProtocolConfig.on_chain_validators``).
        # Best-effort: same fallback semantics as the count above.
        try:
            on_chain_addrs = _read_validators(rpc_url, registry_address)
        except Exception as exc:
            logger.warning(
                "ProtocolConfig: getValidators() failed on %s (%s); "
                "approval-auth will fall back to in-memory validators "
                "(may reject valid sigs during peer-discovery jitter)",
                registry_address, exc,
            )
            on_chain_addrs = []
            read_error = f"getValidators: {exc}"
        # Observability stamps (best-effort — never block construction).
        # chain_id never changes for an RPC, so it's read only here.
        try:
            chain_id = _read_chain_id(rpc_url)
        except Exception:
            chain_id = 0
        try:
            block = _read_block_number(rpc_url)
        except Exception:
            block = 0
        # Only mark "last successful refresh" when both registry reads
        # actually landed — so a frozen cache is distinguishable from a
        # fresh one downstream.
        refreshed_at = time.time() if read_error is None else None
        if quorum_address and quorum_address != registry_address:
            logger.info(
                "ProtocolConfig: loaded quorum_bps=%d (count=%d) from quorum source %s "
                "(validator set from %s)",
                value, on_chain_count, quorum_source, registry_address,
            )
        else:
            logger.info(
                "ProtocolConfig: loaded quorum_bps=%d (count=%d) from ValidatorRegistry %s",
                value, on_chain_count, registry_address,
            )
        return cls(
            quorum_bps=value,
            on_chain_validator_count=on_chain_count,
            on_chain_validators=on_chain_addrs,
            rpc_url=rpc_url,
            registry_address=registry_address,
            refresh_interval_seconds=refresh_interval_seconds,
            my_evm_address=my_evm_address,
            metagraph_provider=metagraph_provider,
            quorum_address=quorum_address,
            chain_id=chain_id,
            last_refresh_block=block,
            last_successful_refresh_at=refreshed_at,
            last_refresh_error=read_error,
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

        # Also refresh the on-chain validator count. Same source as
        # construction; logged as a warning when it changes so operators
        # can correlate quorum_required shifts with on-chain
        # ``updateValidators`` calls without grepping events.
        try:
            new_count = _read_validator_count(self.rpc_url, self.registry_address)
        except Exception as exc:
            logger.warning(
                "ProtocolConfig: getValidatorCount() refresh failed on %s "
                "(%s); keeping cached count=%d",
                self.registry_address, exc, self.on_chain_validator_count,
            )
            self.last_refresh_error = f"getValidatorCount: {exc}"
            return
        if new_count != self.on_chain_validator_count:
            logger.warning(
                "ProtocolConfig: validator_count changed %d -> %d on %s — "
                "quorum_required will pick up the new value on next propose",
                self.on_chain_validator_count, new_count, self.registry_address,
            )
            self.on_chain_validator_count = new_count

        # Refresh the authorized validator address list alongside the count.
        # Used by ConsensusManager.authorize_approver — see
        # ``ProtocolConfig.on_chain_validators``. Lowercased for case-
        # insensitive lookups in the auth check.
        try:
            new_addrs = _read_validators(self.rpc_url, self.registry_address)
        except Exception as exc:
            logger.warning(
                "ProtocolConfig: getValidators() refresh failed on %s "
                "(%s); keeping cached %d addresses",
                self.registry_address, exc, len(self.on_chain_validators),
            )
            self.last_refresh_error = f"getValidators: {exc}"
            return
        # Detect set change for operator visibility (correlated with on-chain
        # ``updateValidators`` events).
        before = {a.lower() for a in self.on_chain_validators}
        after = {a.lower() for a in new_addrs}
        if before != after:
            logger.warning(
                "ProtocolConfig: on-chain validator SET changed "
                "(added=%d removed=%d total=%d)",
                len(after - before), len(before - after), len(new_addrs),
            )
        self.on_chain_validators = list(new_addrs)

        # Both registry reads landed — stamp the observability snapshot.
        # block height is best-effort (its failure mustn't void an otherwise
        # successful refresh); the timestamp + cleared error are the signal
        # the health workflow keys off to tell a live view from a frozen one.
        try:
            self.last_refresh_block = _read_block_number(self.rpc_url)
        except Exception as exc:
            logger.debug("ProtocolConfig: eth_blockNumber read failed: %s", exc)
        self.last_successful_refresh_at = time.time()
        self.last_refresh_error = None

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

        # ── Eviction hysteresis ──────────────────────────────────────────
        # Additions apply immediately; a previously-verified peer that
        # failed THIS probe round is retained until it misses
        # ``peer_eviction_misses`` consecutive rounds. De-authorization on
        # chain (dropped from getValidators()) evicts immediately — the
        # registry is authoritative and doesn't depend on reachability.
        # Retention is safe: the peer list is a routing/liveness hint, not
        # an auth boundary — approvals are signature-verified against
        # ``on_chain_validators`` when they arrive, and a genuinely-down
        # peer just costs one failed HTTP send.
        authorized_lower = {a.lower() for a in authorized}
        new_by_addr = {p.evm_address.lower(): p for p in new_peers}
        retained: list[PeerInfo] = []
        for old in self.peers:
            key = old.evm_address.lower()
            if key in new_by_addr:
                continue
            streak = self._peer_missing_streaks.get(key, 0) + 1
            self._peer_missing_streaks[key] = streak
            if key not in authorized_lower:
                continue
            if streak < self.peer_eviction_misses:
                retained.append(old)
        if retained:
            logger.warning(
                "ProtocolConfig: retaining %d peer(s) that failed this "
                "probe round (miss streaks %s, evict at %d) — probes lie "
                "under local load",
                len(retained),
                {
                    p.evm_address[:10]: self._peer_missing_streaks[p.evm_address.lower()]
                    for p in retained
                },
                self.peer_eviction_misses,
            )
        final = new_peers + retained
        # Streak bookkeeping: probes that succeeded reset their streak;
        # evicted peers' entries are pruned so a later re-appearance
        # starts clean.
        final_keys = {p.evm_address.lower() for p in final}
        self._peer_missing_streaks = {
            k: v for k, v in self._peer_missing_streaks.items()
            if k in final_keys and k not in new_by_addr
        }

        # Mutate in place — consumers hold a reference to self.peers.
        before = {p.evm_address.lower() for p in self.peers}
        after = final_keys
        if before != after:
            added = after - before
            removed = before - after
            logger.warning(
                "ProtocolConfig: peer set changed (added=%d removed=%d "
                "total=%d)",
                len(added), len(removed), len(final),
            )
        self.peers[:] = final


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
    w3 = build_retrying_web3(rpc_url)
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_VALIDATOR_REGISTRY_ABI,
    )
    return int(registry.functions.quorumBps().call())


def _read_validator_count(rpc_url: str, registry_address: str) -> int:
    """Read the canonical validator count from the on-chain registry.

    Used as the quorum denominator (see
    ``ProtocolConfig.on_chain_validator_count`` and
    ``ConsensusManager.quorum_required``). Reads ``getValidatorCount()``
    — the same accessor the on-chain ``AppIntentBase`` uses when
    verifying a quorum bundle, so off-chain and on-chain agree byte-
    for-byte on how many signatures are required.
    """
    w3 = build_retrying_web3(rpc_url)
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_VALIDATOR_REGISTRY_ABI,
    )
    return int(registry.functions.getValidatorCount().call())


def _read_validators(rpc_url: str, registry_address: str) -> list[str]:
    w3 = build_retrying_web3(rpc_url)
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_VALIDATOR_REGISTRY_ABI,
    )
    return [str(a) for a in registry.functions.getValidators().call()]


def read_champion_last_nonce(
    rpc_url: str, champion_registry_address: str, signer: str
) -> int:
    """Read ``ChampionRegistry.lastNonce(signer)`` — the per-signer monotonic
    high-water the contract enforces with
    ``require(nonces[i] > lastNonce[signer], "Nonce not increasing")``.

    Used to FLOOR a freshly-minted champion proposal nonce so a backward
    wall-clock movement on the proposing leader can never mint a nonce <= the
    on-chain high-water (which would brick certification). ``champion_registry_
    address`` is the ChampionRegistry (``ProtocolConfig.quorum_address``), NOT
    the ValidatorRegistry — they are distinct contracts on BT EVM.

    BOUNDED TIMEOUT: this is called inline from the synchronous champion-proposal
    builder on the API event loop. A bounded request timeout guarantees a hung
    BT-EVM RPC degrades to a catchable error (→ the floor's fail-open → bare
    wall-clock nonce) in a few seconds instead of stalling the whole event loop
    until the socket's default timeout.
    """
    w3 = build_retrying_web3(
        rpc_url, request_kwargs={"timeout": _NONCE_READ_TIMEOUT_SECONDS},
    )
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(champion_registry_address),
        abi=_CHAMPION_REGISTRY_ABI,
    )
    return int(
        registry.functions.lastNonce(Web3.to_checksum_address(signer)).call()
    )


def _read_block_number(rpc_url: str) -> int:
    """Latest block height seen by ``rpc_url`` (``eth_blockNumber``).

    Observability only — a height that lags the chain head is the
    signature of an out-of-sync RPC node, which can serve stale contract
    reads (and thus a stale validator count/set) while still answering.
    """
    w3 = build_retrying_web3(rpc_url)
    return int(w3.eth.block_number)


def _read_chain_id(rpc_url: str) -> int:
    """``eth_chainId`` for ``rpc_url`` — surfaced so operators can spot a
    node pointed at the wrong chain's registry."""
    w3 = build_retrying_web3(rpc_url)
    return int(w3.eth.chain_id)
