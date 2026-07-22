"""Actor resolution: collapse a miner's hotkeys to one queue identity.

WHY. The 2026-07-22 audit found the 256 registered SN112 hotkeys belong to 64
on-chain coldkeys (~25 real actors once code lineage is merged): multi-UID
operators rotate which hotkey submits each round, so under per-hotkey LRU every
fleet submission arrives on a maximally-senior hotkey and N hotkeys buy ~N
times one miner's share of build units, lottery tickets and bench-slate seats
(~90% of last-7d seats went to multi-UID actors). Rejecting their submissions
does not help — a rejected submission costs no seniority — the queue itself
must stop counting identities the actor can mint for free.

WHAT. ``resolve_actor(hotkey)`` maps a hotkey to its queue identity: the
on-chain COLDKEY when the metagraph map has it, else the hotkey itself
(graceful degradation to today's per-hotkey behaviour — an unknown hotkey is
its own actor, never someone else's). Rotation slate selection and the build
budget key seniority, pool membership and lottery tickets on this identity;
the rotation ledger keeps its ``{hotkey: ts}`` schema and aggregation happens
at READ time, so the change is instantly revertible.

SCOPE. Leader-local admission control, the same category as the intake caps
and the rotation ledger: no wire-format, store-schema or consensus change;
followers (who take no submissions) never consult it.

KILL-SWITCH. ``SOLVER_ACTOR_KEY=hotkey`` restores per-hotkey behaviour without
a redeploy. Default is ``coldkey`` IN CODE so a leader failover keeps the
guard (same lesson as SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT).

The coldkey map comes from the api's metagraph sync via a provider callable
(set at startup), read lazily on every resolution so a metagraph re-sync is
picked up without any event plumbing. Known evasion, stated honestly: coldkeys
are free to mint, so a fleet can split — but each split coldkey re-enters as a
single never-benched actor (one lottery ticket, no proven-pool seat), which is
exactly the newcomer cost an honest solo miner pays.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Provider returning the current {hotkey: coldkey} view (may be empty before
# the first metagraph sync). Swapped atomically; read lazily per resolution.
_coldkey_provider: Callable[[], dict[str, str]] | None = None


def actor_key_mode() -> str:
    """``coldkey`` (default) or ``hotkey`` (kill-switch: legacy behaviour)."""
    raw = os.environ.get("SOLVER_ACTOR_KEY", "coldkey").strip().lower()
    return raw if raw in ("coldkey", "hotkey") else "coldkey"


def set_coldkey_provider(provider: Callable[[], dict[str, str]] | None) -> None:
    """Install the {hotkey: coldkey} source (api startup; tests)."""
    global _coldkey_provider
    _coldkey_provider = provider


def resolve_actor(hotkey: str) -> str:
    """The queue identity for ``hotkey``: its coldkey when known, else itself.

    Never raises — any provider failure degrades to per-hotkey identity for
    this call only (admission control must not take down the pipeline).
    """
    hotkey = hotkey or ""
    if actor_key_mode() != "coldkey" or _coldkey_provider is None:
        return hotkey
    try:
        coldkey = _coldkey_provider().get(hotkey, "")
    except Exception:
        logger.warning("coldkey provider failed — treating %s as its own actor",
                       hotkey[:12], exc_info=True)
        return hotkey
    return coldkey or hotkey


def get_actor_resolver() -> Callable[[str], str]:
    """The resolver to snapshot into a selection pass. Selection code takes
    the callable (not the module function) so tests inject fixtures without
    touching process state."""
    return resolve_actor


def actor_last_selected(
    last_selected: dict[str, float],
    actor_of: Callable[[str], str],
) -> dict[str, float]:
    """Aggregate a ``{hotkey: ts}`` ledger to ``{actor: max ts}``.

    MAX is the point: benching ANY of an actor's hotkeys makes the whole
    actor junior, so rotating to a fresh sibling hotkey no longer resets
    seniority. An actor with no benched hotkey is absent (=> 0.0 upstream).
    """
    out: dict[str, float] = {}
    for hk, ts in last_selected.items():
        actor = actor_of(hk) or hk
        if ts > out.get(actor, 0.0):
            out[actor] = ts
    return out


def distinct_actor_count(hotkeys: Iterable[Any], actor_of: Callable[[str], str]) -> int:
    """Observability helper: how many actors a set of hotkeys collapses to."""
    return len({actor_of(str(hk) or "") for hk in hotkeys})
