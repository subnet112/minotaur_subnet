"""Deterministic JSON-RPC counting/budget proxy server (aiohttp).

ROLE
====
A TRUSTED validator sidecar that sits between an untrusted benchmark solver
container and the Anvil fork(s). The solver dials this proxy instead of Anvil
directly (e.g. ``ANVIL_RPC_URL=http://<proxy>:<port>/rpc/<session>/<chain>``).
The proxy meters the solver's RPC "work" per session using the versioned cost
table (:mod:`.cost_table`) and, when enforcing, hard-cuts at a fleet-uniform
integer budget.

CONSENSUS SURFACE
=================
Two inputs determine where a solver gets cut off, and BOTH must be uniform
across the validator fleet for benchmark scoring to be deterministic:

  1. the **cost table** (:mod:`.cost_table`, versioned), and
  2. the per-session **budget** (an integer handed in via ``/control/open``).

Given the same session, the same call sequence, and the same cost table, the
cumulative cost is identical on every validator, so the cut-off point is
identical. This deterministic budget replaces a non-deterministic wall-clock
timeout (which depended on CPU/RPC latency and could pass on one validator and
fail on another).

MODES
=====
- ``observe`` (default): forward every request to the upstream UNCHANGED,
  return the upstream bytes UNCHANGED, and accumulate cost. NEVER cut off.
  Used to collect cost data so a sane budget can be chosen before enforcing.
- ``enforce``: forward while ``spent + cost <= budget``; once a request would
  exceed the budget, do NOT forward it and instead return a deterministic
  JSON-RPC error (code ``-32099``, message ``MINOTAUR_BUDGET_EXCEEDED``). The
  session is marked ``exhausted`` so ALL subsequent calls in that session also
  return the error — once over budget, stay over (deterministic).

TRANSPARENCY & FAIL-LOUD
========================
Below budget the proxy is byte-for-byte transparent: it forwards the raw
request body and returns the raw response body, preserving upstream HTTP
errors/timeouts, so a solver cannot observe the proxy by inspecting numbers or
whitespace. When budget is hit it fails LOUD with a well-formed JSON-RPC error
matching the request shape — never a silent pass-through to direct Anvil.

Two deliberate exceptions, both value-preserving:

1. Immutable per-chain constants (``eth_chainId``, ``net_version``) are
   fetched from the upstream ONCE per chain and answered locally thereafter.
   web3.py re-validates the chain id around nearly every call, which made
   these ~2/3 of all upstream traffic; the value cannot differ by block or
   time, so the solver-visible VALUE is identical either way.

2. BLOCK-PINNED reads are cached per ``(chain, block, method, params)``. A
   fixed historical block has exactly ONE canonical state, so after the
   rewrite forces the pin, the upstream's answer is fully determined by the
   request — replaying it is not an approximation. This is where the real
   volume is: every challenger in a round quotes the SAME scenarios at the
   SAME fork block, re-reading state the previous session already fetched.
   Only rewrite-table-pinned methods are cached (``eth_call``,
   ``eth_getStorageAt``, ..., ``eth_getLogs``); anything block-independent
   and non-constant (e.g. ``eth_gasPrice``) always forwards. Budget metering
   is charged BEFORE cache lookup, so a session's ``spent`` sequence — the
   consensus-relevant meter — is identical whether or not a value was cached.

NETWORK ISOLATION (caller's responsibility)
===========================================
The ``/rpc/...`` data plane faces the untrusted solver; the ``/control/...``
plane must be reachable only by the trusted validator. This server implements
both; wiring them onto separate networks/interfaces is the caller's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from ._persist import SnapshotScheduler, load_snapshot
from .cost_table import batch_cost, request_cost
from .rewrite_table import classify, rewrite_single

logger = logging.getLogger(__name__)

# Deterministic budget-exceeded error (consensus-visible to the solver).
BUDGET_EXCEEDED_CODE = -32099
BUDGET_EXCEEDED_MESSAGE = "MINOTAUR_BUDGET_EXCEEDED"

# Bucket name used to count calls that arrive with an unknown session id.
ANON_SESSION = "__anon__"

# Methods whose result is an immutable per-chain constant: answered from a
# proxy-lifetime cache after one upstream fetch (see module docstring).
IMMUTABLE_CHAIN_METHODS = frozenset({"eth_chainId", "net_version"})

# Kill switch for BOTH response caches (chain constants + pinned reads):
# RPC_PROXY_RESPONSE_CACHE=0 restores the fully-transparent forward-everything
# behavior of the pre-cache proxy, without an image rollback.
_FALSEY_ENV = {"0", "false", "no", "off"}


def _response_cache_enabled_from_env() -> bool:
    return os.environ.get(
        "RPC_PROXY_RESPONSE_CACHE", "1"
    ).strip().lower() not in _FALSEY_ENV

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8645
DEFAULT_MODE = "observe"
DEFAULT_BUDGET = 1000
# Bound the session registry: per-run session ids the orchestrator may not
# explicitly close can't grow unbounded on a long-lived proxy (oldest evicted).
MAX_SESSIONS = 64

# How long to wait on the upstream Anvil before surfacing the error to the
# solver. This is a transport timeout, NOT the deterministic budget — it never
# affects metering; it only governs how an unreachable upstream is reported.
UPSTREAM_TIMEOUT_SECONDS = 30
# Bound the proxy's concurrent upstream connections. At BENCHMARK_CONCURRENCY=K, K
# solver runtimes each issue several concurrent reads; without a cap the proxy can
# storm the archive provider into rate-limit timeouts — which vary per-validator and
# would reintroduce non-determinism. Default 24 (~K=4 x a few reads); tune to the RPC tier.
try:
    UPSTREAM_MAX_CONCURRENCY = max(1, int(os.environ.get('RPC_PROXY_UPSTREAM_MAX_CONCURRENCY', '24')))
except ValueError:
    UPSTREAM_MAX_CONCURRENCY = 24


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Bounds for the pinned-read cache. A round pins ONE block per chain, so the
# working set is a handful of (chain, block) groups; old blocks age out as
# rounds advance. Entries are capped per block (insert stops at the cap — the
# set of reads at one block is finite, eviction churn buys nothing), and
# oversized results are never cached so one giant eth_getLogs/eth_getCode
# response can't balloon the proxy.
PIN_CACHE_MAX_BLOCKS = _env_int("RPC_PROXY_PIN_CACHE_MAX_BLOCKS", 4)
PIN_CACHE_MAX_ENTRIES_PER_BLOCK = _env_int(
    "RPC_PROXY_PIN_CACHE_MAX_ENTRIES_PER_BLOCK", 250_000
)
PIN_CACHE_MAX_RESULT_BYTES = _env_int(
    "RPC_PROXY_PIN_CACHE_MAX_RESULT_BYTES", 131_072
)


class PinCache:
    """Bounded ``(chain, block) -> {request-key: result}`` cache for pinned reads.

    Single-event-loop use only (like the session registry) — plain dicts, no
    locking. Block groups are LRU-evicted whole: state at a block never changes,
    so entries are only ever dropped because their block fell out of rotation.
    """

    def __init__(
        self,
        max_blocks: int = PIN_CACHE_MAX_BLOCKS,
        max_entries_per_block: int = PIN_CACHE_MAX_ENTRIES_PER_BLOCK,
        max_result_bytes: int = PIN_CACHE_MAX_RESULT_BYTES,
    ) -> None:
        self.max_blocks = max_blocks
        self.max_entries_per_block = max_entries_per_block
        self.max_result_bytes = max_result_bytes
        # (chain, block_hex) -> {key: result}; dict preserves insertion order,
        # re-inserting on access gives LRU over block groups.
        self._blocks: dict[tuple[str, str], dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(method: str, params: Any) -> str:
        """Canonical request key: method + params with dict keys sorted, so two
        solvers spelling the same read differently share one entry."""
        return f"{method}:{json.dumps(params, sort_keys=True, separators=(',', ':'))}"

    def _group(self, chain: str, block_hex: str) -> dict[str, Any]:
        gk = (chain, block_hex)
        group = self._blocks.get(gk)
        if group is None:
            group = self._blocks[gk] = {}
        else:  # refresh LRU position
            self._blocks[gk] = self._blocks.pop(gk)
        while len(self._blocks) > self.max_blocks:
            evicted = next(iter(self._blocks))
            self._blocks.pop(evicted)
            logger.info("pin-cache: evicted block group %s", evicted)
        return group

    def get(self, chain: str, block_hex: str, key: str) -> tuple[bool, Any]:
        """``(hit, result)`` — the flag disambiguates a cached null result."""
        group = self._group(chain, block_hex)
        if key in group:
            self.hits += 1
            return True, group[key]
        self.misses += 1
        return False, None

    def put_from_response(
        self, chain: str, block_hex: str, key: str, resp: web.StreamResponse
    ) -> None:
        """Cache a forwarded response's ``result``; never raises.

        Only a 200 carrying a single JSON-RPC object with a ``result`` and no
        ``error`` fills the cache — upstream errors, JSON-RPC errors (e.g. a
        transient rate-limit), and oversized bodies keep forwarding.
        """
        body = getattr(resp, "body", None)
        if resp.status != 200 or not body or len(body) > self.max_result_bytes:
            return
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        if not isinstance(data, dict) or data.get("error") is not None:
            return
        if "result" not in data:
            return
        group = self._group(chain, block_hex)
        if len(group) >= self.max_entries_per_block and key not in group:
            return  # full: keep serving what we have, forward the rest
        group[key] = data["result"]

    def stats(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "blocks": len(self._blocks),
            "entries": sum(len(g) for g in self._blocks.values()),
        }

    def snapshot(self) -> list[list[Any]]:
        """On-loop copy for persistence: ``[[chain, block_hex, {key: result}], ...]``
        in LRU order (JSON has no tuple keys, so the ``(chain, block)`` key is
        flattened into the row). Inner groups are copied so the JSON encode can
        run in a worker thread without racing further writes."""
        return [[chain, block, dict(group)]
                for (chain, block), group in self._blocks.items()]

    def restore(self, payload: Any) -> int:
        """Rebuild ``_blocks`` from :meth:`snapshot` output, honouring the current
        ``max_blocks`` / ``max_entries_per_block`` bounds; returns the entry count
        loaded. Tolerant of a malformed payload (bad rows are skipped) — worst
        case a partial warm cache, never a crash."""
        self._blocks = {}
        if not isinstance(payload, list):
            return 0
        for row in payload[-self.max_blocks:]:  # keep the most-recent block groups
            if not (isinstance(row, list) and len(row) == 3):
                continue
            chain, block, group = row
            if not isinstance(group, dict):
                continue
            if len(group) > self.max_entries_per_block:
                group = dict(list(group.items())[:self.max_entries_per_block])
            self._blocks[(str(chain), str(block))] = group
        return sum(len(g) for g in self._blocks.values())


class Session:
    """Per-benchmark-session meter.

    A "session" corresponds to one solver run (e.g. one ``generate_plan``
    invocation, or a sequence of scenarios under one budget). ``spent`` is the
    cumulative cost; ``peak`` is the high-water mark of ``spent`` (useful in
    observe mode to size a budget). Once ``exhausted`` is set in enforce mode
    it stays set until an explicit ``/control/reset`` — deterministic.
    """

    __slots__ = (
        "session_id", "budget", "mode", "spent", "exhausted", "peak", "blocks",
    )

    def __init__(
        self,
        session_id: str,
        budget: int,
        mode: str,
        blocks: dict[str, str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.budget = int(budget)
        self.mode = mode
        self.spent = 0
        self.exhausted = False
        self.peak = 0
        # Per-chain pinned block (hex, 0x-prefixed). When a block is set for a
        # chain, every read on that chain is rewritten to it before forwarding —
        # the block-pin half of the proxy. Empty = byte-transparent (budget-only).
        self.blocks: dict[str, str] = dict(blocks or {})

    def to_record(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "budget": self.budget,
            "mode": self.mode,
            "spent": self.spent,
            "exhausted": self.exhausted,
            "peak": self.peak,
            "blocks": dict(self.blocks),
        }


def _normalize_mode(mode: Any) -> str:
    """Coerce a mode value to ``observe`` or ``enforce`` (default observe)."""
    m = str(mode or "").strip().lower()
    return "enforce" if m == "enforce" else "observe"


def _normalize_blocks(raw: Any) -> tuple[dict[str, str], str | None]:
    """Coerce a ``{chain: block}`` map to ``{chain: "0x<hex>"}``.

    ``block`` may be an int or a hex/decimal string. ``None``/empty -> ({}, None)
    (no pinning). A non-mapping or an unparseable/negative block -> ({}, error).
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "blocks must be a {chain: block} object"
    out: dict[str, str] = {}
    for chain, block in raw.items():
        try:
            if isinstance(block, str):
                n = int(block, 16) if block.lower().startswith("0x") else int(block)
            else:
                n = int(block)
        except (TypeError, ValueError):
            return {}, f"invalid block for chain {chain!r}: {block!r}"
        if n < 0:
            return {}, f"negative block for chain {chain!r}"
        out[str(chain)] = hex(n)
    return out, None


