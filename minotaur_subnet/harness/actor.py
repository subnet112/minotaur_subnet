"""Actor resolution: collapse a miner's hotkeys to one queue identity.

WHY. The 2026-07-22 audit found the 256 registered SN112 hotkeys belong to 64
on-chain coldkeys (~25 real actors once code lineage is merged): multi-UID
operators rotate which hotkey submits each round, so under per-hotkey LRU every
fleet submission arrives on a maximally-senior hotkey and N hotkeys buy ~N
times one miner's share of build units, lottery tickets and bench-slate seats
(~90% of last-7d seats went to multi-UID actors). Rejecting their submissions
does not help — a rejected submission costs no seniority — the queue itself
must stop counting identities the actor can mint for free.

WHAT. :func:`snapshot_resolver` returns a FROZEN hotkey→actor view for one
selection pass / round, or ``None`` when no coldkey attribution is available —
``None`` means every consumer runs the UNCHANGED legacy per-hotkey path. The
actor is the connected component of a hotkey under TWO kinds of link:

  * on-chain COLDKEY (base identity, from the metagraph): a coldkey's hotkeys
    are one actor. Ships since #1030.
  * shared GITHUB OWNER (union, from the submission store): coldkeys an
    operator links by submitting under the same github owner collapse into one
    actor. This closes the coldkey-split evasion the 2026-07-23 red-team found
    live — one operator (SF-1) spread 15 coldkeys across 5 owners, each with a
    freshly-MUTATED fingerprint, so coldkey-only keying saw 15 actors and the
    same-fingerprint copy reject never matched. github_owner is derived from
    the PR's clone_url (routes.py) — an attacker cannot charge a victim's
    identity without controlling the repo — so the union is not poisonable.

  * OPERATOR MERGE (union, from evidence): identifiers proven to be one
    operator collapse into one actor even when they share neither a coldkey nor
    a github owner. Two sources — the operator-supplied ``SOLVER_ACTOR_MERGE_EXTRA``
    groups, and the structural CO-OCCURRENCE ledger below. This closes the shape
    the 2026-07-28 audit found live: a ring of 13 coldkeys, ONE hotkey each,
    each under its OWN throwaway github account, so both links above see 13
    singleton actors and every per-actor cap is satisfied 13 times in parallel.
    No identifier is named in code: a merge is only ever asserted by live
    evidence (the ledger) or by an explicit operator decision (the env var).

The union is over a graph of {coldkey, github-owner, hotkey} tokens: for each
submitted hotkey, union its coldkey token (or its own hotkey token if unmapped)
with each github owner it has used, then union every merge group. Transitivity
does the rest — every coldkey touching any owner in a fleet's owner set
collapses to one component.

STRUCTURAL CO-OCCURRENCE (the automatic merge). The ring above re-rolls its
structural fingerprint EVERY round, so no equality key on code can ever catch
it: what identifies it is the pattern, not the hash — the same actors appearing
together in a same-fingerprint cluster round after round (measured over 26 live
rounds: 79 actor pairs co-clustered in 25 of them, while exactly ONE pair in the
whole window co-occurred just once). :func:`record_structural_coclusters` (fed
by the close-time slate pass) accumulates per-pair round evidence in a sidecar,
and pairs seen together in ``STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS`` distinct rounds
merge. Keyed on COLDKEYS, which cost a registration burn each — so re-rolling
the code does not shake the merge off, and evidence older than the TTL decays so
an honest pair that once forked the same baseline does not stay merged forever.

None-CONTRACT (unchanged): ``None`` when the ``SOLVER_ACTOR_KEY=hotkey``
kill-switch is set OR no coldkey map exists yet (pre-first-sync). The owner
union only ENRICHES a coldkey-backed resolver; it never resurrects one from
nothing, so degradation is byte-identical to #1030. Kill-switch the union
alone with ``SOLVER_ACTOR_OWNER_UNION=0`` to fall back to coldkey-only.

COPY-REJECT ATTRIBUTION. :meth:`mapped` stays conservative: it returns an actor
ONLY for coldkey-backed hotkeys (owner-enriched), else ``None`` (indeterminate).
So the cross-actor copy reject still stands down on a genuinely unmapped hotkey
(the #1030 review's degraded-reject fix) while owner-linked coldkeys correctly
read as the same actor.

MAP SOURCES, in order: the in-process providers (api: coldkey view over the
metagraph sync, owner view over the submission store) — each persists an atomic
sidecar next to the rotation ledger — then those sidecars, which is how the
benchmark worker's slate-width belt (a separate process sharing /data) applies
the SAME actor rule as close-time rotation.

SCOPE. Leader-local admission control, the same category as the intake caps and
the rotation ledger: no wire-format, store-schema or consensus change. The
rotation ledger keeps its ``{hotkey: ts}`` schema; aggregation happens at READ
time (:func:`actor_last_selected`), so the change is instantly revertible. A
merge adds NO new cap and no new rejection path — it only supplies the missing
edge in the identity graph, after which the caps that already run do the work
(``SUBMISSIONS_MAX_PER_ACTOR_PER_ROUND=1`` rejects the ring's 2nd..Nth
submission each round at intake, one seniority clock, one build unit, one slate
seat). Because it changes the benched slate → the pack hash, arming the
structural merge MUST be promoted fleet-uniform.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# In-process providers (api). Swapped atomically; read per snapshot.
_coldkey_provider: Callable[[], dict[str, str]] | None = None
# Owner links: {hotkey: [github_owner, ...]} — every owner a hotkey has used.
_owner_provider: Callable[[], dict[str, list[str]]] | None = None

# Sidecar persistence throttle + read caches, per sidecar file.
_COLDKEY_SIDECAR = "actor_coldkeys.json"
_OWNER_SIDECAR = "actor_owners.json"
_SIDECAR_MIN_WRITE_INTERVAL = 300.0
_persist_state: dict[str, dict[str, Any]] = {}
_read_cache: dict[str, dict[str, Any]] = {}


def actor_key_mode() -> str:
    """``coldkey`` (default) or ``hotkey`` (kill-switch: legacy behaviour).

    Default lives IN CODE so a leader failover keeps the guard (same lesson
    as SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT).
    """
    raw = os.environ.get("SOLVER_ACTOR_KEY", "coldkey").strip().lower()
    return raw if raw in ("coldkey", "hotkey") else "coldkey"


def owner_union_enabled() -> bool:
    """Merge coldkeys that share a github owner (``SOLVER_ACTOR_OWNER_UNION``,
    default on; 0 disables → coldkey-only, i.e. #1030 behaviour)."""
    return os.environ.get(
        "SOLVER_ACTOR_OWNER_UNION", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


# ── operator merges: identifiers proven to be ONE operator ───────────────────

# NOTE (2026-08-17). There is deliberately NO hardcoded merge list here. The
# in-code list (the 2026-07-28 "fp-ring", narrowed to 9 coldkeys on 08-13) was
# removed: naming identifiers in code makes a one-time audit permanent, cannot
# decay as behaviour changes, and needs a deploy to correct. Merges now come
# only from live evidence (the co-occurrence ledger below, which decays on a
# TTL) or an explicit operator decision (``SOLVER_ACTOR_MERGE_EXTRA``).

# Per-pair structural co-occurrence evidence, next to the other sidecars.
_STRUCT_LINK_SIDECAR = "actor_structural_links.json"
# Cap the round-id history kept per pair: only the DISTINCT-round count matters
# and the threshold is single digits, so a short window bounds the file.
_STRUCT_LINK_MAX_ROUNDS = 32


def extra_merge_groups() -> list[frozenset[str]]:
    """Operator merges from ``SOLVER_ACTOR_MERGE_EXTRA`` — groups separated by
    ``;``, members by ``,`` (e.g. ``ckA,ckB;ckC,ckD``). Lets an operator add a
    freshly-caught ring without a deploy; single-member groups are ignored."""
    raw = os.environ.get("SOLVER_ACTOR_MERGE_EXTRA", "").strip()
    groups: list[frozenset[str]] = []
    for chunk in raw.split(";"):
        members = {m.strip() for m in chunk.split(",") if m.strip()}
        if len(members) >= 2:
            groups.append(frozenset(members))
    return groups


def structural_merge_mode() -> str:
    """How the structural CO-OCCURRENCE evidence acts —
    ``STRUCTURAL_ACTOR_MERGE_MODE``:

      * ``observe`` (DEFAULT) — record co-occurrence and LOG the merges it
        would make; the actor map is unchanged.
      * ``enforce`` — also apply them: co-occurring actors become one actor,
        so every per-actor cap, the seniority clock, the build budget and the
        slate see one operator. Changes the benched slate → the pack hash, so
        it MUST be promoted fleet-uniform.
      * ``off`` — record nothing, apply nothing.

    Unknown values fall back to ``observe`` (record + report, never silently
    change selection).
    """
    v = os.environ.get("STRUCTURAL_ACTOR_MERGE_MODE", "").strip().lower()
    return v if v in ("off", "observe", "enforce") else "observe"


def structural_merge_min_rounds() -> int:
    """Distinct rounds a pair must co-cluster before it merges —
    ``STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS``, default 3 in code.

    Honest miners forking a shared baseline collide occasionally; a coordinated
    ring collides every round (live: 79 pairs in 25 of 26 rounds, exactly one
    pair at a single round). 3 sits above the accidental band and below any
    ring's cadence. Values < 2 are clamped to 2 — a single co-occurrence is
    never enough.
    """
    raw = os.environ.get("STRUCTURAL_ACTOR_MERGE_MIN_ROUNDS", "3").strip() or "3"
    try:
        return max(2, int(raw))
    except ValueError:
        return 3


def structural_merge_ttl_seconds() -> float:
    """How long a pair's co-occurrence evidence survives without a repeat —
    ``STRUCTURAL_ACTOR_MERGE_TTL_DAYS``, default 30 days, ``0`` = never expire.

    Evidence decays so a merge is a statement about CURRENT behaviour: two
    miners who once forked the same baseline drift apart and un-merge, while a
    ring that keeps co-submitting keeps refreshing its own evidence.
    """
    raw = os.environ.get("STRUCTURAL_ACTOR_MERGE_TTL_DAYS", "30").strip() or "30"
    try:
        return max(0.0, float(raw)) * 86400.0
    except ValueError:
        return 30 * 86400.0


def set_coldkey_provider(provider: Callable[[], dict[str, str]] | None) -> None:
    """Install the {hotkey: coldkey} source (api startup; tests)."""
    global _coldkey_provider
    _coldkey_provider = provider


def set_owner_links_provider(
    provider: Callable[[], dict[str, list[str]]] | None,
) -> None:
    """Install the {hotkey: [github_owner, ...]} source (api startup; tests)."""
    global _owner_provider
    _owner_provider = provider


def _reset_caches_for_tests() -> None:
    _persist_state.clear()
    _read_cache.clear()


# ── generic sidecar plumbing (one file per map) ──────────────────────────────

def _sidecar_path(filename: str) -> str:
    # Next to the rotation ledger: the one path both the api and the benchmark
    # worker already share (same /data volume, same env).
    from minotaur_subnet.harness.rotation import rotation_ledger_path

    return os.path.join(
        os.path.dirname(rotation_ledger_path()) or ".", filename,
    )


def _persist_sidecar(filename: str, mapping: Mapping[str, Any]) -> None:
    """Best-effort, throttled, atomic write of a map for other processes."""
    st = _persist_state.setdefault(filename, {"ts": 0.0, "digest": ""})
    now = time.time()
    digest = hashlib.sha256(
        json.dumps({k: mapping[k] for k in sorted(mapping)}, sort_keys=True).encode(),
    ).hexdigest()
    if digest == st["digest"] and now - st["ts"] < _SIDECAR_MIN_WRITE_INTERVAL:
        return
    path = _sidecar_path(filename)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".actor-map-")
        with os.fdopen(fd, "w") as f:
            json.dump(dict(mapping), f)
        os.replace(tmp, path)
        tmp = None
        st.update(ts=now, digest=digest)
    except Exception:
        logger.warning("actor-map sidecar write failed (%s)", path, exc_info=True)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_sidecar(filename: str) -> dict[str, Any]:
    """The persisted map (mtime-cached), or {} when absent/unreadable."""
    path = _sidecar_path(filename)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    rc = _read_cache.setdefault(filename, {"mtime": None, "map": {}})
    if rc["mtime"] == mtime:
        return rc["map"]
    try:
        with open(path) as f:
            raw = json.load(f)
        mapping = raw if isinstance(raw, dict) else {}
    except Exception:
        logger.warning("actor-map sidecar unreadable (%s)", path, exc_info=True)
        return {}
    rc.update(mtime=mtime, map=mapping)
    return mapping


def _coldkey_map() -> dict[str, str]:
    """{hotkey: coldkey} — provider (persist sidecar) then sidecar."""
    if _coldkey_provider is not None:
        try:
            m = {str(k): str(v) for k, v in _coldkey_provider().items() if v}
        except Exception:
            logger.warning("coldkey provider failed — falling back to sidecar", exc_info=True)
            m = {}
        if m:
            _persist_sidecar(_COLDKEY_SIDECAR, m)
            return m
    raw = _read_sidecar(_COLDKEY_SIDECAR)
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def _owner_map() -> dict[str, list[str]]:
    """{hotkey: [github_owner, ...]} — provider (persist sidecar) then sidecar."""
    if _owner_provider is not None:
        try:
            m = {
                str(k): [str(o) for o in (v or []) if o]
                for k, v in _owner_provider().items()
            }
            m = {k: v for k, v in m.items() if v}
        except Exception:
            logger.warning("owner provider failed — falling back to sidecar", exc_info=True)
            m = {}
        if m:
            _persist_sidecar(_OWNER_SIDECAR, m)
            return m
    raw = _read_sidecar(_OWNER_SIDECAR)
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            owners = [str(o) for o in v if o]
        elif isinstance(v, str) and v:
            owners = [v]
        else:
            continue
        if owners:
            out[str(k)] = owners
    return out


# ── structural co-occurrence ledger (the automatic merge) ────────────────────

def _pair_key(a: str, b: str) -> str:
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}|{hi}"


