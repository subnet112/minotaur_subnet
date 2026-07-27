#!/usr/bin/env python3
"""Replay the Stage-2 corpus dedup under a PROPOSED role-derived key.

Why this exists
---------------
``order_sampler._dedup_key`` collapses near-duplicate orders so the benchmark
corpus spends its scenarios on distinct trade shapes instead of 300 variations
of the same swap. It does that with hardcoded swap params::

    _BUCKETED_PARAMS = {"input_token", "output_token",
                        "input_amount", "min_output_amount"}

which is DEX-specific code in an app-agnostic path. The replacement idea is to
derive each param's ROLE from the app manifest instead of naming params in
core: ``address``-ish types are categorical (exact), ``uint``-ish types are
magnitudes (decade-bucketed), ``in_signature=False`` params are computed
(excluded), and one new ``derived_from`` field marks a param that SCALES with
another (``min_output_amount`` ~ ``input_amount``) and would otherwise defeat
the bucketing.

Before any of that is written, it has to be shown to reproduce the collapse the
current key achieves on REAL data. ``_dedup_key`` documents its own number: on
the live 2026-07-02 corpus the near-dup key collapsed 393 candidates to 173
distinct shapes (55%), versus 330 (16%) for exact-shape identity. If the
role-derived key can't reproduce that, the design is wrong and should change
before it costs a corpus migration.

This tool changes nothing. It reads a corpus, partitions it under both keys,
and reports whether the partitions agree.

FINDINGS — live leader, 2026-07-27
----------------------------------
QUOTES (2246 rows, app_0867cdd4effd):

    current key  : 2246 -> 2246 shapes (0% collapse)
    proposed key : 2246 -> 2237 shapes (0% collapse)
    pair agreement 1.0000 over 2.5M pairs; 0 split, 9 merged

Two things fall out of that, and the first matters more than the refactor:

1. The documented 55% collapse DOES NOT REPRODUCE on today's corpus — the
   near-dup key currently collapses NOTHING. The 393->173 figure came from the
   ORDER corpus of 2026-07-02; the quote corpus that now feeds Stage-2 is
   dominated by blind-spot probing across distinct token pairs, so almost every
   row is a genuinely different (pair, decade) shape. Whether the bucketing is
   worth its DEX coupling is therefore an OPEN question about corpus mix, not a
   settled 55% that a refactor risks losing. (The order corpus is down to 8 rows
   on this leader — 50% collapse, both keys identical — far too small to judge.)

2. The 9 extra merges are the proposal being MORE correct, not less. Each is a
   pair of quotes differing only by ``unwrap_output: False`` vs the field being
   absent — semantically the same trade. The manifest declares that param
   ``source=system, in_signature=False``, so the proposal excludes it; core
   keeps it exact because it appears in none of the three hardcoded sets.

The role derivation needs NO new manifest field. Every name core hardcodes is
already declared by the app:

    input_token/output_token   user   + address   -> categorical
    input_amount               user   + uint256   -> magnitude
    min_output_amount          quote             -> derived, excluded
    receiver, permit_*         system            -> identity, excluded
    platform_fee_wei,          quote + in_signature=False -> computed, excluded
    quoted_output

Usage
-----
On the leader (has both the code and the store)::

    docker cp tools/corpus_dedup_replay.py production-api-1:/tmp/
    docker exec production-api-1 python3 /tmp/corpus_dedup_replay.py --store /data/store.json

Offline, against a dump taken earlier::

    ./tools/corpus_dedup_replay.py --dump corpus.json

Take that dump with ``--dump-to`` on a node that has the store.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

# ── Role derivation (the PROPOSAL under test) ───────────────────────────────

_CATEGORICAL_PREFIXES = ("address", "bytes", "bool", "string")
_MAGNITUDE_PREFIXES = ("uint", "int")

ROLE_CATEGORICAL = "categorical"
ROLE_MAGNITUDE = "magnitude"
ROLE_COMPUTED = "computed"
ROLE_DERIVED = "derived"
ROLE_UNKNOWN = "unknown"


def _param_specs(fn: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield (name, spec) for either manifest param shape.

    Live apps carry ``params`` as a DICT keyed by name; the typed
    ``IntentFunctionSpec`` dataclass form carries a LIST of specs each with a
    ``name``. Both are in use, so read both.
    """
    params = fn.get("params")
    if isinstance(params, dict):
        return [(k, v if isinstance(v, dict) else {}) for k, v in params.items()]
    out = []
    for spec in params or []:
        if isinstance(spec, dict) and spec.get("name"):
            out.append((spec["name"], spec))
    return out