class BudgetProxy:
    """The counting/budget proxy application.

    Holds the upstream URLs, the session registry, and the aiohttp client used
    to talk to the upstreams. All session mutation happens on the single
    asyncio event loop; a plain dict is therefore safe for storage. The
    spend-decision for a single session is made *synchronously* (no ``await``)
    before any forward, so two concurrent calls to the same session cannot both
    slip under budget.
    """

    def __init__(
        self,
        upstreams: dict[str, str],
        *,
        default_mode: str = DEFAULT_MODE,
        default_budget: int = DEFAULT_BUDGET,
        control_token: str | None = None,
        response_cache_enabled: bool = True,
    ) -> None:
        if not upstreams:
            raise ValueError("BudgetProxy requires at least one upstream URL")
        # Preserve insertion order; the first upstream is the default chain.
        self.upstreams: dict[str, str] = dict(upstreams)
        self._default_chain: str = next(iter(self.upstreams))
        self.default_mode = _normalize_mode(default_mode)
        self.default_budget = int(default_budget)
        # Shared secret guarding the control plane. When set, /control/* requires
        # an X-Control-Token header matching it — so an untrusted solver that can
        # reach the data plane on the SAME port cannot open/reset/close sessions
        # (it can neither override its pin nor reset its budget). Empty = no auth
        # (dev/local; the standalone-validation deploy ran without it).
        self.control_token: str = str(control_token or "")
        self.sessions: dict[str, Session] = {}
        self._client: ClientSession | None = None
        # (chain, method) -> cached "result" for IMMUTABLE_CHAIN_METHODS.
        # Proxy-lifetime: an UPSTREAMS repoint requires a restart, which drops it.
        self._chain_constants: dict[tuple[str, str], Any] = {}
        # Deterministic pinned-read cache, shared across sessions (all sessions
        # at one fork block read the same canonical state).
        self._pin_cache = PinCache()
        # False = RPC_PROXY_RESPONSE_CACHE=0 rollback: skip both caches, forward
        # everything (metering/pinning unaffected — they never depended on them).
        self.response_cache_enabled = bool(response_cache_enabled)
        # Optional disk persistence for the pin cache: warm-load on startup and
        # snapshot on a cadence so the api's rm+run proxy recreate (on every
        # update) skips the cold re-fetch storm. Inert unless the env path is set.
        self._pin_persist_path = os.environ.get(
            "RPC_PROXY_PIN_CACHE_PERSIST_PATH", "").strip()
        self._pin_snapshotter = SnapshotScheduler(
            self._pin_persist_path,
            _env_int("RPC_PROXY_PIN_CACHE_PERSIST_INTERVAL", 300),
            self._pin_cache.snapshot,
        )

    # -- lifecycle ---------------------------------------------------------

    async def _on_startup(self, _app: web.Application) -> None:
        self._client = ClientSession(
            timeout=ClientTimeout(total=UPSTREAM_TIMEOUT_SECONDS),
            connector=TCPConnector(limit=UPSTREAM_MAX_CONCURRENCY),
        )
        logger.info(
            "rpc_budget_proxy started: chains=%s default_chain=%s mode=%s budget=%d",
            list(self.upstreams),
            self._default_chain,
            self.default_mode,
            self.default_budget,
        )
        if self._pin_snapshotter.enabled and self.response_cache_enabled:
            payload = load_snapshot(self._pin_persist_path)
            if payload is not None:
                n = self._pin_cache.restore(payload)
                logger.info(
                    "pin-cache warm-loaded %d entries from %s",
                    n, self._pin_persist_path,
                )
            await self._pin_snapshotter.start()

    async def _on_cleanup(self, _app: web.Application) -> None:
        await self._pin_snapshotter.stop()  # final snapshot + cancel periodic task
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- upstream selection ------------------------------------------------

    def _upstream_for(self, chain: str | None) -> str | None:
        """Resolve a chain name to an upstream URL.

        An absent/empty chain selects the default (first configured) upstream.
        An unknown non-empty chain returns ``None`` (handled as a 400).
        """
        if not chain:
            return self.upstreams[self._default_chain]
        return self.upstreams.get(chain)

    # -- metering ----------------------------------------------------------

    def _charge(self, session: Session, cost: int) -> bool:
        """Synchronously decide whether a request may proceed, and meter it.

        Returns ``True`` if the request should be forwarded, ``False`` if it
        must be rejected with a budget-exceeded error.

        This method contains NO ``await``: it runs to completion atomically on
        the event loop, so concurrent calls to the same session are serialized
        at the decision point and cannot both pass under budget.

        - observe mode: always returns True, always accumulates cost.
        - enforce mode: if already exhausted -> False (stay exhausted). Else if
          ``spent + cost > budget`` -> mark exhausted, return False (do NOT
          spend the cost that didn't run). Else spend and return True.
        """
        if session.mode == "observe":
            session.spent += cost
            if session.spent > session.peak:
                session.peak = session.spent
            return True

        # enforce
        if session.exhausted:
            return False
        if session.spent + cost > session.budget:
            session.exhausted = True
            return False
        session.spent += cost
        if session.spent > session.peak:
            session.peak = session.spent
        return True

    # -- data plane: /rpc/<session_id>[/<chain>] ---------------------------

    async def handle_rpc(self, request: web.Request) -> web.StreamResponse:
        session_id = request.match_info.get("session_id", "")
        chain = request.match_info.get("chain") or None

        upstream = self._upstream_for(chain)
        if upstream is None:
            return self._json_error_response(
                None,
                code=-32602,
                message=f"unknown chain '{chain}'",
                http_status=400,
            )

        raw_body = await request.read()
        parsed, parse_ok = _safe_parse(raw_body)

        # Compute cost from the parsed body. An unparseable body is charged the
        # default cost (one unit) so it still consumes budget rather than
        # slipping through free; it is forwarded so the upstream can produce the
        # canonical JSON-RPC parse error.
        cost, is_batch, ids = _cost_and_shape(parsed, parse_ok)

        session = self.sessions.get(session_id)
        if session is None:
            # Unknown session: do NOT break a misconfigured run. Count under an
            # anonymous bucket (observe semantics) and forward transparently.
            anon = self.sessions.get(ANON_SESSION)
            if anon is None:
                anon = Session(ANON_SESSION, self.default_budget, "observe")
                self.sessions[ANON_SESSION] = anon
            anon.spent += cost
            if anon.spent > anon.peak:
                anon.peak = anon.spent
            logger.warning(
                "rpc for unknown session_id=%r (chain=%s cost=%d) -> anon bucket "
                "spent=%d; forwarding transparently",
                session_id,
                chain or self._default_chain,
                cost,
                anon.spent,
            )
            return await self._forward(upstream, raw_body, request)

        # Atomic spend decision (no await before this returns).
        allowed = self._charge(session, cost)

        if not allowed:
            logger.info(
                "BUDGET_EXCEEDED session=%s mode=%s spent=%d budget=%d cost=%d "
                "batch=%s -> deterministic error",
                session_id,
                session.mode,
                session.spent,
                session.budget,
                cost,
                is_batch,
            )
            return self._budget_exceeded_response(is_batch, ids)

        logger.info(
            "rpc session=%s chain=%s cost=%d spent=%d/%d mode=%s batch=%s",
            session_id,
            chain or self._default_chain,
            cost,
            session.spent,
            session.budget,
            session.mode,
            is_batch,
        )

        # Immutable per-chain constants: answer from the cache (or fetch-and-fill
        # on the first call). Placed AFTER the budget decision so an exhausted
        # enforce session still gets the deterministic budget error, and BEFORE
        # the block-pin because these methods are block-independent.
        if self.response_cache_enabled and parse_ok and isinstance(parsed, dict):
            method = parsed.get("method")
            if method in IMMUTABLE_CHAIN_METHODS:
                chain_key = chain or self._default_chain
                cached = self._chain_constants.get((chain_key, method))
                if cached is not None:
                    return web.json_response(
                        {"jsonrpc": "2.0", "id": parsed.get("id"), "result": cached}
                    )
                resp = await self._forward(upstream, raw_body, request)
                self._maybe_cache_constant(chain_key, method, resp)
                return resp

        # Block-pin: when this session has a pinned block for the chain, FORCE
        # every read to it (rewriting the request body) so the untrusted solver
        # reads exactly the scored state — deterministic on any archive upstream.
        # No block configured for the chain -> byte-transparent raw forward
        # (budget-only / legacy mode).
        block_hex = session.blocks.get(chain or self._default_chain)
        if block_hex is not None and parse_ok:
            if isinstance(parsed, dict):
                return await self._pinned_single(
                    upstream, parsed, block_hex, chain or self._default_chain, request
                )
            pinned = self._block_pin(parsed, block_hex)
            if isinstance(pinned, web.StreamResponse):
                return pinned  # synthetic: rejected mixed batch
            return await self._forward(upstream, pinned, request)  # rewritten body

        return await self._forward(upstream, raw_body, request)

    async def _pinned_single(
        self,
        upstream: str,
        parsed: dict,
        block_hex: str,
        chain_key: str,
        request: web.Request,
    ) -> web.StreamResponse:
        """Handle one pinned single (non-batch) request: rewrite, then serve
        pinned reads from the :class:`PinCache` (fill on miss).

        Same rewrite semantics as the batch path; the cache only ever sees a
        request AFTER the pin was forced, so a key can't alias two blocks.
        """
        action, payload = rewrite_single(parsed, block_hex)
        req_id = parsed.get("id")
        if action == "blocknumber":
            return web.json_response(
                {"jsonrpc": "2.0", "id": req_id, "result": payload}
            )
        if action == "reject":
            return self._json_error_response(
                req_id,
                code=-32601,
                message=f"method {payload!r} not allowed (read-only block-pinned proxy)",
                http_status=200,
            )

        method = payload.get("method") if isinstance(payload, dict) else None
        cache_key = None
        if (
            self.response_cache_enabled
            and isinstance(method, str)
            and classify(method) in ("rewrite", "getlogs")
        ):
            cache_key = PinCache.key(method, payload.get("params"))
            hit, result = self._pin_cache.get(chain_key, block_hex, cache_key)
            if hit:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": req_id, "result": result}
                )
        resp = await self._forward(upstream, json.dumps(payload).encode(), request)
        if cache_key is not None:
            self._pin_cache.put_from_response(chain_key, block_hex, cache_key, resp)
        return resp

    async def _forward(
        self, upstream: str, raw_body: bytes, request: web.Request
    ) -> web.StreamResponse:
        """Forward the raw body to the upstream and relay the raw response.

        Byte-for-byte transparent: we send ``raw_body`` unchanged and return
        the upstream status + body bytes unchanged (no reserialization). The
        upstream content-type is preserved. Connection failures/timeouts to the
        upstream surface as a 502 with a JSON-RPC-shaped error (fail-loud, never
        a silent success).
        """
        assert self._client is not None, "client session not started"
        content_type = request.headers.get("Content-Type", "application/json")
        try:
            async with self._client.post(
                upstream,
                data=raw_body,
                headers={"Content-Type": content_type},
            ) as resp:
                body = await resp.read()
                resp_ct = resp.headers.get("Content-Type", "application/json")
                return web.Response(
                    body=body,
                    status=resp.status,
                    content_type=_content_type_only(resp_ct),
                )
        except asyncio.TimeoutError:
            logger.error("upstream timeout forwarding to %s", upstream)
            return self._json_error_response(
                None, code=-32000, message="upstream timeout", http_status=504
            )
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            logger.error("upstream error forwarding to %s: %s", upstream, exc)
            return self._json_error_response(
                None, code=-32000, message="upstream unreachable", http_status=502
            )

    def _maybe_cache_constant(
        self, chain_key: str, method: str, resp: web.StreamResponse
    ) -> None:
        """Cache the ``result`` of a successful constant fetch; never raises.

        Only a 200 with a parseable single JSON-RPC object carrying a non-null
        ``result`` fills the cache — errors keep forwarding until one succeeds.
        """
        body = getattr(resp, "body", None)
        if resp.status != 200 or not body:
            return
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        if isinstance(data, dict) and data.get("result") is not None:
            self._chain_constants[(chain_key, method)] = data["result"]

    # -- deterministic responses -------------------------------------------

    def _budget_exceeded_response(
        self, is_batch: bool, ids: list[Any]
    ) -> web.Response:
        """Build the deterministic MINOTAUR_BUDGET_EXCEEDED response.

        Single request -> one error object echoing the request id.
        Batch request  -> an array of error objects, one per member id.
        """
        err = {"code": BUDGET_EXCEEDED_CODE, "message": BUDGET_EXCEEDED_MESSAGE}
        if is_batch:
            payload: Any = [
                {"jsonrpc": "2.0", "id": _id, "error": dict(err)} for _id in ids
            ]
        else:
            single_id = ids[0] if ids else None
            payload = {"jsonrpc": "2.0", "id": single_id, "error": dict(err)}
        # HTTP 200 with a JSON-RPC error body is the canonical JSON-RPC shape.
        return web.json_response(payload, status=200)

    def _json_error_response(
        self, _id: Any, *, code: int, message: str, http_status: int
    ) -> web.Response:
        return web.json_response(
            {"jsonrpc": "2.0", "id": _id, "error": {"code": code, "message": message}},
            status=http_status,
        )

    # -- block-pin ---------------------------------------------------------

    def _block_pin(self, parsed: Any, block_hex: str) -> Any:
        """Apply the block-pin rewrite to a parsed BATCH body (singles go
        through :meth:`_pinned_single`, which adds the pinned-read cache).

        Returns either a synthetic ``web.Response`` (a batch mixing
        intercepted/rejected members) OR the rewritten body ``bytes`` to
        forward. For a batch with no intercept/reject members, each member's
        block tag is rewritten and the batch is forwarded as one (uncached —
        the reference solver issues single calls; batches are rare).
        """
        out = []
        for member in parsed:
            action, payload = rewrite_single(member, block_hex)
            if action in ("blocknumber", "reject"):
                # A batch mixing intercepted/rejected members can't be a
                # single upstream forward; reject it whole (fail-loud, rare —
                # the reference solver issues single calls, handled fully).
                return self._json_error_response(
                    None,
                    code=-32601,
                    message=(
                        "eth_blockNumber / state-changing methods must be "
                        "single calls under block-pin"
                    ),
                    http_status=200,
                )
            out.append(payload)
        return json.dumps(out).encode()

    # -- control plane -----------------------------------------------------

    async def control_open(self, request: web.Request) -> web.Response:
        data = await _read_json(request)
        session_id = data.get("session_id")
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        budget = data.get("budget", self.default_budget)
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            return web.json_response({"error": "budget must be an integer"}, status=400)
        mode = _normalize_mode(data.get("mode", self.default_mode))
        blocks, berr = _normalize_blocks(data.get("blocks"))
        if berr is not None:
            return web.json_response({"error": berr}, status=400)
        # Bound the registry (evict oldest non-anon) so per-run session ids the
        # orchestrator may not explicitly close can't grow unbounded.
        while len(self.sessions) >= MAX_SESSIONS and session_id not in self.sessions:
            oldest = next((k for k in self.sessions if k != ANON_SESSION), None)
            if oldest is None:
                break
            self.sessions.pop(oldest, None)
        # create OR replace, spent reset to 0
        self.sessions[session_id] = Session(session_id, budget, mode, blocks=blocks)
        logger.info(
            "control/open session=%s budget=%d mode=%s blocks=%s",
            session_id, budget, mode, blocks,
        )
        return web.json_response(self.sessions[session_id].to_record())

    async def control_reset(self, request: web.Request) -> web.Response:
        data = await _read_json(request)
        session_id = data.get("session_id")
        session = self.sessions.get(session_id) if session_id else None
        if session is None:
            return web.json_response({"error": "unknown session_id"}, status=404)
        session.spent = 0
        session.exhausted = False
        if "blocks" in data:
            blocks, berr = _normalize_blocks(data.get("blocks"))
            if berr is not None:
                return web.json_response({"error": berr}, status=400)
            session.blocks = blocks  # re-point to the new round's blocks
        # peak is intentionally NOT reset here; it tracks across scenarios.
        logger.info("control/reset session=%s blocks=%s", session_id, session.blocks)
        return web.json_response(session.to_record())

    async def control_close(self, request: web.Request) -> web.Response:
        data = await _read_json(request)
        session_id = data.get("session_id")
        session = self.sessions.pop(session_id, None) if session_id else None
        if session is None:
            return web.json_response({"error": "unknown session_id"}, status=404)
        logger.info(
            "control/close session=%s spent=%d exhausted=%s peak=%d",
            session_id,
            session.spent,
            session.exhausted,
            session.peak,
        )
        return web.json_response(
            {
                "session_id": session.session_id,
                "spent": session.spent,
                "exhausted": session.exhausted,
                "peak": session.peak,
            }
        )

    async def control_stats(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "sessions": {
                    sid: sess.to_record() for sid, sess in self.sessions.items()
                },
                "pin_cache": self._pin_cache.stats(),
            }
        )

    # -- app factory -------------------------------------------------------

    def build_app(self) -> web.Application:
        token = self.control_token

        @web.middleware
        async def _control_auth(request: web.Request, handler: Any) -> web.StreamResponse:
            # The untrusted solver shares the proxy's port (data plane), so the
            # control plane must require the shared secret when one is configured.
            if token and request.path.startswith("/control/"):
                if request.headers.get("X-Control-Token") != token:
                    return web.json_response(
                        {"error": "control: forbidden"}, status=403
                    )
            return await handler(request)

        app = web.Application(middlewares=[_control_auth])
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        app.add_routes(
            [
                # data plane (faces untrusted solver)
                web.post("/rpc/{session_id}/{chain}", self.handle_rpc),
                web.post("/rpc/{session_id}", self.handle_rpc),
                # control plane (trusted validator only)
                web.post("/control/open", self.control_open),
                web.post("/control/reset", self.control_reset),
                web.post("/control/close", self.control_close),
                web.get("/control/stats", self.control_stats),
            ]
        )
        return app


