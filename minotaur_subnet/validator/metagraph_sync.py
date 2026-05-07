"""MetagraphSync — periodic metagraph polling and deterministic leader election.

Provides a uniform view of the validator peer set and deterministic leader
election based on stake. Used by the validator to determine its role
(leader vs follower) and discover peer endpoints.

Leader election rule: highest stake wins, ties broken by hotkey
(lexicographic ascending). Matches the pattern in
tests/emulation/fixtures/validator_cluster.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak

logger = logging.getLogger(__name__)


def _extract_int(value: Any) -> int | None:
    """Best-effort integer extraction from SDK/query wrapper objects."""
    current = value
    for _ in range(4):
        if current is None:
            return None
        if isinstance(current, bool):
            return int(current)
        if isinstance(current, int):
            return int(current)
        if hasattr(current, "value"):
            current = current.value
            continue
        item = getattr(current, "item", None)
        if callable(item):
            try:
                current = item()
                continue
            except Exception:
                return None
        break
    try:
        return int(current)
    except Exception:
        return None


def _lookup_field(raw: Any, *names: str) -> Any:
    """Read a field from a dict-like or attribute-based SDK object."""
    for name in names:
        if isinstance(raw, dict) and name in raw:
            return raw[name]
        if hasattr(raw, name):
            return getattr(raw, name)
    return None


def _call_with_variants(fn: Any, variants: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
    """Try multiple call signatures against a SDK method."""
    for args, kwargs in variants:
        try:
            return fn(*args, **kwargs)
        except TypeError:
            continue
        except Exception:
            logger.debug("Subtensor helper call failed", exc_info=True)
            continue
    return None


@dataclass(frozen=True)
class SubnetEpochInfo:
    """Native subnet epoch state derived from on-chain tempo and step progress."""

    tempo_blocks: int
    epoch_length_blocks: int
    blocks_since_last_step: int
    last_step_block: int
    epoch_index: int


def _build_subnet_epoch_info(
    *,
    block: int,
    tempo_blocks: int | None,
    blocks_since_last_step: int | None = None,
    last_step_block: int | None = None,
) -> SubnetEpochInfo | None:
    """Build exact subnet epoch metadata from chain timing fields."""
    tempo = _extract_int(tempo_blocks)
    if tempo is None or tempo <= 0:
        return None

    step_block = _extract_int(last_step_block)
    since = _extract_int(blocks_since_last_step)
    if step_block is None:
        if since is None:
            return None
        step_block = max(0, int(block) - int(since))
    if since is None:
        since = max(0, int(block) - int(step_block))

    # Bittensor subnet steps advance every ``tempo + 1`` blocks, not ``tempo``.
    epoch_length_blocks = int(tempo) + 1
    epoch_index = max(0, int(step_block) // epoch_length_blocks)
    return SubnetEpochInfo(
        tempo_blocks=int(tempo),
        epoch_length_blocks=epoch_length_blocks,
        blocks_since_last_step=max(0, int(since)),
        last_step_block=max(0, int(step_block)),
        epoch_index=epoch_index,
    )


def _resolve_subnet_epoch_info(
    subtensor: Any,
    *,
    netuid: int,
    block: int,
) -> SubnetEpochInfo | None:
    """Resolve exact subnet epoch timing from chain state when available."""
    tempo_blocks: int | None = None
    blocks_since_last_step: int | None = None
    last_step_block: int | None = None

    query_subtensor = getattr(subtensor, "query_subtensor", None)
    if callable(query_subtensor):
        if tempo_blocks is None:
            tempo_blocks = _extract_int(_call_with_variants(
                query_subtensor,
                [
                    ((), {"name": "Tempo", "block": block, "params": [netuid]}),
                    (("Tempo",), {"block": block, "params": [netuid]}),
                    (("Tempo",), {"params": [netuid]}),
                ],
            ))
        if blocks_since_last_step is None:
            blocks_since_last_step = _extract_int(_call_with_variants(
                query_subtensor,
                [
                    ((), {"name": "BlocksSinceLastStep", "block": block, "params": [netuid]}),
                    (("BlocksSinceLastStep",), {"block": block, "params": [netuid]}),
                    (("BlocksSinceLastStep",), {"params": [netuid]}),
                ],
            ))

    blocks_since_fn = getattr(subtensor, "blocks_since_last_step", None)
    if blocks_since_last_step is None and callable(blocks_since_fn):
        blocks_since_last_step = _extract_int(_call_with_variants(
            blocks_since_fn,
            [
                ((netuid,), {"block": block}),
                ((netuid,), {}),
            ],
        ))

    hyperparams_fn = getattr(subtensor, "get_subnet_hyperparameters", None)
    if tempo_blocks is None and callable(hyperparams_fn):
        params = _call_with_variants(
            hyperparams_fn,
            [
                ((netuid,), {}),
                ((), {"netuid": netuid}),
                ((netuid,), {"block": block}),
                ((), {"netuid": netuid, "block": block}),
            ],
        )
        tempo_blocks = _extract_int(_lookup_field(params, "tempo"))

    for getter_name in ("get_subnet_info", "get_all_subnets_info", "all_subnets"):
        getter = getattr(subtensor, getter_name, None)
        if not callable(getter):
            continue
        result = _call_with_variants(
            getter,
            [
                ((netuid,), {}),
                ((), {"netuid": netuid}),
                ((netuid,), {"block": block}),
                ((), {"netuid": netuid, "block": block}),
                ((), {"block": block}),
                ((), {}),
            ],
        )
        if result is None:
            continue

        entries = result if isinstance(result, list) else [result]
        for entry in entries:
            entry_netuid = _extract_int(_lookup_field(entry, "netuid"))
            if entry_netuid is not None and entry_netuid != netuid:
                continue
            tempo_blocks = tempo_blocks or _extract_int(_lookup_field(entry, "tempo"))
            blocks_since_last_step = (
                blocks_since_last_step
                or _extract_int(_lookup_field(entry, "blocks_since_epoch", "blocks_since_last_step"))
            )
            last_step_block = last_step_block or _extract_int(_lookup_field(entry, "last_step"))
            if tempo_blocks is not None and (blocks_since_last_step is not None or last_step_block is not None):
                break
        if tempo_blocks is not None and (blocks_since_last_step is not None or last_step_block is not None):
            break

    return _build_subnet_epoch_info(
        block=block,
        tempo_blocks=tempo_blocks,
        blocks_since_last_step=blocks_since_last_step,
        last_step_block=last_step_block,
    )


@dataclass
class PeerInfo:
    """Information about a single peer validator."""

    uid: int
    hotkey: str  # SS58 address
    stake: float  # TAO
    evm_address: str  # keccak256(hotkey_bytes)[-20:]
    axon_url: str = ""  # http://ip:port from axon_info


@dataclass(frozen=True)
class MetagraphState:
    """Snapshot of the metagraph at a specific block."""

    block: int
    peers: list[PeerInfo]
    validators: list[PeerInfo]  # stake > 0, sorted by (-stake, hotkey)
    leader: PeerInfo | None
    my_uid: int | None
    my_role: str  # "leader" | "follower" | "unregistered"
    epoch: SubnetEpochInfo | None = None
    timestamp: float = field(default_factory=time.time)


def _hotkey_to_evm(hotkey_ss58: str) -> str:
    """Derive an EVM address from a hotkey SS58 string.

    Uses keccak256(hotkey_bytes)[-20:] — same derivation as
    validator_sync.py:116 and validator_cluster.py.
    """
    return "0x" + keccak(hotkey_ss58.encode())[-20:].hex()


def elect_leader(peers: list[PeerInfo]) -> PeerInfo | None:
    """Deterministic leader election: highest stake, ties broken by hotkey ascending.

    Args:
        peers: List of all peers (any stake level).

    Returns:
        The elected leader, or None if no staked peers exist.
    """
    staked = [p for p in peers if p.stake > 0]
    if not staked:
        return None
    return min(staked, key=lambda p: (-p.stake, p.hotkey))


class MetagraphSync:
    """Periodic metagraph polling and leader election.

    Connects to a subtensor node and periodically queries the metagraph
    to maintain an up-to-date view of the validator set. Fires the
    ``leader_changed`` event when the leader changes.

    Args:
        subtensor_url: WebSocket URL for subtensor (e.g. ws://localhost:9944).
        netuid: Network UID for the subnet.
        my_hotkey: This validator's hotkey SS58 address.
        poll_interval: Seconds between metagraph polls.
    """

    def __init__(
        self,
        subtensor_url: str,
        netuid: int,
        my_hotkey: str,
        poll_interval: float = 60.0,
        max_backoff: float = 300.0,
    ) -> None:
        self.subtensor_url = subtensor_url
        self.netuid = netuid
        self.my_hotkey = my_hotkey
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff

        self._state: MetagraphState | None = None
        self.leader_changed = asyncio.Event()
        self._subtensor: Any = None
        self._consecutive_failures: int = 0
        self._last_successful_sync: float = 0.0

    @property
    def state(self) -> MetagraphState | None:
        """Current metagraph state (None if never synced)."""
        return self._state

    @property
    def is_leader(self) -> bool:
        """Whether this validator is the current leader."""
        return self._state is not None and self._state.my_role == "leader"

    def _get_subtensor(self, force_reconnect: bool = False) -> Any:
        """Lazily create or reconnect a Subtensor client."""
        if self._subtensor is None or force_reconnect:
            if self._subtensor is not None:
                logger.info("Reconnecting to subtensor at %s", self.subtensor_url)
            import bittensor as bt
            self._subtensor = bt.Subtensor(network=self.subtensor_url)
        return self._subtensor

    @property
    def staleness_seconds(self) -> float:
        """Seconds since the last successful sync."""
        if self._last_successful_sync <= 0:
            return float("inf")
        return time.time() - self._last_successful_sync

    @property
    def is_stale(self) -> bool:
        """True if the state is significantly older than the poll interval."""
        return self.staleness_seconds > self.poll_interval * 3

    async def sync_once(self) -> MetagraphState:
        """Query the metagraph and update the local state.

        Returns the new MetagraphState. Falls back to last known state on
        error but tracks failure count for backoff and reconnection.
        """
        try:
            # Force reconnect after multiple consecutive failures
            force_reconnect = self._consecutive_failures >= 3
            if force_reconnect:
                logger.warning(
                    "Forcing subtensor reconnect after %d failures",
                    self._consecutive_failures,
                )

            state = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._sync_blocking(force_reconnect=force_reconnect),
            )
            self._consecutive_failures = 0
            self._last_successful_sync = time.time()
        except Exception as exc:
            self._consecutive_failures += 1
            staleness = self.staleness_seconds
            logger.error(
                "Metagraph sync failed (attempt %d, stale %.0fs): %s",
                self._consecutive_failures, staleness, exc,
            )
            if self._state is not None:
                if self.is_stale:
                    logger.warning(
                        "Metagraph state is stale (%.0fs) — leader/role may be incorrect",
                        staleness,
                    )
                return self._state
            raise

        old_leader = self._state.leader if self._state else None
        self._state = state

        # Detect leader change
        new_leader = state.leader
        if old_leader is None and new_leader is not None:
            self.leader_changed.set()
        elif old_leader is not None and new_leader is not None:
            if old_leader.hotkey != new_leader.hotkey:
                logger.info(
                    "Leader changed: %s -> %s",
                    old_leader.hotkey[:16], new_leader.hotkey[:16],
                )
                self.leader_changed.set()

        return state

    def _sync_blocking(self, force_reconnect: bool = False) -> MetagraphState:
        """Blocking metagraph query (runs in executor)."""
        sub = self._get_subtensor(force_reconnect=force_reconnect)
        block = sub.block
        metagraph = sub.metagraph(netuid=self.netuid)
        epoch_info = _resolve_subnet_epoch_info(sub, netuid=self.netuid, block=block)
        peers: list[PeerInfo] = []
        my_uid: int | None = None

        for uid in range(metagraph.n.item()):
            hotkey = metagraph.hotkeys[uid]
            stake = float(metagraph.S[uid].item())

            # Derive EVM address from hotkey
            evm_address = _hotkey_to_evm(hotkey)

            # Build axon URL if available
            axon_url = ""
            axon = metagraph.axons[uid]
            if hasattr(axon, "ip") and axon.ip and axon.ip != "0.0.0.0":
                port = getattr(axon, "port", 0)
                axon_url = f"http://{axon.ip}:{port}"

            peer = PeerInfo(
                uid=uid,
                hotkey=hotkey,
                stake=stake,
                evm_address=evm_address,
                axon_url=axon_url,
            )
            peers.append(peer)

            if hotkey == self.my_hotkey:
                my_uid = uid

        # Sort validators by (-stake, hotkey) — staked peers only
        validators = sorted(
            [p for p in peers if p.stake > 0],
            key=lambda p: (-p.stake, p.hotkey),
        )

        leader = elect_leader(peers)

        # Determine my role
        if my_uid is None:
            my_role = "unregistered"
        elif leader is not None and leader.hotkey == self.my_hotkey:
            my_role = "leader"
        else:
            my_role = "follower"

        return MetagraphState(
            block=block,
            peers=peers,
            validators=validators,
            leader=leader,
            my_uid=my_uid,
            my_role=my_role,
            epoch=epoch_info,
        )

    async def sync_loop(self) -> None:
        """Run the sync loop forever with exponential backoff on failure.

        On success: wait ``poll_interval`` seconds.
        On failure: wait ``min(poll_interval * 2^failures, max_backoff)``
        with jitter, up to ``max_backoff`` seconds.
        """
        import random

        logger.info(
            "MetagraphSync started (url=%s, netuid=%d, interval=%.0fs)",
            self.subtensor_url, self.netuid, self.poll_interval,
        )
        while True:
            try:
                state = await self.sync_once()
                logger.debug(
                    "Metagraph synced: block=%d, validators=%d, role=%s",
                    state.block, len(state.validators), state.my_role,
                )
                await asyncio.sleep(self.poll_interval)
            except Exception as exc:
                logger.error("Metagraph sync error: %s", exc)
                # Exponential backoff with jitter
                backoff = min(
                    self.poll_interval * (2 ** self._consecutive_failures),
                    self.max_backoff,
                )
                jitter = random.uniform(0.5, 1.5)
                delay = backoff * jitter
                logger.info(
                    "Backing off %.1fs before next sync attempt (failures=%d)",
                    delay, self._consecutive_failures,
                )
                await asyncio.sleep(delay)
