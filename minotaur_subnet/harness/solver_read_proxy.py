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
    # Deterministic per-scenario RPC-read budget (integer cost units, metered by
    # the proxy against the versioned cost table). 0 = NOT enforced: the proxy
    # session runs in observe mode and the non-deterministic wall-clock timeout
    # remains the cutoff (today's behavior). >0 = the budget IS the per-scenario
    # cutoff (the wall-clock loosens to a runaway backstop). Defaults to 0 so
    # existing instantiations keep observe semantics.
    budget: int = 0


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
      - ``SOLVER_READ_PROXY_BUDGET``: integer per-scenario RPC-read budget. Unset,
        invalid, or ``<=0`` -> 0 (observe mode; wall-clock stays the cutoff). ``>0``
        -> the proxy enforces this budget as the DETERMINISTIC per-scenario cutoff
        and it folds into the benchmark pack hash (consensus-bound).
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
    # Budget: invalid/unset/non-positive -> 0 (observe; inert as a cutoff).
    raw_budget = os.environ.get("SOLVER_READ_PROXY_BUDGET", "").strip()
    try:
        budget = int(raw_budget) if raw_budget else 0
    except ValueError:
        logger.error("SOLVER_READ_PROXY_BUDGET not an int: %r; using 0 (observe)", raw_budget)
        budget = 0
    if budget < 0:
        budget = 0
    return ReadProxyConfig(
        url=base.rstrip("/"),
        control_url=control.rstrip("/"),
        token=token,
        chain_ids=chains,
        budget=budget,
    )


def budget_enforced() -> bool:
    """``True`` iff the proxy is configured AND a positive budget is set.

    When ``True`` the proxy session runs in enforce mode with a deterministic
    integer cutoff, so the wall-clock GENERATE_PLAN timeout is no longer the
    cutoff (it loosens to a runaway backstop) and the budget folds into the
    benchmark pack hash. ``False`` => everything budget-related is inert.
    """
    cfg = read_proxy_config()
    return cfg is not None and cfg.budget > 0


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

    When ``cfg.budget > 0`` the session opens in ``enforce`` mode with that
    budget (the deterministic per-scenario cutoff). Otherwise budget/mode are
    omitted, so the proxy applies its observe defaults (today's behavior).
    """
    body: dict = {"session_id": session_id, "blocks": blocks}
    if cfg.budget > 0:
        body["budget"] = cfg.budget
        body["mode"] = "enforce"
    return await asyncio.to_thread(_control_post, cfg, "/control/open", body)


async def reset_session(cfg: ReadProxyConfig, session_id: str) -> None:
    """Reset a session's spent budget to 0 (best-effort).

    Called before each ``generate_plan`` so every scenario starts with a fresh
    budget ``B`` — making the budget a PER-SCENARIO cutoff that mirrors the
    per-scenario wall-clock timeout it replaces. The blocks/pin are left intact
    (``/control/reset`` only zeros spent + clears exhausted when no ``blocks``
    key is sent). A failed reset is logged and swallowed: it must not crash the
    benchmark (worst case the next scenario continues with carried-over spend,
    which only makes the cutoff stricter, never silently looser).
    """
    try:
        await asyncio.to_thread(
            _control_post, cfg, "/control/reset", {"session_id": session_id}
        )
    except Exception as exc:  # noqa: BLE001 - a failed reset must not abort the run
        logger.warning("read-proxy reset failed for session=%s: %s", session_id, exc)


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


def pack_hash_compute_budget() -> dict | None:
    """The compute-budget record to fold into the benchmark pack hash, or ``None``.

    Folded IFF the budget is the ACTIVE per-scenario cutoff for this round — i.e.
    the proxy is configured (``SOLVER_READ_PROXY``), the round is pinned
    (``ROUND_ANCHORED_PIN``, so :func:`run_benchmark` actually routes reads
    through the proxy), AND a positive budget is set (``SOLVER_READ_PROXY_BUDGET``
    > 0). This mirrors :func:`pack_hash_block_rewrite`'s gating exactly, plus the
    budget>0 condition, so the record is folded under precisely the conditions in
    which the budget enforces.

    Returning ``None`` otherwise keeps the pack hash byte-identical to validators
    not enforcing a budget, so an inert fleet is unaffected. Once a fleet
    enforces, a validator on a different ``B`` (or a different cost-table version,
    or in observe) computes a different pack hash and cannot reach quorum
    (consensus-versioned — same discipline as ROUND_ANCHORED_PIN / the block
    rewrite). Fail-loud, never silent.
    """
    from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
    from minotaur_subnet.harness.rpc_budget_proxy.cost_table import (
        compute_budget_record,
    )

    cfg = read_proxy_config()
    if cfg is not None and round_anchored_pin_enabled() and cfg.budget > 0:
        return compute_budget_record(cfg.budget)
    return None


def generate_plan_recv_timeout(default: float) -> float:
    """The wall-clock recv timeout to apply to a GENERATE_PLAN response.

    When the deterministic budget is the cutoff (:func:`budget_enforced`), the
    wall-clock is NO LONGER the cutoff — it would re-introduce the very cross-host
    non-determinism the budget removes (a slow host/RPC/GC could trip 30s and
    score 0 on validator A but not B, with an identical pack hash). So loosen it
    to a mere runaway backstop (``GENERATE_PLAN_BACKSTOP_SECONDS``, default 300s,
    never below ``default``). When the budget is off, return ``default`` unchanged
    (today's behavior — fully inert).
    """
    if not budget_enforced():
        return default
    try:
        backstop = float(os.environ.get("GENERATE_PLAN_BACKSTOP_SECONDS", "300"))
    except ValueError:
        backstop = 300.0
    return max(default, backstop)