# ---------------------------------------------------------------------------
# Body parsing / cost helpers (pure functions — deterministic).
# ---------------------------------------------------------------------------


def _safe_parse(raw_body: bytes) -> tuple[Any, bool]:
    """Parse a JSON body. Returns (value, ok). ok=False on parse failure."""
    if not raw_body:
        return None, False
    try:
        return json.loads(raw_body), True
    except (json.JSONDecodeError, ValueError):
        return None, False


def _cost_and_shape(parsed: Any, parse_ok: bool) -> tuple[int, bool, list[Any]]:
    """Compute (cost, is_batch, ids) for a parsed JSON-RPC body.

    - Single object: cost = request_cost(method); ids = [id].
    - Batch array: cost = sum of member costs; ids = [member ids...].
    - Unparseable / unexpected shape: cost = default (1 unit, via
      request_cost("")); not a batch; ids = [None].
    """
    from .cost_table import DEFAULT_COST  # local import keeps top clean

    if not parse_ok:
        return DEFAULT_COST, False, [None]

    if isinstance(parsed, list):
        methods = [
            (m.get("method") if isinstance(m, dict) else "") for m in parsed
        ]
        ids = [
            (m.get("id") if isinstance(m, dict) else None) for m in parsed
        ]
        return batch_cost(methods), True, ids

    if isinstance(parsed, dict):
        method = parsed.get("method", "")
        return request_cost(method), False, [parsed.get("id")]

    # Some other JSON scalar — charge default, treat as single.
    return DEFAULT_COST, False, [None]