def _load_struct_links() -> dict[str, dict[str, Any]]:
    """``{"<a>|<b>": {"rounds": [...], "last": ts}}`` — the raw evidence."""
    raw = _read_sidecar(_STRUCT_LINK_SIDECAR)
    pairs = raw.get("pairs") if isinstance(raw.get("pairs"), dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in (pairs or {}).items():
        if not isinstance(val, dict):
            continue
        rounds = val.get("rounds")
        if not isinstance(rounds, list):
            continue
        try:
            last = float(val.get("last") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        out[str(key)] = {"rounds": [str(r) for r in rounds if r], "last": last}
    return out


def base_identity(hotkey: str) -> str:
    """A hotkey's PRE-MERGE identity: its coldkey, or itself when unmapped.

    Evidence is keyed on this, never on the resolved actor: once a ring is
    merged its members all resolve to ONE actor, so an actor-keyed recorder
    would see no cross-actor cluster, stop refreshing the evidence, and let the
    merge expire on the TTL — a 30-day flap. The base identity keeps refreshing
    for as long as the ring keeps co-submitting.
    """
    hotkey = hotkey or ""
    return _coldkey_map().get(hotkey) or hotkey


def record_structural_coclusters(
    round_id: str,
    clusters: Iterable[Iterable[str]],
    now: float | None = None,
) -> list[frozenset[str]]:
    """Record that these HOTKEYS shipped structurally-identical code in one
    round; return the merge groups that are armed AFTER this record.

    One call per round (the close-time slate pass). Hotkeys are folded to their
    base identity (:func:`base_identity`) first, so a single operator's own
    sibling hotkeys are ONE member and never manufacture evidence against
    themselves. A pair is credited at most once per round — a ring cannot
    frame a victim by submitting the victim's structure many times in one
    round, only by doing it across ``structural_merge_min_rounds()`` DISTINCT
    rounds, which requires the victim to keep co-appearing. Evidence older than
    the TTL is pruned here, so the file stays bounded and a merge always
    reflects current behaviour.

    Best-effort and never raises: losing this ledger degrades to "no automatic
    merges", exactly the pre-feature behaviour.
    """
    if structural_merge_mode() == "off":
        return []
    now = time.time() if now is None else now
    try:
        links = _load_struct_links()
        ttl = structural_merge_ttl_seconds()
        for cluster in clusters:
            members = sorted({base_identity(str(hk)) for hk in cluster if hk})
            if len(members) < 2:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    entry = links.setdefault(_pair_key(a, b), {"rounds": [], "last": 0.0})
                    if round_id and round_id not in entry["rounds"]:
                        entry["rounds"].append(round_id)
                        del entry["rounds"][:-_STRUCT_LINK_MAX_ROUNDS]
                    entry["last"] = now
        if ttl > 0:
            links = {k: v for k, v in links.items() if now - float(v["last"] or 0.0) <= ttl}
        _persist_sidecar(_STRUCT_LINK_SIDECAR, {"version": 1, "pairs": links})
        return _groups_from_links(links)
    except Exception:
        logger.warning(
            "structural co-occurrence record failed for %s (no automatic "
            "merges this round)", round_id, exc_info=True,
        )
        return []


def _groups_from_links(links: Mapping[str, Mapping[str, Any]]) -> list[frozenset[str]]:
    """Connected components over pairs that cleared the round threshold."""
    threshold = structural_merge_min_rounds()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked = False
    for key, val in links.items():
        if len({str(r) for r in (val.get("rounds") or [])}) < threshold:
            continue
        a, _, b = key.partition("|")
        if not a or not b:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
            linked = True
    if not linked:
        return []
    comps: dict[str, set[str]] = {}
    for node in list(parent):
        comps.setdefault(find(node), set()).add(node)
    return [frozenset(m) for m in comps.values() if len(m) >= 2]


def structural_merge_groups() -> list[frozenset[str]]:
    """Armed automatic merges from the persisted evidence (mode-independent —
    callers decide whether to APPLY them; ``merge_groups`` applies only in
    ``enforce``, while ``observe`` uses this for reporting)."""
    try:
        return _groups_from_links(_load_struct_links())
    except Exception:
        logger.warning("structural link ledger unreadable (no merges)", exc_info=True)
        return []


def merge_groups() -> list[frozenset[str]]:
    """Every operator merge to APPLY: env extras + structural co-occurrence
    (the latter only in ``enforce``)."""
    groups: list[frozenset[str]] = []
    groups.extend(extra_merge_groups())
    if structural_merge_mode() == "enforce":
        groups.extend(structural_merge_groups())
    return groups


# ── union-find over coldkey ∪ github-owner ───────────────────────────────────

def _build_actor_map(
    coldkey_map: dict[str, str],
    owner_map: dict[str, list[str]],
) -> tuple[dict[str, str], set[str]]:
    """Return ``(hotkey → canonical actor, coldkey-backed hotkeys)``.

    Union-find over namespaced tokens — ``ck:<coldkey>`` (base identity of a
    mapped hotkey), ``hk:<hotkey>`` (base of an unmapped hotkey), and
    ``own:<github_owner>``. With the owner union on, each hotkey's base token is
    merged with every owner it used, so any coldkeys sharing an owner collapse
    into one component (transitively).

    The component's public actor id is a CLEAN, prefix-stripped label, choosing
    a coldkey over an owner over a hotkey (smallest within that tier). So a
    lone coldkey resolves to exactly its coldkey string (byte-identical to
    #1030 — coldkeys are SS58 and github owners can't be, so stripped labels
    never collide in production), while an owner-linked fleet resolves to its
    smallest member coldkey, deterministically.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:  # path-compress
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        parent[hi] = lo

    def base(hk: str) -> str:
        ck = coldkey_map.get(hk)
        return f"ck:{ck}" if ck else f"hk:{hk}"

    union_on = owner_union_enabled()
    hotkeys = set(coldkey_map)
    if union_on:
        hotkeys |= set(owner_map)

    tokens: set[str] = set()
    for hk in hotkeys:
        t = base(hk)
        find(t)  # register the token even with no owner edge
        tokens.add(t)
        if union_on:
            for owner in owner_map.get(hk, ()):  # link every owner this hk used
                ot = f"own:{owner}"
                tokens.add(ot)
                union(t, ot)

    # Operator merges: union EVERY token form of each member, so a group may
    # name coldkeys, hotkeys or owners interchangeably and still collapse.
    # Token forms that no submitted hotkey uses are inert — they are never
    # added to ``tokens``, so they cannot become a component's label.
    for group in merge_groups():
        anchor: str | None = None
        for ident in sorted(group):
            forms = [f"ck:{ident}", f"own:{ident}", f"hk:{ident}"]
            if ident in coldkey_map:
                # Named by HOTKEY: merge the identity it actually resolves to
                # (its coldkey), not just its own inert hotkey token.
                forms.append(base(ident))
            for form in forms:
                find(form)
                if anchor is None:
                    anchor = form
                else:
                    union(anchor, form)

    # Best clean label per component: coldkey > owner > hotkey, smallest within.
    def _rank(tok: str) -> tuple[int, str]:
        tier = 0 if tok.startswith("ck:") else 1 if tok.startswith("own:") else 2
        return (tier, tok)

    rep: dict[str, str] = {}
    for tok in tokens:
        r = find(tok)
        if r not in rep or _rank(tok) < _rank(rep[r]):
            rep[r] = tok

    def _label(tok: str) -> str:
        return tok.split(":", 1)[1] if ":" in tok else tok

    actor_map = {hk: _label(rep[find(base(hk))]) for hk in hotkeys}
    return actor_map, set(coldkey_map)


class ActorResolver:
    """A FROZEN hotkey→actor view for one selection pass / round."""

    __slots__ = ("_actor", "_coldkey_backed", "source")

    def __init__(self, actor_map: dict[str, str], coldkey_backed: set[str], source: str) -> None:
        self._actor = actor_map
        self._coldkey_backed = coldkey_backed
        self.source = source

    @classmethod
    def from_maps(
        cls,
        coldkey_map: dict[str, str],
        owner_map: dict[str, list[str]] | None = None,
        *,
        source: str = "built",
    ) -> ActorResolver:
        """Build a resolver by running the coldkey ∪ owner union-find — the
        single construction path (snapshot + tests)."""
        actor_map, backed = _build_actor_map(coldkey_map, owner_map or {})
        return cls(actor_map, backed, source)

    def __call__(self, hotkey: str) -> str:
        hotkey = hotkey or ""
        return self._actor.get(hotkey) or hotkey

    def mapped(self, hotkey: str) -> str | None:
        """The actor for a COLDKEY-BACKED hotkey (owner-enriched), else None.

        Conservative on purpose: the cross-actor copy reject treats a hotkey
        with no on-chain coldkey as INDETERMINATE and stands down (the #1030
        review's degraded-reject fix), while owner-linked coldkeys correctly
        resolve to one actor.
        """
        hotkey = hotkey or ""
        if hotkey not in self._coldkey_backed:
            return None
        return self._actor.get(hotkey) or None


def snapshot_resolver() -> ActorResolver | None:
    """The actor view for one pass — or ``None``, meaning: run the legacy
    per-hotkey path, unchanged.

    ``None`` when the kill-switch is on OR no coldkey map exists yet. Never
    raises. The owner union only enriches a coldkey-backed resolver.
    """
    if actor_key_mode() != "coldkey":
        return None
    coldkey_map = _coldkey_map()
    if not coldkey_map:
        return None
    owner_map = _owner_map() if owner_union_enabled() else {}
    src = "metagraph" if _coldkey_provider is not None else "sidecar"
    if owner_map:
        src += "+owner"
    return ActorResolver.from_maps(coldkey_map, owner_map, source=src)


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
    actor junior, so rotating to a fresh sibling hotkey (or coldkey, under the
    owner union) no longer resets seniority. An actor with no benched hotkey is
    absent (=> 0.0 upstream).
    """
    out: dict[str, float] = {}
    for hk, ts in last_selected.items():
        actor = actor_of(hk) or hk
        if ts > out.get(actor, 0.0):
            out[actor] = ts
    return out


def actor_first_seen(
    first_seen: dict[str, float],
    actor_of: Callable[[str], str],
) -> dict[str, float]:
    """Aggregate a ``{hotkey: first_seen_ts}`` map to ``{actor: MIN ts}``.

    MIN is the point: an actor is as senior as its EARLIEST-appearing hotkey,
    so an operator can't reset its wait clock by rotating to a fresh sibling
    hotkey/coldkey — the fleet's oldest identity sets the actor's age. Pairs
    with :func:`actor_last_selected` (MAX benched) in :func:`rotation.wait_ts`.
    """
    out: dict[str, float] = {}
    for hk, ts in first_seen.items():
        actor = actor_of(hk) or hk
        cur = out.get(actor)
        if cur is None or ts < cur:
            out[actor] = ts
    return out


def distinct_actor_count(hotkeys: Iterable[Any], actor_of: Callable[[str], str]) -> int:
    """Observability helper: how many actors a set of hotkeys collapses to."""
    return len({actor_of(str(hk) or "") for hk in hotkeys})
