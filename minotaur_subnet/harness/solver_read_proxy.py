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
import uuid
from dataclasses import dataclass

from minotaur_subnet.chains import registry

logger = logging.getLogger(__name__)

# chain_id -> the proxy/UPSTREAMS chain key (matches the sidecar's UPSTREAMS map
# and the ``/rpc/<session>/<chain>`` path segment). Derived from the chain
# registry's ``slug`` so the proxy route, the read_proxy_manager UPSTREAMS keys,
# and this map cannot drift apart. Wired chains only (== the legacy 4 entries).
CHAIN_NAMES: dict[int, str] = {
    cid: registry.slug(cid)
    for cid in registry.wired_chain_ids()
    if registry.slug(cid)
}


@dataclass(frozen=True)
class ReadProxyConfig:
    """Resolved wiring for routing solver reads through the proxy.

    TWO addresses because the trusted api (control plane) and the untrusted
    solver (data plane) sit on DIFFERENT docker networks: the api reaches the
    proxy on the validator/minotaur net; the solver reaches it on the sealed
    sandbox net (a different IP). A single URL can't serve both.
    """

    url: str  # DATA-plane base the SOLVER dials (e.g. http://172.30.0.5:8645)
    control_url: str  # CONTROL-plane base the API dials (e.g. http://minotaur-rpc-pin-proxy:8645)
    token: str  # control-plane shared secret (sent as X-Control-Token)
    chain_ids: tuple[int, ...]  # chains to route + pin through the proxy
    # Deterministic per-scenario RPC-read budget (integer cost units, metered by
    # the proxy against the versioned cost table). 0 = NOT enforced: the proxy
    # session runs in observe mode and the non-deterministic wall-clock timeout
    # remains the cutoff (today's behavior). >0 = the budget IS the per-scenario
    # cutoff (the wall-clock loosens to a runaway backstop). Defaults to 0 so
    # existing instantiations keep observe semantics.
    budget: int = 0


# The deterministic per-scenario RPC-read budget — a CONSENSUS-UNIFORM CODE
# CONSTANT (like EPOCH_SECONDS / the cost-table version), NOT a per-validator env,
# so the whole fleet enforces the SAME cutoff and folds the SAME value into the
# benchmark pack hash. A bare :stable validator therefore gets the same B as the
# fleet with no env to set. Calibrated generous: the observed per-scenario max via
# the proxy is ~300 reads (cold DAI multi-hop), so 5000 is ~16x headroom — legit
# scenarios always pass; only a runaway loop is cut. Tighten only fleet-wide.
DEFAULT_GENERATE_PLAN_BUDGET = 5000


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
    # Budget: UNSET -> the uniform code constant (default-on, consensus-bound; so a
    # bare :stable validator enforces the SAME B as the fleet with no env). An
    # explicit positive value overrides (dev/tuning — but a non-uniform value splits
    # the pack hash, same discipline as ROUND_ANCHORED_PIN). Explicit 0 disables
    # (observe; emergency only). Invalid/negative -> the constant (fail-safe to the
    # uniform default rather than silently dropping ONE node to observe).
    raw_budget = os.environ.get("SOLVER_READ_PROXY_BUDGET", "").strip()
    if not raw_budget:
        budget = DEFAULT_GENERATE_PLAN_BUDGET
    else:
        try:
            budget = int(raw_budget)
        except ValueError:
            logger.error(
                "SOLVER_READ_PROXY_BUDGET not an int: %r; using default %d",
                raw_budget, DEFAULT_GENERATE_PLAN_BUDGET,
            )
            budget = DEFAULT_GENERATE_PLAN_BUDGET
        if budget < 0:
            budget = DEFAULT_GENERATE_PLAN_BUDGET
    return ReadProxyConfig(
        url=base.rstrip("/"),
        control_url=control.rstrip("/"),
        token=token,
        chain_ids=chains,
        budget=budget,
    )


# ── LIVE champion path (BLIND: keyless + metered, head reads) ────────────────
#
# The benchmark path above pins reads to a fork block for determinism. The LIVE
# champion instead needs LATEST-block reads, but it must STILL be (a) keyless —
# the Alchemy/blockmachine API key stays in the proxy, never in the untrusted
# container — and (b) metered, so a hostile or runaway champion can't run up the
# provider bill (BLIND-3). It reaches the proxy on the dedicated ``live-solver``
# internal net (a DIFFERENT IP than the benchmark-sandbox data plane), whose URL
# ``read_proxy_manager`` exports as ``SOLVER_LIVE_RPC_PROXY`` once it has attached
# the proxy to that net.

# Default per-ORDER live RPC-cost budget (reset before each generate_plan/quote,
# so it bounds a single order, not the champion's whole lifetime). Generous like
# the benchmark constant — legit routing always fits; only a runaway loop is cut.
DEFAULT_LIVE_RPC_BUDGET = DEFAULT_GENERATE_PLAN_BUDGET

# Live proxy session ids are UNIQUE PER RUNTIME (``live-<hex>``), never a fixed
# name: during a hot-swap the new champion's create() overlaps the displaced
# champion's shutdown(), and with a shared id the outgoing close_session() would
# close the session the successor just opened — silently un-metering it (the
# proxy forwards unknown sessions via its anon observe bucket). The ``live-``
# prefix keeps them distinct from the benchmark path's per-run ids.
LIVE_PROXY_SESSION_PREFIX = "live"


def new_live_session_id() -> str:
    """A fresh proxy session id for ONE live-champion runtime instance."""
    return f"{LIVE_PROXY_SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"