def _content_type_only(value: str) -> str:
    """Strip any charset/params from a Content-Type header for web.Response.

    aiohttp's ``content_type`` kwarg rejects a value containing parameters
    (e.g. ``application/json; charset=utf-8``); we keep only the media type.
    """
    return value.split(";", 1)[0].strip() or "application/json"


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Read a JSON object body from a control request, tolerating empties."""
    try:
        raw = await request.read()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Config + entrypoint
# ---------------------------------------------------------------------------


def _parse_upstreams_env(value: str | None) -> dict[str, str]:
    """Parse the UPSTREAMS env var into a chain->url dict.

    Accepts either JSON (``{"eth": "http://...", "base": "http://..."}``) or a
    comma-separated ``chain=url`` list (``eth=http://...,base=http://...``).
    """
    if not value:
        return {}
    value = value.strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
            return {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError):
            logger.error("UPSTREAMS is not valid JSON; ignoring")
            return {}
    out: dict[str, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            logger.error("ignoring malformed UPSTREAMS entry: %r", pair)
            continue
        chain, url = pair.split("=", 1)
        out[chain.strip()] = url.strip()
    return out


def make_app() -> web.Application:
    """Factory: build the aiohttp application from environment configuration.

    Env vars:
      - ``UPSTREAMS``: chain->url map (JSON or ``a=...,b=...``). If unset, falls
        back to ``ANVIL_RPC_URL`` (single default upstream).
      - ``ANVIL_RPC_URL``: single upstream URL used when ``UPSTREAMS`` is unset.
      - ``BUDGET_PROXY_MODE``: ``observe`` (default) or ``enforce``.
      - ``BUDGET_PROXY_DEFAULT_BUDGET``: integer default budget.
    """
    upstreams = _parse_upstreams_env(os.environ.get("UPSTREAMS"))
    if not upstreams:
        single = os.environ.get("ANVIL_RPC_URL")
        if single:
            upstreams = {"eth": single}
    if not upstreams:
        raise ValueError(
            "No upstreams configured: set UPSTREAMS or ANVIL_RPC_URL"
        )

    mode = os.environ.get("BUDGET_PROXY_MODE", DEFAULT_MODE)
    try:
        default_budget = int(
            os.environ.get("BUDGET_PROXY_DEFAULT_BUDGET", DEFAULT_BUDGET)
        )
    except (TypeError, ValueError):
        default_budget = DEFAULT_BUDGET

    proxy = BudgetProxy(
        upstreams,
        default_mode=mode,
        default_budget=default_budget,
        control_token=os.environ.get("CONTROL_TOKEN", ""),
        response_cache_enabled=_response_cache_enabled_from_env(),
    )
    return proxy.build_app()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("LISTEN_HOST", DEFAULT_LISTEN_HOST)
    try:
        port = int(os.environ.get("LISTEN_PORT", DEFAULT_LISTEN_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_LISTEN_PORT

    app = make_app()
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
