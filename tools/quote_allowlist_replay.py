#!/usr/bin/env python3
"""Replay quote-case param retention under the app-declared ALLOWLIST.

Why this exists
---------------
A quote case is served publicly on /v1/quotes and replicated fleet-wide, so
only the trade descriptor may be retained. Today that is enforced by a
DENYLIST (``_QUOTE_IDENTITY_PARAMS``) whose own comment flags the hole: a
novel identity key on a future app reaches a public endpoint by omission.
#1168 replaces it with an allowlist derived from the app manifest — keep
exactly what the app declares ``source=user``, drop everything else.

That is not a free swap. The stored params feed ``quote_case_id``, which is
content-addressed, so any row whose retained params CHANGE gets a new id. New
ids churn the Stage-2 corpus and therefore move ``benchmark_pack_hash`` — a
consensus artifact that must land fleet-uniform on a round boundary. And the
rule FAILS CLOSED, so rows it cannot describe are skipped rather than stored
with a hollowed-out descriptor.

This tool changes nothing. It reads the live quote corpus, applies both rules,
and reports how many rows are retained identically, churn to a new id, or get
skipped — so the pack-hash move is a measured number rather than a hope.

Usage
-----
On a node with the store::

    docker cp tools/quote_allowlist_replay.py production-api-1:/tmp/
    docker exec production-api-1 python3 /tmp/quote_allowlist_replay.py \
        --store /data/store.json

Exit code is 0 always — this is a measurement, not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any


# ── Current rule: denylist strip (mirrors order_sampler capture) ────────────

_PII_FIELDS = {"user_address", "user", "sender", "from", "recipient", "receiver", "to"}
_VOLATILE_PARAMS = {"deadline", "nonce", "salt", "valid_until", "expiry"}
_QUOTE_IDENTITY_PARAMS = {"owner", "beneficiary", "refund_address", "referrer"}
_STRIP = _PII_FIELDS | _VOLATILE_PARAMS | _QUOTE_IDENTITY_PARAMS


def current_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if k not in _STRIP}


# ── Proposed rule: manifest allowlist (mirrors quote_case_params) ───────────

def proposed_params(
    manifest: dict[str, Any],
    intent_function: str,
    params: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    declared: dict[str, str] = {}
    for fn in (manifest or {}).get("intent_functions", []) or []:
        if fn.get("name") != intent_function:
            continue
        raw = fn.get("params")
        items = (
            list(raw.items()) if isinstance(raw, dict)
            else [(p.get("name"), p) for p in (raw or []) if isinstance(p, dict)]
        )
        declared = {
            n: str(spec.get("source", "user")).lower()
            for n, spec in items
            if n and isinstance(spec, dict)
        }
        break
    if not declared:
        return None, f"app declares no params for intent '{intent_function}'"

    supplied = dict(params or {})
    unknown = [k for k in supplied if k not in declared and k not in _STRIP]
    if unknown:
        return None, f"request carries undeclared params: {sorted(unknown)[:4]}"

    kept = {k: v for k, v in supplied.items() if declared.get(k) == "user"}
    if not kept:
        return None, "no source=user params survive the allowlist"
    return kept, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", help="path to a node's store (e.g. /data/store.json)")
    ap.add_argument("--dump", help="corpus JSON from --dump-to")
    ap.add_argument("--dump-to", help="write the loaded corpus here and exit")
    args = ap.parse_args()

    if args.store:
        from minotaur_subnet.store.app_intent_store import AppIntentStore

        store = AppIntentStore(args.store)
        rows = store.list_quotes()
        manifests = {}
        for app_id in {r.get("app_id") for r in rows if r.get("app_id")}:
            app = store.get_app(app_id)
            m = getattr(app, "manifest", None)
            manifests[app_id] = m if isinstance(m, dict) else {}
    elif args.dump:
        blob = json.load(open(args.dump))
        rows, manifests = blob["quotes"], blob["manifests"]
    else:
        ap.error("one of --store / --dump is required")

    if args.dump_to:
        json.dump({"quotes": rows, "manifests": manifests}, open(args.dump_to, "w"))
        print(f"wrote {len(rows)} rows to {args.dump_to}")
        return 0

    same = churn = skipped = 0
    reasons: Counter = Counter()
    churn_examples: list[str] = []
    skip_examples: list[str] = []

    for r in rows:
        fn = r.get("intent_function") or "swap"
        cur = current_params(r.get("params"))
        prop, why = proposed_params(manifests.get(r.get("app_id", ""), {}), fn, r.get("params"))
        qid = str(r.get("quote_id", "?"))[:14]
        if prop is None:
            skipped += 1
            reasons[why] += 1
            if len(skip_examples) < 5:
                skip_examples.append(f"    {qid} intent={fn!r} — {why}")
        elif prop == cur:
            same += 1
        else:
            churn += 1
            dropped = sorted(set(cur) - set(prop))
            added = sorted(set(prop) - set(cur))
            if len(churn_examples) < 5:
                churn_examples.append(
                    f"    {qid} intent={fn!r} drops={dropped} adds={added}"
                )

    total = len(rows)
    print(f"corpus: {total} quotes, {len(manifests)} apps\n")
    print(f"  retained IDENTICALLY (id stable) : {same}")
    print(f"  params CHANGE (new quote_case_id): {churn}   <-- pack-hash churn")
    print(f"  SKIPPED, fail-closed             : {skipped}")
    if churn_examples:
        print("\n  churn examples:")
        print("\n".join(churn_examples))
    if reasons:
        print("\n  why skipped:")
        for why, n in reasons.most_common():
            print(f"    {why:<55} {n}")
        print("\n  skip examples:")
        print("\n".join(skip_examples))

    pct = (100.0 * churn / total) if total else 0.0
    print(f"\n  corpus churn: {churn}/{total} = {pct:.2f}%")
    print("  (churn moves benchmark_pack_hash — promote fleet-uniform on a round boundary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
