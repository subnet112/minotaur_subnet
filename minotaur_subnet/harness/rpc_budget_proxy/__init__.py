"""Deterministic JSON-RPC governance proxy for the benchmark (trusted sidecar).

A trusted proxy inserted between an UNTRUSTED benchmark solver container and the
validator's RPC upstream. The solver's ONLY route to a fork/RPC is this proxy.
It carries two orthogonal, composable concerns:

1. **Block-pin** (the load-bearing fix): when a session has a per-chain pinned
   block, the proxy FORCES every read to that block (:mod:`.rewrite_table`)
   before forwarding to the validator's own configured upstream archive RPC.
   So a single ``eth_call`` is ONE round-trip the upstream evaluates against its
   local archive — instead of an Anvil fork lazily fetching every cold slot
   one-at-a-time — and the solver reads exactly the state it is scored against,
   deterministically on any archive provider (a fixed block has one state root).
2. **Budget** (anti-abuse backstop): metering the solver's RPC work per session
   via a versioned cost table (:mod:`.cost_table`); observe by default, with an
   optional hard cut so a pathological solver firing thousands of (now cheap)
   calls can be bounded identically on every validator.

TRUSTED sidecar (not in the untrusted blast radius): the data plane (``/rpc/...``)
faces the solver; the control plane (``/control/...``, where the per-session
pinned blocks + budget are set) is reachable only by the validator on a separate
network — so the untrusted solver can neither override the pin nor reach the
upstream/key directly.

Modules:
- ``rewrite_table``: the versioned consensus block-rewrite rules.
- ``cost_table``: the versioned consensus cost constant.
- ``proxy``: the aiohttp pinning + budget proxy server.
"""
