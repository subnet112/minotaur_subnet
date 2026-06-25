"""Orchestrator-side client for routing a solver's READS through the block-pin
proxy (the split-fork architecture).

When ``SOLVER_READ_PROXY`` is set, :func:`run_benchmark` routes the (untrusted)
solver's per-chain reads at the round's ``fork_block`` through the trusted
block-pin proxy sidecar instead of the Anvil fork — collapsing each cold quote
(~N serial slot fetches) into ONE upstream round-trip (measured ~30ms vs ~1.4s)
and forcing every read to exactly the scored block, deterministically on any
archive provider. The simulator's EXECUTION path is unchanged (it keeps its
Anvil fork; only the solver's reads move).

This module is the trusted CONTROL-plane client: it opens a per-run session on
the proxy with the round's pinned block(s) (authenticated with the shared
control token) and yields the proxy data-plane URL for each routed chain. It is
INERT unless ``SOLVER_READ_PROXY`` is set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# chain_id -> the proxy/UPSTREAMS chain key (matches the sidecar's UPSTREAMS map
# and the ``/rpc/<session>/<chain>`` path segment).
CHAIN_NAMES: dict[int, str] = {1: "eth", 31337: "eth", 8453: "base", 964: "btevm"}


@dataclass(frozen=True)
class ReadProxyConfig:
    """Resolved wiring for routing solver reads through the proxy.

    TWO addresses because the trusted api (control plane) and the untrusted
    solver (data plane) sit on DIFFERENT docker networks: the api reaches the
    proxy on the validator/minotaur net; the solver reaches it on the sealed
    sandbox net (a different IP). A single URL can't serve both.
    """

    url: str  # DATA-plane base the SOLVER dials (e.g. http://172.30.0.5:8645)
    control_url: str  # CONTROL-plane base the API dials (e.g. http://rpc-pin-proxy:8645)
    token: str  # control-plane shared secret (sent as X-Control-Token)
    chain_ids: tuple[int, ...]  # chains to route + pin through the proxy


def read_proxy_config() -> ReadProxyConfig | None:
    """Resolve the proxy wiring from the environment, or ``None`` if disabled.

    Env:
      - ``SOLVER_READ_PROXY``: the proxy base URL. Unset/empty -> disabled (inert).
      - ``SOLVER_READ_PROXY_TOKEN``: control-plane shared secret (matches the
        proxy's ``CONTROL_TOKEN``).
      - ``SOLVER_READ_PROXY_CHAINS``: comma-separated chain ids to route + pin
        (default ``8453`` — the Base round anchor). A single ``fork_block`` pins
        these; multi-chain with distinct block heights needs per-chain blocks
        (a future extension — see :func:`build_pin_blocks`).
    """
    base = os.environ.get("SOLVER_READ_PROXY", "").strip()
    if not base:
        return None
    # The api (control) and the solver (data) are on different networks, so the
    # control-plane address may differ from the solver-facing one. Defaults to
    # the data URL when they coincide (e.g. local testnet, single network).
    control = os.environ.get("SOLVER_READ_PROXY_CONTROL", "").strip() or base
    token = os.environ.get("SOLVER_READ_PROXY_TOKEN", "").strip()
    raw = os.environ.get("SOLVER_READ_PROXY_CHAINS", "8453").strip()
    try:
        chains = tuple(int(c) for c in raw.split(",") if c.strip())
    except ValueError:
        logger.error("SOLVER_READ_PROXY_CHAINS not a csv of ints: %r; using (8453,)", raw)
        chains = (8453,)
    return ReadProxyConfig(
        url=base.rstrip("/"),
        control_url=control.rstrip("/"),
        token=token,
        chain_ids=chains,
    )


def build_pin_blocks(
    cfg: ReadProxyConfig, rpc_map: dict[int, str], fork_block: int
) -> dict[str, int]:
    """The ``{chain_name: block}`` to pin: the routed chains present in ``rpc_map``,
    each at ``fork_block``.

    NOTE: a single ``fork_block`` is correct only while the routed chains share
    one anchor (today: Base-only). Routing multiple chains with distinct heights
    would need a per-chain block map — guarded by the single-anchor default.
    """
    return {
        CHAIN_NAMES[cid]: fork_block
        for cid in rpc_map
        if cid in cfg.chain_ids and cid in CHAIN_NAMES
    }


def proxy_rpc_url(cfg: ReadProxyConfig, session_id: str, chain_id: int) -> str:
    """The proxy data-plane URL the solver dials for ``chain_id``."""
    return f"{cfg.url}/rpc/{session_id}/{CHAIN_NAMES.get(chain_id, str(chain_id))}"


def _control_post(cfg: ReadProxyConfig, path: str, body: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if cfg.token:
        headers["X-Control-Token"] = cfg.token
    req = urllib.request.Request(
        cfg.control_url + path, data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted internal URL)
        return json.loads(resp.read())


async def open_session(cfg: ReadProxyConfig, session_id: str, blocks: dict[str, int]) -> dict:
    """Open/replace a proxy session pinned to ``blocks`` ({chain_name: block}).

    Runs the (blocking) control POST off the event loop. Raises on transport or
    auth failure — the caller fails the run loud rather than silently falling
    back to the unpinned Anvil fork (which would re-introduce non-determinism).
    """
    return await asyncio.to_thread(
        _control_post, cfg, "/control/open", {"session_id": session_id, "blocks": blocks}
    )


async def close_session(cfg: ReadProxyConfig, session_id: str) -> None:
    """Best-effort close — never fails the run (the proxy also caps its registry)."""
    try:
        await asyncio.to_thread(
            _control_post, cfg, "/control/close", {"session_id": session_id}
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not raise
        logger.warning("read-proxy close failed for session=%s: %s", session_id, exc)


def pack_hash_block_rewrite() -> dict | None:
    """The block-rewrite record to fold into the benchmark pack hash, or ``None``.

    Folded IFF this round's scoring actually routes solver reads through the
    proxy — i.e. the proxy is configured (``SOLVER_READ_PROXY``) AND the round is
    pinned (``ROUND_ANCHORED_PIN``), exactly the condition under which
    :func:`run_benchmark` repoints reads to the proxy. Returning ``None``
    otherwise keeps the pack hash byte-identical to validators not routing
    through the proxy, so a non-proxy fleet is unaffected; once a fleet routes, a
    divergent ``BLOCK_REWRITE_VERSION`` yields a different hash and cannot reach
    quorum (consensus-versioned, same discipline as the compute budget).
    """
    from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
    from minotaur_subnet.harness.rpc_budget_proxy.rewrite_table import (
        rewrite_table_record,
    )

    if read_proxy_config() is not None and round_anchored_pin_enabled():
        return rewrite_table_record()
    return None
