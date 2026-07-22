"""Actor resolution: collapse a miner's hotkeys to one queue identity.

WHY. The 2026-07-22 audit found the 256 registered SN112 hotkeys belong to 64
on-chain coldkeys (~25 real actors once code lineage is merged): multi-UID
operators rotate which hotkey submits each round, so under per-hotkey LRU every
fleet submission arrives on a maximally-senior hotkey and N hotkeys buy ~N
times one miner's share of build units, lottery tickets and bench-slate seats
(~90% of last-7d seats went to multi-UID actors). Rejecting their submissions
does not help — a rejected submission costs no seniority — the queue itself
must stop counting identities the actor can mint for free.

WHAT. :func:`snapshot_resolver` returns a FROZEN hotkey→actor view (actor =
on-chain coldkey) for one selection pass or one round, or ``None`` when no
coldkey attribution is available — and ``None`` means every consumer runs the
UNCHANGED legacy per-hotkey path. That None-contract is load-bearing twice
over: the ``SOLVER_ACTOR_KEY=hotkey`` kill-switch restores byte-identical
legacy behaviour (no half-enabled dedup), and a degraded map (api booting
before its first metagraph sync) degrades to exactly yesterday's queue instead
of a half-actor'd one. Freezing matters too: the build gate snapshots one
resolver per round, so ``_is_proven``/``charged_actors`` can never disagree
with the pool ordering because a metagraph re-sync landed mid-round.

The rotation ledger keeps its ``{hotkey: ts}`` schema and aggregation happens
at READ time (:func:`actor_last_selected` — MAX over the actor's hotkeys), so
the change is instantly revertible.

MAP SOURCES, in order: the in-process provider (api: lazy view over the
metagraph sync, installed at startup) — which also persists an atomic sidecar
JSON next to the rotation ledger — then that sidecar, which is how the
benchmark worker's slate-width belt (a separate process sharing /data) applies
the SAME slate rule as close-time rotation instead of silently recomputing a
per-hotkey one.

SCOPE. Leader-local admission control, the same category as the intake caps
and the rotation ledger: no wire-format, store-schema or consensus change;
followers (who take no submissions) never consult it. Known evasion, stated
honestly: coldkeys are free to mint, so a fleet can split — but each split
coldkey re-enters as a single never-benched actor (one lottery ticket, no
proven-pool seat), which is exactly the newcomer cost an honest solo pays.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

# In-process {hotkey: coldkey} source (api). Swapped atomically; read per
# snapshot, not per resolution.
_coldkey_provider: Callable[[], dict[str, str]] | None = None

# Sidecar persistence throttle (provider side) and read cache (worker side).
_SIDECAR_FILENAME = "actor_coldkeys.json"
_SIDECAR_MIN_WRITE_INTERVAL = 300.0
_persist_state: dict[str, Any] = {"ts": 0.0, "digest": ""}
_read_cache: dict[str, Any] = {"mtime": None, "map": {}}


def actor_key_mode() -> str:
    """``coldkey`` (default) or ``hotkey`` (kill-switch: legacy behaviour).

    Default lives IN CODE so a leader failover keeps the guard (same lesson
    as SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT).
    """
    raw = os.environ.get("SOLVER_ACTOR_KEY", "coldkey").strip().lower()
    return raw if raw in ("coldkey", "hotkey") else "coldkey"


def set_coldkey_provider(provider: Callable[[], dict[str, str]] | None) -> None:
    """Install the {hotkey: coldkey} source (api startup; tests)."""
    global _coldkey_provider
    _coldkey_provider = provider


def _reset_caches_for_tests() -> None:
    _persist_state.update(ts=0.0, digest="")
    _read_cache.update(mtime=None, map={})


class ActorResolver:
    """A FROZEN hotkey→actor view for one selection pass / round.

    ``resolver(hotkey)`` returns the actor (coldkey when mapped, else the
    hotkey itself). :meth:`mapped` returns the coldkey or ``None`` — consumers
    that must not act on guesses (the cross-actor copy reject) use it to treat
    unmapped hotkeys as INDETERMINATE rather than distinct.
    """

    __slots__ = ("_map", "source")

    def __init__(self, mapping: dict[str, str], source: str) -> None:
        self._map = mapping
        self.source = source

    def __call__(self, hotkey: str) -> str:
        hotkey = hotkey or ""
        return self._map.get(hotkey, "") or hotkey

    def mapped(self, hotkey: str) -> str | None:
        return self._map.get(hotkey or "") or None


def _sidecar_path() -> str:
    # Next to the rotation ledger: the one path both the api and the
    # benchmark worker already share (same /data volume, same env).
    from minotaur_subnet.harness.rotation import rotation_ledger_path

    return os.path.join(
        os.path.dirname(rotation_ledger_path()) or ".", _SIDECAR_FILENAME,
    )


def _persist_sidecar(mapping: dict[str, str]) -> None:
    """Best-effort, throttled, atomic write of the map for other processes."""
    now = time.time()
    digest = hashlib.sha256(
        json.dumps(sorted(mapping.items())).encode(),
    ).hexdigest()
    if (
        digest == _persist_state["digest"]
        and now - _persist_state["ts"] < _SIDECAR_MIN_WRITE_INTERVAL
    ):
        return
    path = _sidecar_path()
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".actor-map-",
        )
        with os.fdopen(fd, "w") as f:
            json.dump(mapping, f)
        os.replace(tmp, path)
        tmp = None
        _persist_state.update(ts=now, digest=digest)
    except Exception:
        logger.warning("actor-map sidecar write failed (%s)", path, exc_info=True)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_sidecar() -> dict[str, str]:
    """The persisted map (mtime-cached), or {} when absent/unreadable."""
    path = _sidecar_path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    if _read_cache["mtime"] == mtime:
        return _read_cache["map"]
    try:
        with open(path) as f:
            raw = json.load(f)
        mapping = {
            str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v
        } if isinstance(raw, dict) else {}
    except Exception:
        logger.warning("actor-map sidecar unreadable (%s)", path, exc_info=True)
        return {}
    _read_cache.update(mtime=mtime, map=mapping)
    return mapping


def snapshot_resolver() -> ActorResolver | None:
    """The actor view for one pass — or ``None``, meaning: run the legacy
    per-hotkey path, unchanged.

    ``None`` when the kill-switch is on OR no coldkey data exists yet (no
    provider and no sidecar). Never raises.
    """
    if actor_key_mode() != "coldkey":
        return None
    if _coldkey_provider is not None:
        try:
            mapping = dict(_coldkey_provider())
        except Exception:
            logger.warning("coldkey provider failed — falling back to sidecar",
                           exc_info=True)
            mapping = {}
        if mapping:
            _persist_sidecar(mapping)
            return ActorResolver(mapping, source="metagraph")
    mapping = _read_sidecar()
    if mapping:
        return ActorResolver(mapping, source="sidecar")
    return None


def resolve_actor(hotkey: str) -> str:
    """One-off resolution (logging/tests). Selection passes must snapshot."""
    resolver = snapshot_resolver()
    return resolver(hotkey) if resolver is not None else (hotkey or "")


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
