"""Deterministic block-tag rewrite rules — a Minotaur consensus constant.

The block-pin proxy forces every read an UNTRUSTED solver makes to a FIXED
historical block (the round's ``fork_block``) before forwarding it to the
validator's configured upstream archive RPC. That makes the solver quote/route
against exactly the state it is *scored* against, identically on every validator
and on ANY archive provider (a fixed historical block has one canonical state
root, so Alchemy/Infura/QuickNode/own-node all return byte-identical results).

CONSENSUS SURFACE
=================
Which methods carry a block argument, and where, MUST be byte-identical across
the fleet — two validators that rewrite differently would pin to different state
and silently disagree. So this table is **versioned** (:data:`BLOCK_REWRITE_VERSION`);
any change is a protocol change. The version is folded into the benchmark pack
hash so a divergent table fails consensus *loudly* rather than mis-pinning.

The rewrite ENFORCES the pin — it does not trust the solver to pass a block.
Even the reference BaselineSwapSolver issues plain ``.call()`` with no
``block_identifier`` (defaults to ``latest``, a moving head) — exactly the
non-determinism this kills. An *absent* block arg is treated as ``latest`` and
forced to the pin too.
"""
from __future__ import annotations

from typing import Any

BLOCK_REWRITE_VERSION = "v1"

# Read methods whose block tag is a positional param at a fixed index.
BLOCK_PARAM_INDEX: dict[str, int] = {
    "eth_call": 1,
    "eth_getBalance": 1,
    "eth_getCode": 1,
    "eth_getTransactionCount": 1,
    "eth_getStorageAt": 2,
    "eth_getProof": 2,
    "eth_getBlockByNumber": 0,
}

# Intercepted: the proxy answers eth_blockNumber with the PIN itself, so the
# solver (and any web3 latest->blockNumber resolution) sees the pin as head and
# cannot compute a different "latest" to read at.
INTERCEPT_BLOCKNUMBER = "eth_blockNumber"

# The block range of eth_getLogs is clamped to [B, B] (point-in-time) so the
# solver can't scan a wide/moving range (heavy + non-deterministic).
GETLOGS = "eth_getLogs"

# State-changing / fork-cheat / chain-mutating methods are REJECTED: the read
# proxy is read-only by contract, the solver has no business mutating, and an
# archive upstream can't execute writes anyway.
REJECT_METHODS: frozenset[str] = frozenset({
    "eth_sendTransaction",
    "eth_sendRawTransaction",
    "eth_submitWork",
    "eth_submitHashrate",
})
REJECT_PREFIXES: tuple[str, ...] = ("anvil_", "evm_", "hardhat_", "ots_")


def _is_reject(method: str) -> bool:
    return method in REJECT_METHODS or any(method.startswith(p) for p in REJECT_PREFIXES)


def classify(method: Any) -> str:
    """Classify a method: ``reject`` | ``blocknumber`` | ``getlogs`` |
    ``rewrite`` | ``passthrough``. A non-string method is passthrough (the
    upstream produces the canonical error)."""
    if not isinstance(method, str):
        return "passthrough"
    if _is_reject(method):
        return "reject"
    if method == INTERCEPT_BLOCKNUMBER:
        return "blocknumber"
    if method == GETLOGS:
        return "getlogs"
    if method in BLOCK_PARAM_INDEX:
        return "rewrite"
    return "passthrough"


def rewrite_params(method: str, params: Any, block_hex: str) -> Any:
    """Return ``params`` with the block tag FORCED to ``block_hex``.

    For a positional-block method the canonical index is set to ``block_hex``,
    PADDING the list if the block arg was omitted (absence == ``latest`` == must
    be pinned). For ``eth_getLogs`` the filter's ``fromBlock``/``toBlock`` are
    both set to ``block_hex`` (and a mutually-exclusive ``blockHash`` dropped).
    """
    if method == GETLOGS:
        f = (
            dict(params[0])
            if (isinstance(params, list) and params and isinstance(params[0], dict))
            else {}
        )
        f["fromBlock"] = block_hex
        f["toBlock"] = block_hex
        f.pop("blockHash", None)
        return [f]
    idx = BLOCK_PARAM_INDEX[method]
    p = list(params) if isinstance(params, list) else []
    while len(p) <= idx:
        p.append(None)
    p[idx] = block_hex
    return p


def rewrite_single(req: Any, block_hex: str) -> tuple[str, Any]:
    """Process one JSON-RPC request object against the pinned ``block_hex``.

    Returns ``(action, payload)``:
      - ``("forward", req)``       — forward this (possibly block-rewritten) request.
      - ``("blocknumber", block)`` — eth_blockNumber: the proxy synthesizes the result.
      - ``("reject", method)``     — a state-changing/cheat method: deterministic error.
    """
    if not isinstance(req, dict):
        return ("forward", req)  # let the upstream produce the canonical error
    method = req.get("method")
    kind = classify(method)
    if kind == "reject":
        return ("reject", method)
    if kind == "blocknumber":
        return ("blocknumber", block_hex)
    if kind in ("rewrite", "getlogs"):
        new = dict(req)
        new["params"] = rewrite_params(method, req.get("params"), block_hex)
        return ("forward", new)
    return ("forward", req)  # block-independent (eth_chainId, net_version, ...)


def rewrite_table_record() -> dict:
    """Canonical, hashable record of the active rewrite table — folded into the
    benchmark pack hash so a divergent table can't reach quorum."""
    return {
        "version": BLOCK_REWRITE_VERSION,
        "block_param_index": dict(sorted(BLOCK_PARAM_INDEX.items())),
        "intercept_blocknumber": INTERCEPT_BLOCKNUMBER,
        "getlogs": GETLOGS,
        "reject_methods": sorted(REJECT_METHODS),
        "reject_prefixes": list(REJECT_PREFIXES),
    }