def derive_roles(manifest: dict[str, Any], intent_function: str) -> dict[str, str]:
    """param name -> role, read off what the app ALREADY declares.

    No new manifest field is needed — the roles the sampler wants are exactly
    the ``source`` values apps already publish:

      source="system"    -> identity/platform-supplied (receiver, permit_*).
                            Excluded: this is what _QUOTE_IDENTITY_PARAMS
                            hardcodes today.
      source="quote"     -> derived from the quote (min_output_amount, whose
                            ``quote_field`` is suggested_min_output). Excluded:
                            it SCALES with the amount and would defeat the
                            magnitude bucketing — precisely why the current key
                            lists it in _BUCKETED_PARAMS to drop it.
      in_signature=False -> computed/appended (fee, quoted output). Excluded.
      source="user" ...  -> the trade descriptor, split by ABI type:
                              address/bytes/bool -> categorical (exact)
                              uint/int           -> magnitude (decade bucket)

    Nothing here names a swap param.
    """
    roles: dict[str, str] = {}
    for fn in manifest.get("intent_functions", []) or []:
        if fn.get("name") != intent_function:
            continue
        for name, spec in _param_specs(fn):
            source = str(spec.get("source", "user")).lower()
            if spec.get("in_signature") is False:
                roles[name] = ROLE_COMPUTED
            elif source == "system":
                roles[name] = ROLE_COMPUTED
            elif source == "quote" or spec.get("derived_from"):
                roles[name] = ROLE_DERIVED
            else:
                vt = str(spec.get("type") or spec.get("value_type") or "").lower()
                if vt.startswith(_CATEGORICAL_PREFIXES):
                    roles[name] = ROLE_CATEGORICAL
                elif vt.startswith(_MAGNITUDE_PREFIXES):
                    roles[name] = ROLE_MAGNITUDE
                else:
                    roles[name] = ROLE_UNKNOWN
    return roles


def proposed_dedup_key(
    order: dict[str, Any],
    roles_by_fn: dict[str, dict[str, str]],
    volatile: set[str],
) -> str:
    """The role-derived near-dup key. Mirrors _dedup_key's SHAPE exactly —
    prefix, categoricals exact+lowered, magnitudes decade-bucketed, everything
    else kept exact — but decides which is which from the manifest."""
    params = order.get("params") or {}
    core = {k: v for k, v in params.items() if k not in volatile}
    prefix = [order.get("app_id", ""), order.get("intent_function", ""),
              order.get("chain_id")]
    roles = roles_by_fn.get(order.get("intent_function", ""), {})

    categorical: list[str] = []
    magnitude: list[str] = []
    rest: dict[str, Any] = {}
    for k in sorted(core):
        role = roles.get(k, ROLE_UNKNOWN)
        if role in (ROLE_COMPUTED, ROLE_DERIVED):
            continue                      # excluded from identity
        if role == ROLE_CATEGORICAL:
            categorical.append(f"{k}={str(core[k]).lower()}")
        elif role == ROLE_MAGNITUDE:
            magnitude.append(f"{k}={_amount_decade(core[k])}")
        else:
            rest[k] = core[k]

    # No manifest roles at all -> exact-shape identity, the same conservative
    # fallback the current key uses for a non-swap order.
    if not categorical and not magnitude:
        return _canon(prefix + [core])
    return _canon(prefix + [categorical, magnitude, rest])


def _canon(parts: Any) -> str:
    return json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)