# Name of the dedicated ``--internal`` net the live champion + proxy share when
# the feature is on. Both read_proxy_manager (which creates it + attaches the
# proxy) and runtime_solver (which puts the champion on it) default to this, so
# enabling LIVE_SOLVER_RPC_VIA_PROXY alone lands both on the same net — no
# separate LIVE_SOLVER_NETWORK needed. Override both via LIVE_SOLVER_NETWORK.
LIVE_SOLVER_NETWORK_DEFAULT = "live-solver"

_TRUTHY_ENV = {"1", "true", "yes", "on"}


def live_rpc_via_proxy_enabled() -> bool:
    """Whether the live champion should route RPC through the keyless proxy.

    Opt-in (``LIVE_SOLVER_RPC_VIA_PROXY``): default off preserves today's direct
    keyed-RPC behavior, so this ships inert. Enable it TOGETHER with a
    ``live-solver`` ``--internal`` net (``LIVE_SOLVER_NETWORK``) so the champion
    reaches only the proxy — see ``read_proxy_manager`` + ``runtime_solver``.
    """
    return os.environ.get("LIVE_SOLVER_RPC_VIA_PROXY", "").strip().lower() in _TRUTHY_ENV


def live_read_proxy_config() -> ReadProxyConfig | None:
    """Config for routing the LIVE champion's reads through the keyless metered
    proxy, or ``None`` if the feature is disabled / not yet wired.

    Differs from :func:`read_proxy_config` (benchmark) in two ways: the data URL
    is ``SOLVER_LIVE_RPC_PROXY`` (the proxy's live-solver-net IP, since the live
    champion is on a different internal net than benchmark solvers), and the
    session pins NO block (``blocks={}`` at open → byte-transparent head reads).
    The budget still enforces (BLIND-3). Returns ``None`` unless the feature is
    enabled AND ``read_proxy_manager`` has exported the live data URL — so a
    failed proxy attach fails SAFE (champion keeps its direct RPC) rather than
    pointing the champion at an unreachable proxy.
    """
    if not live_rpc_via_proxy_enabled():
        return None
    data = os.environ.get("SOLVER_LIVE_RPC_PROXY", "").strip()
    if not data:
        return None
    control = os.environ.get("SOLVER_READ_PROXY_CONTROL", "").strip() or data
    token = os.environ.get("SOLVER_READ_PROXY_TOKEN", "").strip()
    raw_budget = os.environ.get("LIVE_SOLVER_RPC_BUDGET", "").strip()
    budget = DEFAULT_LIVE_RPC_BUDGET
    if raw_budget:
        try:
            budget = int(raw_budget)
        except ValueError:
            logger.error(
                "LIVE_SOLVER_RPC_BUDGET not an int: %r; using default %d",
                raw_budget, DEFAULT_LIVE_RPC_BUDGET,
            )
            budget = DEFAULT_LIVE_RPC_BUDGET
    if budget < 0:
        budget = DEFAULT_LIVE_RPC_BUDGET
    return ReadProxyConfig(
        url=data.rstrip("/"),
        control_url=control.rstrip("/"),
        token=token,
        chain_ids=tuple(CHAIN_NAMES),
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
    cfg: ReadProxyConfig,
    rpc_map: dict[int, str],
    fork_block: int | dict[int, int],
) -> dict[str, int]:
    """The ``{chain_name: block}`` to pin for the routed chains present in ``rpc_map``.

    ``fork_block`` is either a single int (every routed chain pinned at the same
    block — the Base-only case) or a ``{chain_id: block}`` map (multi-chain
    rounds; each chain pinned at ITS OWN canonical block). With a map, EVERY
    routed chain MUST have an entry — a missing pin is a determinism hole (the
    solver would read that chain unpinned), so raise ``ValueError`` rather than
    silently drop it; the caller (``run_benchmark``) translates that into a
    fail-loud defer.
    """
    out: dict[str, int] = {}
    for cid in rpc_map:
        if cid not in cfg.chain_ids or cid not in CHAIN_NAMES:
            continue
        if isinstance(fork_block, dict):
            if cid not in fork_block:
                raise ValueError(
                    f"build_pin_blocks: no fork pin for routed chain {cid} in "
                    f"{sorted(fork_block)} — refusing to pin it unpinned"
                )
            out[CHAIN_NAMES[cid]] = int(fork_block[cid])
        else:
            out[CHAIN_NAMES[cid]] = int(fork_block)
    return out


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


async def reset_session(cfg: ReadProxyConfig, session_id: str) -> bool:
    """Reset a session's spent budget to 0 (best-effort); ``True`` on success.

    Called before each ``generate_plan`` so every scenario starts with a fresh
    budget ``B`` — making the budget a PER-SCENARIO cutoff that mirrors the
    per-scenario wall-clock timeout it replaces. The blocks/pin are left intact
    (``/control/reset`` only zeros spent + clears exhausted when no ``blocks``
    key is sent). A failed reset is logged and swallowed: it must not crash the
    benchmark (worst case the next scenario continues with carried-over spend,
    which only makes the cutoff stricter, never silently looser). The return
    value lets the LIVE path distinguish "session gone" (e.g. the proxy
    container restarted and dropped its in-memory registry) and re-open it.
    """
    try:
        await asyncio.to_thread(
            _control_post, cfg, "/control/reset", {"session_id": session_id}
        )
        return True
    except Exception as exc:  # noqa: BLE001 - a failed reset must not abort the run
        logger.warning("read-proxy reset failed for session=%s: %s", session_id, exc)
        return False


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
