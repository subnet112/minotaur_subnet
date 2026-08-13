#!/usr/bin/env python3
"""Replay the destination-leg delivery measurement for determinism soak.

Why this exists
---------------
``destination_delivered`` (#1133) is measured observe-only today, with one
exit criterion standing between it and any scoring rule::

    Before any scoring rule consumes this: destination_delivered must prove
    IDENTICAL on leader and follower for the same plan on the same per-chain
    fork pins. A rule fed by a non-deterministic input splits consensus.

No solver emits cross-chain plans yet, so there is no organic traffic to soak
on — the criterion has to be proven synthetically. This tool runs the EXACT
production measurement path (``harness.orchestrator._measure_destination_delivery``
over the node's own sim forks, the same call the benchmark makes) N times over
a case file and reports the observed values. Run it with the same case file
and the same ``fork_block`` pins on two nodes; the outputs must be
byte-identical, both intra-node (N repeats) and across nodes.

This tool changes nothing: the measurement path is read-only against the sim
forks (snapshot/revert), and nothing here writes to any store.

Usage
-----
On a node (api or benchmark-worker container has the code, env and anvils)::

    docker cp tools/destination_delivery_replay.py production-api-1:/tmp/
    docker exec production-api-1 python3 /tmp/destination_delivery_replay.py \
        --case /tmp/case.json --repeat 5 --out /tmp/replay.leader.json

    # same case + pins on a follower, then:
    diff <(jq -S .runs replay.leader.json) <(jq -S .runs replay.follower.json)

Generate a starter case file (synthetic mock-shaped legs — runs anywhere, but
exercises only the plumbing; for the real soak, dump a baseline-solver plan)::

    ./tools/destination_delivery_replay.py --dump-template case.json

Case file schema (all amounts are DECIMAL STRINGS)::

    {
      "plan": {
        "intent_id": "...",
        "interactions": [{"target": "0x..", "value": "0",
                          "call_data": "0x..", "chain_id": 8453}],
        "deadline": 0, "nonce": 0,
        "metadata": { ... cross_chain_plan | multi_leg_plan | legs ... }
      },
      "state": {"contract_address": "0x..", "chain_id": 8453,
                "raw_params": { ... intent params ... }},
      "token_balances": {"0x..token": "1000000000000000000"} | null,
      "fork_block": 12345678 | null
    }

Exit codes: 0 = all repeats identical, 1 = intra-node divergence (determinism
bug — file it before any scoring rule ships), 2 = setup/usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


TEMPLATE_CASE: dict[str, Any] = {
    "plan": {
        "intent_id": "replay-demo",
        "interactions": [],
        "deadline": 0,
        "nonce": 0,
        "metadata": {
            "cross_chain_plan": {
                "legs": [
                    {"chain_id": 8453, "interactions": [{
                        "target": "0x" + "11" * 20, "value": "0",
                        "call_data": "0xa9059cbb" + "00" * 28,
                        "chain_id": 8453,
                    }]},
                    {"chain_id": 1, "interactions": [{
                        "target": "0x" + "11" * 20, "value": "0",
                        "call_data": "0xa9059cbb" + "00" * 28,
                        "chain_id": 1,
                    }]},
                ],
                "bridge_requests": [{
                    # WETH Base -> Ethereum, 1 WETH
                    "token": "0x4200000000000000000000000000000000000006",
                    "amount": 10**18,
                    "src_chain_id": 8453,
                    "dst_chain_id": 1,
                }],
            },
        },
    },
    "state": {
        "contract_address": "",
        "chain_id": 8453,
        "raw_params": {},
    },
    "token_balances": None,
    "fork_block": None,
}


def _build_simulator() -> Any:
    """The node's MultiChainSimulator, from the same registry ladders the
    api / benchmark-worker use (api/startup.py:_build_simulator)."""
    from minotaur_subnet.chains import wiring as chain_wiring

    sim_rpc_urls = chain_wiring.sim_rpc_urls()
    if not sim_rpc_urls:
        raise SystemExit("no sim RPC urls configured on this node (exit 2)")
    from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator

    return MultiChainSimulator(
        sim_rpc_urls, upstream_rpc_urls=chain_wiring.upstream_rpc_urls(),
    )


def _load_case(path: str) -> tuple[Any, Any, dict[str, int] | None, int | None]:
    from minotaur_subnet.shared.types import ExecutionPlan, Interaction, IntentState

    with open(path) as f:
        case = json.load(f)

    p = case["plan"]
    plan = ExecutionPlan(
        intent_id=p.get("intent_id", "replay"),
        interactions=[
            Interaction(
                target=i["target"], value=str(i.get("value", "0")),
                call_data=i["call_data"], chain_id=int(i.get("chain_id", 0)),
            )
            for i in p.get("interactions", [])
        ],
        deadline=int(p.get("deadline", 0)),
        nonce=int(p.get("nonce", 0)),
        metadata=p.get("metadata") or {},
    )

    s = case.get("state") or {}
    state = IntentState(
        contract_address=s.get("contract_address", ""),
        chain_id=int(s.get("chain_id", 0)),
        nonce=int(s.get("nonce", 0)),
        owner=s.get("owner", ""),
        raw_params=s.get("raw_params") or {},
    )

    balances_raw = case.get("token_balances")
    token_balances = (
        {k: int(v) for k, v in balances_raw.items()} if balances_raw else None
    )
    fork_block = case.get("fork_block")
    return plan, state, token_balances, (int(fork_block) if fork_block else None)


async def _run(args: argparse.Namespace) -> int:
    from minotaur_subnet.harness.orchestrator import _measure_destination_delivery

    plan, state, token_balances, fork_block = _load_case(args.case)
    simulator = _build_simulator()

    runs: list[dict[str, Any]] = []
    for n in range(args.repeat):
        delivered, source, diagnosis = await _measure_destination_delivery(
            simulator, plan, state, token_balances, fork_block,
        )
        runs.append({
            "run": n,
            "destination_delivered": delivered,
            "destination_amount_source": source,
            # WHY a zero, when it is one. Part of the determinism surface: the
            # diagnosis rides a persisted row, so it has to be identical across
            # runs and validators exactly like the amount is.
            "destination_delivery_diagnosis": diagnosis,
        })
        print(json.dumps(runs[-1], sort_keys=True))

    distinct = {
        json.dumps(
            (r["destination_delivered"], r["destination_amount_source"],
             r["destination_delivery_diagnosis"]),
            sort_keys=True,
        )
        for r in runs
    }
    report = {
        "case": args.case,
        "fork_block": fork_block,
        "repeat": args.repeat,
        "identical": len(distinct) == 1,
        "runs": runs,
    }
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
    print(f"identical={report['identical']} distinct={len(distinct)}")
    return 0 if report["identical"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", help="case JSON file (see module docstring)")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--out", help="write the full report JSON here")
    ap.add_argument(
        "--dump-template",
        help="write a starter case file to this path and exit",
    )
    args = ap.parse_args()

    if args.dump_template:
        with open(args.dump_template, "w") as f:
            json.dump(TEMPLATE_CASE, f, indent=2)
        print(f"template written to {args.dump_template}")
        return 0
    if not args.case:
        ap.error("--case is required (or --dump-template)")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
