"""Deterministic JSON-RPC counting/budget proxy (trusted validator sidecar).

This package inserts a counting proxy between an untrusted benchmark solver
container and the Anvil fork(s) it executes against. The solver's ONLY route
to a fork is this proxy, which meters the solver's RPC work per session via a
versioned, fleet-uniform cost table (:mod:`.cost_table`) and — when enforcing —
hard-cuts at an integer budget so a too-expensive ``generate_plan`` fails
identically on every validator.

The proxy is a TRUSTED sidecar (not in the untrusted blast radius): its data
plane (``/rpc/...``) faces the solver, while its control plane
(``/control/...``) is reachable only by the validator on a separate network.

Modules:
- ``cost_table``: the versioned consensus cost constant.
- ``proxy``: the aiohttp counting/budget proxy server.
"""