def _amount_decade(amount: Any) -> str:
    """Byte-identical to order_sampler._amount_decade (kept local so this tool
    runs against a dump without importing the package)."""
    try:
        value = int(str(amount))
    except (TypeError, ValueError):
        return f"raw:{amount}"
    if value <= 0:
        return f"raw:{amount}"
    return f"e{len(str(value)) - 1}"


# ── Partition comparison ────────────────────────────────────────────────────


def partition(orders: list[dict[str, Any]], key_fn: Any) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = defaultdict(list)
    for i, order in enumerate(orders):
        buckets[key_fn(order)].append(i)
    return buckets


def compare(
    orders: list[dict[str, Any]],
    current: dict[str, list[int]],
    proposed: dict[str, list[int]],
) -> dict[str, Any]:
    """Do the two keys induce the SAME grouping?

    Reported as pair-level agreement, which is what actually matters: a
    collapse ratio can match by coincidence while grouping different orders
    together. ``split`` = pairs the current key collapsed and the proposal
    separated (corpus grows, diversity over-counted). ``merged`` = pairs the
    proposal collapsed and the current key kept apart (corpus shrinks,
    potentially losing a genuinely distinct scenario) — the riskier direction.
    """
    cur_of = {i: k for k, idxs in current.items() for i in idxs}
    pro_of = {i: k for k, idxs in proposed.items() for i in idxs}

    split = merged = agree_together = agree_apart = 0
    n = len(orders)
    for a in range(n):
        for b in range(a + 1, n):
            same_cur = cur_of[a] == cur_of[b]
            same_pro = pro_of[a] == pro_of[b]
            if same_cur and same_pro:
                agree_together += 1
            elif not same_cur and not same_pro:
                agree_apart += 1
            elif same_cur:
                split += 1
            else:
                merged += 1
    total_pairs = n * (n - 1) // 2
    return {
        "pairs": total_pairs,
        "agree_together": agree_together,
        "agree_apart": agree_apart,
        "split_by_proposal": split,
        "merged_by_proposal": merged,
        "pair_agreement": (
            (agree_together + agree_apart) / total_pairs if total_pairs else 1.0
        ),
    }


# ── Corpus loading ──────────────────────────────────────────────────────────


