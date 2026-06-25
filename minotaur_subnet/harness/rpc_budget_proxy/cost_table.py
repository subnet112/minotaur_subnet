"""Deterministic JSON-RPC cost table — a Minotaur consensus constant.

CONSENSUS SURFACE
=================
This module defines the *price list* the validator fleet uses to meter an
untrusted solver's RPC "work" against an Anvil fork during benchmark scoring.
It MUST be byte-identical on every validator. Two validators that disagree on
the cost of a method would disagree on whether a given ``generate_plan`` ran
out of budget, which would make a too-expensive solver pass on one validator
and fail on another — i.e. consensus would not form.

For that reason the table is treated like any other consensus constant:

- It is **versioned** (:data:`COST_TABLE_VERSION`). A change to any cost, the
  default, or the version string is a protocol change. Bump the version when
  you change the numbers so the new table can be folded into the benchmark
  pack hash and old/new validators refuse to agree across the boundary.
- :func:`cost_table_record` returns a canonical, sorted, hashable record. The
  benchmark-pack hashing layer (``benchmark_pack.py``) folds this record in so
  that a fleet running a different cost table produces a different pack hash
  and therefore cannot reach quorum with the majority. (We only *build* the
  record here — hashing lives in the pack layer.)

WHY A FIXED INTEGER TABLE (not wall-clock)
==========================================
The budget proxy replaces a non-deterministic wall-clock timeout. Wall-clock
time depends on CPU, RPC latency, and load, so the same solver can time out on
one validator and finish on another — non-deterministic, consensus-breaking.
A fixed *integer* cost per JSON-RPC method plus a fleet-uniform integer budget
gives every validator the exact same cut-off point for the exact same call
sequence: deterministic by construction.

Keep the table **small, explicit, and documented**. Every entry is a
deliberate consensus decision, not an implementation detail.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Versioned consensus constant. Bump on ANY change to the costs/default below.
# ---------------------------------------------------------------------------
COST_TABLE_VERSION = "v1"

# Default cost charged for any JSON-RPC method not explicitly listed below.
# Unknown methods are assumed to be "normal" state reads (cost 1) rather than
# free, so a solver can't dodge the meter by inventing exotic method names.
DEFAULT_COST = 1

# Explicit per-method costs. Anything not here uses DEFAULT_COST.
#
# Rationale for the chosen weights:
#   * State reads against the fork (eth_call / eth_getStorageAt / eth_getCode /
#     eth_getBalance / eth_getTransactionCount / eth_getBlockByNumber) are the
#     solver's bread-and-butter and each costs 1.
#   * eth_getLogs can scan a wide block range and is materially heavier, so it
#     costs 2.
#   * Trivial metadata calls (chain id, block number, gas price, net version)
#     are effectively free (cost 0) — they don't represent solver "work" we
#     want to bound and charging for them would only add noise.
METHOD_COST: dict[str, int] = {
    "eth_call": 1,
    "eth_getStorageAt": 1,
    "eth_getCode": 1,
    "eth_getBalance": 1,
    "eth_getTransactionCount": 1,
    "eth_getBlockByNumber": 1,
    "eth_getLogs": 2,
    "eth_blockNumber": 0,
    "eth_chainId": 0,
    "net_version": 0,
    "eth_gasPrice": 0,
}


def request_cost(method: str) -> int:
    """Return the integer cost of a single JSON-RPC method.

    Unlisted methods fall back to :data:`DEFAULT_COST`. A non-string or empty
    method is also charged the default cost (fail-loud-ish: an unparseable
    request still consumes budget rather than slipping through free).
    """
    if not isinstance(method, str) or not method:
        return DEFAULT_COST
    return METHOD_COST.get(method, DEFAULT_COST)


def batch_cost(methods: list[str]) -> int:
    """Return the total cost of a JSON-RPC batch = sum of member costs.

    A batch request is charged the sum of the costs of each of its members,
    so batching gives a solver no discount on the meter.
    """
    return sum(request_cost(m) for m in methods)


def cost_table_record() -> dict:
    """Return the canonical, hashable record of the active cost table.

    The record is deterministic: methods are sorted by name so the dict has a
    stable iteration order regardless of insertion order. This is the object
    the benchmark-pack hashing layer folds in to bind the cost table into
    consensus. We build the record here; we do NOT hash it.

    Returns:
        ``{"version": str, "default": int, "methods": {sorted method: cost}}``
    """
    return {
        "version": COST_TABLE_VERSION,
        "default": DEFAULT_COST,
        "methods": dict(sorted(METHOD_COST.items())),
    }
