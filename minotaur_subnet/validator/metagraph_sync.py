"""MetagraphSync — periodic metagraph polling and deterministic leader election.

Provides a uniform view of the validator peer set and deterministic leader
election. Used by the validator to determine its role (leader vs follower)
and discover peer endpoints.

Leader election rule (current): the hotkey listed in ``LOCKED_LEADER_HOTKEY``
is the only validator allowed to take leadership. Stake ordering is ignored.
If that hotkey isn't present in the metagraph, ``elect_leader`` returns
``None`` and the network has no proposer — order/champion consensus halts
until the locked validator comes back online. This is intentional: it
removes the risk of a higher-stake but misconfigured (or malicious) peer
being auto-promoted to leader.

To unlock (return to stake-based election), set ``LOCKED_LEADER_HOTKEY = ""``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak

logger = logging.getLogger(__name__)


# Locked leader identity. Both fields must match the same operator:
# - ``LOCKED_LEADER_HOTKEY`` is the SS58 of the subnet 112 validator hotkey
#   that we permit to act as consensus leader.
# - ``LOCKED_LEADER_EVM_ADDRESS`` is the EVM address that signs that
#   validator's EIP-712 consensus payloads (i.e. the address derived from
#   ``VALIDATOR_PRIVATE_KEY``). Followers refuse proposals whose
#   ``proposer_signature`` recovers to anything else.
#
# Empty string disables the lock and restores stake-based election +
# any-validator proposer acceptance. Treat both fields as a pair — changing
# only one breaks consensus (election would point one way, sig-check the
# other). Both values are public information already published in the
# on-chain ValidatorRegistry / metagraph.
LOCKED_LEADER_HOTKEY = os.environ.get("LOCKED_LEADER_HOTKEY", "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt")
LOCKED_LEADER_EVM_ADDRESS = os.environ.get("LOCKED_LEADER_EVM_ADDRESS", "0x3f1649704bAcf67EEeD4B373F761dFAdd9df504D")

if not LOCKED_LEADER_EVM_ADDRESS:
    logger.warning(
        "LOCKED_LEADER_EVM_ADDRESS is empty — leader lock CLEARED "
        "(stake-based election + any-validator proposer acceptance)."
    )


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
    # Block at which OUR hotkey last set weights on the subnet (0 if never).
    # Used by the validator daemon at startup to backdate its local
    # ChampionWeights epoch clock so a stale validator emits on the FIRST
    # epoch tick after restart instead of waiting another full epoch_seconds.
    # None when my_uid is None (unregistered).
    my_last_update_block: int | None = None


def _hotkey_to_evm(hotkey_ss58: str) -> str:
    """Derive an EVM address from a hotkey SS58 string.

    Uses keccak256(hotkey_bytes)[-20:] — same derivation as
    validator_sync.py:116 and validator_cluster.py.
    """
    return "0x" + keccak(hotkey_ss58.encode())[-20:].hex()


def elect_leader(peers: list[PeerInfo]) -> PeerInfo | None:
    """Deterministic leader election.

    When ``LOCKED_LEADER_HOTKEY`` is set (the default), only the peer whose
    hotkey matches it is eligible to be leader, regardless of stake. Returns
    ``None`` if that peer isn't in the metagraph — in which case every
    validator's ``my_role`` resolves to ``follower``/``unregistered`` and no
    one proposes. The lock is intentional: a misconfigured high-stake peer
    cannot disrupt service by auto-winning the election.

    When ``LOCKED_LEADER_HOTKEY`` is empty (unlock), falls back to the
    original stake-based election: highest stake wins, ties broken by
    hotkey ascending.

    Args:
        peers: List of all peers (any stake level).

    Returns:
        The elected leader, or None if no eligible peer exists.
    """
    if LOCKED_LEADER_HOTKEY:
        for peer in peers:
            if peer.hotkey == LOCKED_LEADER_HOTKEY:
                return peer
        return None
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

    def resolve_subnet_owner(self) -> str:
        """Chain-primary subnet-owner hotkey (env fallback), reusing this sync's
        subtensor connection."""
        from minotaur_subnet.weight_policy import resolve_subnet_owner_hotkey
        try:
            subtensor = self._get_subtensor()
        except Exception:
            subtensor = None
        return resolve_subnet_owner_hotkey(subtensor, self.netuid)

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

        # Our last-update block (when we last successfully set_weights on
        # this subnet). 0 when we've never emitted. None when unregistered.
        my_last_update_block: int | None = None
        if my_uid is not None:
            try:
                my_last_update_block = int(metagraph.last_update[my_uid])
            except (IndexError, AttributeError, ValueError):
                my_last_update_block = None

        return MetagraphState(
            block=block,
            peers=peers,
            validators=validators,
            leader=leader,
            my_uid=my_uid,
            my_role=my_role,
            epoch=epoch_info,
            my_last_update_block=my_last_update_block,
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