def load_from_store(
    store_path: str, source: str = "orders",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the corpus + each app's manifest from a live node's store.

    ``source="quotes"`` reads the QUOTE-case corpus, which is where the volume
    actually is (thousands of rows vs a handful of orders on a leader that has
    mostly served quotes). A quote case is the same shape the dedup key needs —
    app_id / intent_function / chain_id / params — with identity already
    stripped at capture, so the two keys compare on it directly.
    """
    sys.path.insert(0, "/home/minotaur/app")
    from minotaur_subnet.store.app_intent_store import AppIntentStore

    store = AppIntentStore(store_path)
    if source == "quotes":
        orders = [
            # quote_id doubles as the representative id for reporting
            {**q, "order_id": q.get("quote_id", "")}
            for q in store.list_quotes()
        ]
    else:
        orders = store.list_orders()
    manifests: dict[str, Any] = {}
    for app_id in {o.get("app_id") for o in orders if o.get("app_id")}:
        try:
            app = store.get_app(app_id)
            manifest = getattr(app, "manifest", None)
            manifests[app_id] = manifest if isinstance(manifest, dict) else {}
        except Exception as exc:  # noqa: BLE001
            print(f"  ! manifest unavailable for {app_id}: {exc}", file=sys.stderr)
            manifests[app_id] = {}
    return orders, manifests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", help="path to a live node's store (e.g. /data/store.json)")
    ap.add_argument("--dump", help="corpus JSON written earlier by --dump-to")
    ap.add_argument("--dump-to", help="write the loaded corpus here and exit")
    ap.add_argument("--app", help="restrict to one app_id")
    ap.add_argument("--source", choices=("orders", "quotes"), default="orders",
                    help="which corpus to replay (quotes is where the volume is)")
    args = ap.parse_args()

    if args.store:
        orders, manifests = load_from_store(args.store, args.source)
    elif args.dump:
        with open(args.dump) as fh:
            blob = json.load(fh)
        orders, manifests = blob["orders"], blob["manifests"]
    else:
        ap.error("one of --store / --dump is required")

    if args.app:
        orders = [o for o in orders if o.get("app_id") == args.app]

    if args.dump_to:
        with open(args.dump_to, "w") as fh:
            json.dump({"orders": orders, "manifests": manifests}, fh)
        print(f"wrote {len(orders)} orders + {len(manifests)} manifests "
              f"to {args.dump_to}")
        return 0

    if not orders:
        print("no orders in corpus — nothing to compare")
        return 1

    # Current key: import the real thing so this can never drift from prod.
    try:
        sys.path.insert(0, "/home/minotaur/app")
        from minotaur_subnet.harness.order_sampler import (
            _VOLATILE_PARAMS,
            _dedup_key,
        )
    except ImportError:
        print("! cannot import order_sampler — run with --store on a node, or "
              "from a checkout", file=sys.stderr)
        return 2

    roles_by_app: dict[str, dict[str, dict[str, str]]] = {}
    for app_id, manifest in manifests.items():
        fns = {}
        for fn in (manifest or {}).get("intent_functions", []) or []:
            name = fn.get("name")
            if name:
                fns[name] = derive_roles(manifest, name)
        roles_by_app[app_id] = fns

    def proposed(order: dict[str, Any]) -> str:
        return proposed_dedup_key(
            order, roles_by_app.get(order.get("app_id", ""), {}),
            set(_VOLATILE_PARAMS),
        )

    cur = partition(orders, _dedup_key)
    pro = partition(orders, proposed)

    print(f"corpus: {len(orders)} orders, {len(manifests)} apps\n")
    print(f"  current key  : {len(orders)} -> {len(cur)} shapes "
          f"({100 * (1 - len(cur) / len(orders)):.0f}% collapse)")
    print(f"  proposed key : {len(orders)} -> {len(pro)} shapes "
          f"({100 * (1 - len(pro) / len(orders)):.0f}% collapse)")

    # Role coverage — a param the manifest doesn't describe falls into the
    # exact-shape "rest" bucket, which is safe but under-collapses.
    unknown: dict[str, int] = defaultdict(int)
    for order in orders:
        roles = roles_by_app.get(order.get("app_id", ""), {}).get(
            order.get("intent_function", ""), {},
        )
        for k in (order.get("params") or {}):
            if k not in roles and k not in _VOLATILE_PARAMS:
                unknown[k] += 1
    if unknown:
        print("\n  params with NO manifest role (fall back to exact identity):")
        for name, count in sorted(unknown.items(), key=lambda kv: -kv[1])[:15]:
            print(f"    {name:28s} {count}")

    stats = compare(orders, cur, pro)
    print(f"\n  pair agreement : {stats['pair_agreement']:.4f} "
          f"({stats['pairs']} pairs)")
    print(f"    split by proposal  : {stats['split_by_proposal']} "
          f"(current collapsed, proposal separated)")
    print(f"    merged by proposal : {stats['merged_by_proposal']} "
          f"(proposal collapsed, current separated)  <-- the risky direction")

    if stats["merged_by_proposal"]:
        print("\n  MERGED EXAMPLES (proposal may be losing a distinct scenario):")
        shown = 0
        cur_of = {i: k for k, idxs in cur.items() for i in idxs}
        for _, idxs in pro.items():
            if len(idxs) < 2 or shown >= 3:
                continue
            keys = {cur_of[i] for i in idxs}
            if len(keys) > 1:
                shown += 1
                print(f"    proposal bucket of {len(idxs)} spans "
                      f"{len(keys)} current shapes:")
                for i in idxs[:3]:
                    p = orders[i].get("params") or {}
                    print(f"      {orders[i].get('order_id','?')[:12]} "
                          f"{ {k: p[k] for k in sorted(p)[:4]} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
