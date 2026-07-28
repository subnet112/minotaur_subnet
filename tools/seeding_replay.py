#!/usr/bin/env python3
"""Replay simulator token-seeding under a MANIFEST-DERIVED rule.

Why this exists
---------------
Two places decide which tokens to pre-fund before simulating an order, and
both guess from param NAMES:

  blockloop/simulation.py       input_token | tokenIn | token_in | asset
                                input_amount | amountPerBuy | amount_per_buy
                                             | amount
                                (+ "amountPerBuy present" => deposit-model,
                                 seed the contract instead of the executor)

  harness/orchestrator._build_token_balances
                                a manifest ``_fund`` map when present, else
                                the same input_token/input_amount convention

That alias list is a fixed set of app archetypes (swap / DCA / yield) living
in an app-agnostic path. The replacement idea is to derive the spend side from
the app manifest, which already declares each param's ``type`` and ``source``.

Seeding is NOT cosmetic: if it picks the wrong token or amount, the order's
simulation reverts in safeTransferFrom and scores 0. So before changing it,
measure the proposed rule against REAL orders and show it seeds identically.
This tool changes nothing — it reads orders, computes both seedings, and
reports disagreement.

FINDINGS — live leader, 2026-07-27
----------------------------------
QUOTES (2367 rows, app_0867cdd4effd):

    identical seeding                 2362
    both decline to seed                 4
    CURRENT seeds, proposal does NOT     1   <-- the only disagreement
    both seed, DIFFERENTLY               0   <-- none, the dangerous class

So the manifest-derived rule reproduces the alias-guessing on every row that
matters, and never seeds a DIFFERENT token/amount — the failure mode that
would silently mis-score an order.

The single disagreement is instructive rather than disqualifying:
``q_b0e4f8c50b66`` carries ``intent_function="execute"``, which the app's
manifest does not declare (it declares only ``swap``). It is 1 of 2367 quotes
and 0 of 8 orders — a stale artifact of the removed ``_KNOWN_SIGS`` map, which
aliased ``"execute" -> swap(address,address,uint256,uint256,address)``. Since
#1152 removed that map, an ``execute`` order already resolves its selector to
``keccak("execute()")``, which the contract does not implement, so it was
ALREADY unsupported before this change. Declining to seed is consistent with
that, not a new regression.

What it does prove is a design flaw worth fixing in the implementation: the
manifest-derived rule declines SILENTLY, and a declined seed shows up as a
mysterious score of 0 rather than an error. An app whose manifest is merely
incomplete would be scored 0 with no signal. The real change must therefore
log loudly when it declines to seed an order that HAS params — a manifest gap
should be visible in logs, not inferred from zero scores.

Usage
-----
On a node with the store::

    docker cp tools/seeding_replay.py production-api-1:/tmp/
    docker exec production-api-1 python3 /tmp/seeding_replay.py \
        --store /data/store.json

Offline against a dump::

    ./tools/seeding_replay.py --dump corpus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

# ── Current rule (mirrors blockloop/simulation.py) ──────────────────────────

_TOKEN_ALIASES = ("input_token", "tokenIn", "token_in", "asset")
_AMOUNT_ALIASES = ("input_amount", "amountPerBuy", "amount_per_buy", "amount")
_DEPOSIT_MARKERS = ("amountPerBuy", "amount_per_buy")


def current_seeding(order: dict[str, Any]) -> tuple[dict[str, int] | None, bool]:
    """(token_balances, is_deposit_model) exactly as the live path computes."""
    params = order.get("params") or {}
    token = next((params[k] for k in _TOKEN_ALIASES if params.get(k)), None)
    if token and str(token).startswith("eip155:"):
        token = str(token).split(":")[-1]
    amount = next((params[k] for k in _AMOUNT_ALIASES if params.get(k)), None)
    balances = None
    if token and amount is not None:
        try:
            balances = {token: int(amount)}
        except (TypeError, ValueError):
            balances = None
    deposit = bool(any(params.get(k) for k in _DEPOSIT_MARKERS))
    return balances, deposit


# ── Proposed rule (manifest-derived) ────────────────────────────────────────


def manifest_spend_params(
    manifest: dict[str, Any], intent_function: str,
) -> tuple[str | None, str | None]:
    """Infer (token_param, amount_param) from what the app already declares.

    The spend side is the app's own USER-supplied input: an address-typed
    ``source=user`` param paired with a uint-typed ``source=user`` param.
    ``source=quote`` (a slippage guard) and ``source=system`` (receiver,
    permit fields) are excluded by construction — the same ``source`` values
    the dedup work leaned on.

    Returns (None, None) when the app declares no unambiguous pair, which the
    caller must treat as "cannot seed" rather than guessing.
    """
    for fn in manifest.get("intent_functions", []) or []:
        if fn.get("name") != intent_function:
            continue
        params = fn.get("params")
        items = (
            list(params.items()) if isinstance(params, dict)
            else [(p.get("name"), p) for p in (params or []) if isinstance(p, dict)]
        )
        addrs, uints = [], []
        for name, spec in items:
            if not name or not isinstance(spec, dict):
                continue
            if str(spec.get("source", "user")).lower() != "user":
                continue
            if spec.get("in_signature") is False:
                continue
            vt = str(spec.get("type") or spec.get("value_type") or "").lower()
            if vt.startswith("address"):
                addrs.append(name)
            elif vt.startswith(("uint", "int")):
                uints.append(name)
        # Unambiguous only when exactly one uint is user-supplied; an app with
        # several amounts must say which one it spends.
        if addrs and len(uints) == 1:
            return addrs[0], uints[0]
        if len(addrs) == 1 and uints:
            return addrs[0], uints[0]
        return None, None
    return None, None


def proposed_seeding(
    order: dict[str, Any], manifest: dict[str, Any],
) -> tuple[dict[str, int] | None, str]:
    """(token_balances, reason). reason explains a None so the report is honest."""
    params = order.get("params") or {}
    tok_p, amt_p = manifest_spend_params(
        manifest or {}, order.get("intent_function", ""),
    )
    if not tok_p or not amt_p:
        return None, "no unambiguous spend pair declared"
    token, amount = params.get(tok_p), params.get(amt_p)
    if not token or amount is None:
        return None, f"order omits {tok_p}/{amt_p}"
    if str(token).startswith("eip155:"):
        token = str(token).split(":")[-1]
    try:
        return {token: int(amount)}, "ok"
    except (TypeError, ValueError):
        return None, "non-integer amount"


# ── Report ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", help="path to a node's store (e.g. /data/store.json)")
    ap.add_argument("--dump", help="corpus JSON from --dump-to")
    ap.add_argument("--dump-to", help="write the loaded corpus here and exit")
    ap.add_argument("--source", choices=("orders", "quotes"), default="orders")
    args = ap.parse_args()

    if args.store:
        sys.path.insert(0, "/home/minotaur/app")
        from minotaur_subnet.store.app_intent_store import AppIntentStore

        store = AppIntentStore(args.store)
        rows = (
            [{**q, "order_id": q.get("quote_id", "")} for q in store.list_quotes()]
            if args.source == "quotes" else store.list_orders()
        )
        manifests = {}
        for app_id in {r.get("app_id") for r in rows if r.get("app_id")}:
            app = store.get_app(app_id)
            m = getattr(app, "manifest", None)
            manifests[app_id] = m if isinstance(m, dict) else {}
    elif args.dump:
        blob = json.load(open(args.dump))
        rows, manifests = blob["orders"], blob["manifests"]
    else:
        ap.error("one of --store / --dump is required")

    if args.dump_to:
        json.dump({"orders": rows, "manifests": manifests}, open(args.dump_to, "w"))
        print(f"wrote {len(rows)} rows to {args.dump_to}")
        return 0

    agree = differ = cur_only = prop_only = neither = 0
    reasons: Counter = Counter()
    examples: list[str] = []

    for r in rows:
        cur, _deposit = current_seeding(r)
        prop, reason = proposed_seeding(r, manifests.get(r.get("app_id", ""), {}))
        if cur == prop:
            agree += 1 if cur is not None else 0
            neither += 1 if cur is None else 0
        elif cur is not None and prop is None:
            cur_only += 1
            reasons[reason] += 1
            if len(examples) < 5:
                examples.append(
                    f"    {r.get('order_id','?')[:14]} cur={cur} prop=None ({reason})"
                )
        elif cur is None and prop is not None:
            prop_only += 1
        else:
            differ += 1
            if len(examples) < 5:
                examples.append(
                    f"    {r.get('order_id','?')[:14]} cur={cur} prop={prop}"
                )

    total = len(rows)
    print(f"corpus: {total} {args.source}, {len(manifests)} apps\n")
    print(f"  identical seeding      : {agree}")
    print(f"  both decline to seed   : {neither}")
    print(f"  CURRENT seeds, proposal does NOT : {cur_only}   <-- regressions")
    print(f"  proposal seeds, current does not : {prop_only}")
    print(f"  both seed, DIFFERENTLY           : {differ}   <-- the dangerous one")
    if reasons:
        print("\n  why the proposal declined:")
        for reason, n in reasons.most_common():
            print(f"    {reason:42s} {n}")
    if examples:
        print("\n  examples:")
        for e in examples:
            print(e)
    ok = (cur_only == 0 and differ == 0)
    print(f"\n  VERDICT: {'safe to swap in' if ok else 'NOT equivalent — do not ship'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
