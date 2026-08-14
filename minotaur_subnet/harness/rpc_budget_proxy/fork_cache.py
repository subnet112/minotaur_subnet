"""Read-through JSON-RPC cache between the anvil forks and their upstreams.

WHY
===
The anvil forks run ``--no-storage-caching`` (a deliberate defense: a malicious
solver's ``anvil_setStorageAt`` could otherwise persist in anvil's OWN read
cache across snapshot/revert) and the simulator re-forks before every
simulation. Every scenario therefore re-downloads all touched state
(``eth_getStorageAt`` / ``eth_getCode`` / block + tx data) from the archive
provider — after the block-pin proxy's response cache landed (#491), these
fork fetches became the DOMINANT Alchemy compute-unit driver.

This cache sits OUTSIDE anvil (anvil's ``--fork-url`` points here), so the
solver cannot write to it — the poisoning vector ``--no-storage-caching``
closes stays closed, while repeated fetches of the same immutable state are
served locally.

WHAT IS CACHED (and what never is)
==================================
Only responses that are IMMUTABLE by construction:

- state reads whose block tag is an EXPLICIT number (the rewrite table's
  ``BLOCK_PARAM_INDEX`` methods) — one canonical state per historical block.
  A ``latest``/``pending``/``safe``/``finalized``/missing tag is a moving
  target and ALWAYS forwards.
- hash-keyed lookups (``eth_getBlockByHash``, ``eth_getTransactionByHash``,
  ``eth_getTransactionReceipt``) — content-addressed, immutable.
- per-chain constants (``eth_chainId``, ``net_version``) — as in the block-pin
  proxy (#490).

Everything else (``eth_blockNumber``, ``eth_gasPrice``, ``eth_feeHistory``,
unknown methods) forwards untouched. Error responses and null results are
never cached (a transient provider failure must not be frozen). Batches are
served locally only when EVERY member is a cache hit; otherwise the whole
batch forwards and cacheable members are harvested from the response.

NOT a consensus surface: the values served are the upstream's own canonical
results for immutable queries; the simulator's behavior depends on the state
VALUES only. Reorg semantics match foundry's OWN per-(chain, block) storage
cache: state cached at a near-head block that later reorgs would be served
pre-reorg — the benchmark path pins one epoch back (12+ confirmations) so its
reads are final; only head-of-chain current-state sims carry the (tiny, Base
sequencer) reorg exposure anvil's native cache always carried.

Deployment: a compose service the anvils depend on — deliberately NOT
api-managed (the api needs anvil at startup; anvil needs its fork url at
startup — an api-managed cache would deadlock a fresh host).

Env: ``UPSTREAMS`` (``chain=url,...`` — same format as the budget proxy),
``LISTEN_PORT`` (default 8650), ``FORK_CACHE_DISABLE=1`` (forward everything),
``FORK_CACHE_MAX_ENTRIES`` / ``FORK_CACHE_MAX_RESULT_BYTES`` bounds,
``FORK_CACHE_PERSIST_PATH`` (empty = off; a path = snapshot the cache there and
warm-load it on startup, so a restart skips the cold re-fetch storm) /
``FORK_CACHE_PERSIST_INTERVAL`` (snapshot cadence in seconds, default 300).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from ._persist import SnapshotScheduler, load_snapshot
from minotaur_subnet.rpc_backoff import (
    DEFAULT_FORWARD_DEADLINE_SECONDS,
    body_has_retryable_rpc_error,
    is_retryable_status,
    retry_async,
)

from .proxy import _content_type_only, _host_of, _parse_upstreams_env, _safe_parse
from .rewrite_table import BLOCK_PARAM_INDEX

logger = logging.getLogger(__name__)

DEFAULT_LISTEN_PORT = 8650
UPSTREAM_TIMEOUT_SECONDS = 30

# Hash-keyed lookups: content-addressed, immutable for any params.
HASH_KEYED_METHODS = frozenset({
    "eth_getBlockByHash",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
    "eth_getRawTransactionByHash",
})
# Immutable per-chain constants (see the block-pin proxy's constant cache).
CONSTANT_METHODS = frozenset({"eth_chainId", "net_version"})

# Block tags that name MOVING state — a request carrying one never caches.
_MOVING_TAGS = frozenset({"latest", "pending", "earliest", "safe", "finalized"})

# Methods the JSON-RPC spec defines as returning an OBJECT or null — "null"
# meaning "no such thing", which is a routine answer, not a failure.
#
# A provider that answers one of these with a scalar has said something the
# spec has no room for, and the damage is out of all proportion to the typo:
# anvil deserializes these into ``Option<T>``, so ``null`` becomes "not found"
# and a JSON *string* becomes a TYPE ERROR that fails the whole fork request —
# `Fork Error: DeserError { invalid type: string "0x0", expected struct ... }`.
# A routine "does this tx exist?" turns into a dead simulation.
#
# Observed live 2026-08-13: rpc-base.blockmachine.io returns the string "0x0"
# instead of null for unknown tx hashes (eth_getTransactionReceipt 30/30,
# eth_getTransactionByHash 28/30). Their ETH endpoint is correct on the same
# gateway build, and Base's own mainnet.base.org plus two other independent
# Base RPCs all return null — so this is that provider, not the chain.
# It cost ~14h at 94.7% Base row failure.
NULLABLE_OBJECT_METHODS = frozenset({
    "eth_getBlockByHash",
    "eth_getBlockByNumber",
    "eth_getTransactionByHash",
    "eth_getTransactionByBlockHashAndIndex",
    "eth_getTransactionByBlockNumberAndIndex",
    "eth_getTransactionReceipt",
    "eth_getUncleByBlockHashAndIndex",
    "eth_getUncleByBlockNumberAndIndex",
})


def conforms(method: Any, result: Any) -> bool:
    """Is ``result`` a shape the spec allows for ``method``?

    Only ever False for :data:`NULLABLE_OBJECT_METHODS` given a non-object,
    non-null result. Every other method/result pair is accepted unexamined —
    this is a narrow conformance guard, not a schema validator.
    """
    if method not in NULLABLE_OBJECT_METHODS:
        return True
    return result is None or isinstance(result, dict)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _is_explicit_block(value: Any) -> bool:
    """True iff ``value`` names one fixed block (int or 0x-hex/decimal string)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _MOVING_TAGS:
            return False
        try:
            int(v, 16) if v.startswith("0x") else int(v)
            return True
        except ValueError:
            return False
    return False


def cache_key(chain: str, req: Any) -> str | None:
    """The cache key for one request object, or None when it must forward.

    A key exists only for provably-immutable queries (see module docstring);
    the key canonicalizes params (sorted dict keys) so equivalent spellings
    share an entry.
    """
    if not isinstance(req, dict):
        return None
    method = req.get("method")
    if not isinstance(method, str):
        return None
    params = req.get("params")
    if method in CONSTANT_METHODS:
        pass  # params are irrelevant/empty for constants
    elif method in HASH_KEYED_METHODS:
        pass  # content-addressed
    elif method in BLOCK_PARAM_INDEX:
        idx = BLOCK_PARAM_INDEX[method]
        if not isinstance(params, list) or len(params) <= idx:
            return None  # absent tag == latest — moving
        if not _is_explicit_block(params[idx]):
            return None
    else:
        return None
    try:
        canon = json.dumps(params, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None  # unserializable params — just forward
    return f"{chain}:{method}:{canon}"


class ForkCache:
    """The read-through cache application (single event loop, plain dicts)."""

    def __init__(
        self,
        upstreams: dict[str, str],
        *,
        max_entries: int | None = None,
        max_result_bytes: int | None = None,
        disabled: bool = False,
    ) -> None:
        # Drop empty upstream URLs (unset env) — requests to those chains fail
        # loud (502), exactly as anvil would have failed on the empty env.
        self.upstreams = {k: v for k, v in upstreams.items() if v}
        if not self.upstreams:
            raise ValueError("ForkCache requires at least one non-empty upstream")
        self.max_entries = max_entries or _env_int("FORK_CACHE_MAX_ENTRIES", 200_000)
        self.max_result_bytes = (
            max_result_bytes or _env_int("FORK_CACHE_MAX_RESULT_BYTES", 131_072)
        )
        self.disabled = disabled
        # key -> result. dict preserves insertion order; re-insert on hit = LRU.
        self._cache: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self.uncacheable = 0
        # Spec-violating results rewritten to null on the way to anvil. Should
        # be 0 against a conformant provider; a rising count names a broken one.
        self.nonconforming = 0
        self._client: ClientSession | None = None
        # Optional disk persistence: warm-load a snapshot on startup and re-write
        # it on a cadence, so a restart skips the cold re-fetch storm. Inert
        # unless FORK_CACHE_PERSIST_PATH is set (see ._persist).
        self._persist_path = os.environ.get("FORK_CACHE_PERSIST_PATH", "").strip()
        self._snapshotter = SnapshotScheduler(
            self._persist_path,
            _env_int("FORK_CACHE_PERSIST_INTERVAL", 300),
            self._snapshot_payload,
        )

    # -- lifecycle -----------------------------------------------------------

    async def _on_startup(self, _app: web.Application) -> None:
        limit = _env_int("FORK_CACHE_UPSTREAM_MAX_CONCURRENCY", 24)
        self._client = ClientSession(
            timeout=ClientTimeout(total=UPSTREAM_TIMEOUT_SECONDS),
            connector=TCPConnector(limit=limit),
        )
        logger.info(
            "fork_cache started: chains=%s max_entries=%d disabled=%s",
            list(self.upstreams), self.max_entries, self.disabled,
        )
        if self._snapshotter.enabled:
            payload = load_snapshot(self._persist_path)
            if payload is not None:
                self._restore(payload)
            await self._snapshotter.start()

    async def _on_cleanup(self, _app: web.Application) -> None:
        await self._snapshotter.stop()  # final snapshot + cancel the periodic task
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- cache primitives ----------------------------------------------------

    def _get(self, key: str) -> tuple[bool, Any]:
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)  # refresh LRU position
            self.hits += 1
            return True, self._cache[key]
        self.misses += 1
        return False, None

    def _put(self, key: str, result: Any) -> None:
        if result is None:
            return  # null results are not cached (mirrors the pin cache)
        # Never store a shape the spec forbids. ``_conform`` already rewrote
        # these to null upstream of here, so this is defense in depth — but it
        # is the layer that matters, because THIS cache persists to disk. That
        # is how one provider's transient bug became a 14-hour outage that
        # survived every restart: 2366/2366 cached base receipts held "0x0".
        method = key.split(":", 2)[1] if key.count(":") >= 2 else None
        if not conforms(method, result):
            return
        try:
            size = len(json.dumps(result))
        except (TypeError, ValueError):
            return
        if size > self.max_result_bytes:
            return
        while len(self._cache) >= self.max_entries:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = result

    # -- persistence ---------------------------------------------------------

    def _snapshot_payload(self) -> dict[str, Any]:
        """On-loop shallow copy handed to the snapshotter. Cheap (one dict copy)
        and safe to serialize in a thread: values are immutable JSON results."""
        return dict(self._cache)

    def _restore(self, payload: Any) -> None:
        """Warm-load a snapshot, honouring the CURRENT ``max_entries`` bound
        (keep the most-recently-inserted entries = LRU tail)."""
        if not isinstance(payload, dict):
            return
        items = list(payload.items())
        if len(items) > self.max_entries:
            items = items[-self.max_entries:]
        # Drop entries a conformant provider could never have produced. A
        # snapshot written before this guard existed can carry poison, and
        # warm-loading it would re-serve the outage from disk — the operator
        # would have to purge the volume by hand to recover.
        kept = [
            (k, v) for k, v in items
            if conforms(k.split(":", 2)[1] if k.count(":") >= 2 else None, v)
        ]
        dropped = len(items) - len(kept)
        self._cache = dict(kept)
        if dropped:
            logger.warning(
                "fork_cache dropped %d non-conforming entries while warm-loading "
                "(a provider returned a shape the spec forbids and it was "
                "persisted) — they will be re-fetched", dropped,
            )
        logger.info("fork_cache warm-loaded %d entries from snapshot", len(self._cache))

    # -- request handling ----------------------------------------------------

    async def handle_rpc(self, request: web.Request) -> web.StreamResponse:
        chain = request.match_info.get("chain", "")
        upstream = self.upstreams.get(chain)
        if upstream is None:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32602, "message": f"unknown chain '{chain}'"}},
                status=400,
            )
        raw_body = await request.read()
        parsed, ok = _safe_parse(raw_body)
        if not ok or self.disabled:
            return await self._forward(upstream, raw_body, request)

        if isinstance(parsed, dict):
            return await self._handle_single(upstream, chain, parsed, raw_body, request)
        if isinstance(parsed, list):
            return await self._handle_batch(upstream, chain, parsed, raw_body, request)
        return await self._forward(upstream, raw_body, request)

    async def _handle_single(
        self, upstream: str, chain: str, req: dict, raw_body: bytes,
        request: web.Request,
    ) -> web.StreamResponse:
        key = cache_key(chain, req)
        if key is None:
            self.uncacheable += 1
            return await self._forward(upstream, raw_body, request)
        hit, result = self._get(key)
        if hit:
            return web.json_response(
                {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
            )
        resp = self._conform(
            await self._forward(upstream, raw_body, request), req.get("method"),
        )
        self._harvest_single(key, resp)
        return resp

    async def _handle_batch(
        self, upstream: str, chain: str, batch: list, raw_body: bytes,
        request: web.Request,
    ) -> web.StreamResponse:
        keys = [cache_key(chain, m) for m in batch]
        answers: list[Any] = []
        all_hit = True
        for member, key in zip(batch, keys):
            if key is None:
                all_hit = False
                break
            hit, result = self._get(key)
            if not hit:
                all_hit = False
                break
            answers.append({
                "jsonrpc": "2.0",
                "id": member.get("id") if isinstance(member, dict) else None,
                "result": result,
            })
        if all_hit and answers:
            return web.json_response(answers)

        resp = self._conform(
            await self._forward(upstream, raw_body, request),
            {m.get("id"): m.get("method") for m in batch if isinstance(m, dict)},
        )
        self._harvest_batch(batch, keys, resp)
        return resp

    # -- conformance ---------------------------------------------------------

    def _conform(
        self, resp: web.StreamResponse, methods: dict[Any, Any] | str,
    ) -> web.StreamResponse:
        """Rewrite spec-violating scalar results to ``null`` before anvil sees them.

        This DELIBERATELY breaks the byte-transparency ``_forward`` otherwise
        keeps, for one narrow case the spec leaves no room for: a
        :data:`NULLABLE_OBJECT_METHODS` result that is neither an object nor
        null. Transparency exists so anvil sees what the provider really said;
        it is not worth preserving when what the provider said is unspellable
        and costs us the whole simulation (see NULLABLE_OBJECT_METHODS).

        Narrow on purpose. We do NOT invent a receipt, mask an error object, or
        touch any other method — the only rewrite is scalar → null, which maps
        a malformed answer back onto the "no such thing" the provider was
        trying to express. Everything else forwards untouched.

        ``methods`` is the single request's method, or {id: method} for a batch.
        """
        data = self._response_json(resp)
        if data is None:
            return resp
        fixed = 0

        def _fix(member: Any) -> None:
            nonlocal fixed
            if not isinstance(member, dict) or "result" not in member:
                return
            method = (
                methods if isinstance(methods, str)
                else methods.get(member.get("id"))
            )
            if method is None or conforms(method, member["result"]):
                return
            logger.warning(
                "fork_cache: %s returned a non-conforming result (%r) — "
                "rewriting to null; the provider should send null for "
                "'not found'", method, member["result"],
            )
            member["result"] = None
            fixed += 1

        if isinstance(data, dict):
            _fix(data)
        elif isinstance(data, list):
            for member in data:
                _fix(member)
        if not fixed:
            return resp
        self.nonconforming += fixed
        return web.json_response(data, status=resp.status)

    # -- fill-on-forward -----------------------------------------------------

    def _response_json(self, resp: web.StreamResponse) -> Any:
        body = getattr(resp, "body", None)
        if resp.status != 200 or not body:
            return None
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def _harvest_single(self, key: str, resp: web.StreamResponse) -> None:
        data = self._response_json(resp)
        if isinstance(data, dict) and data.get("error") is None and "result" in data:
            self._put(key, data["result"])

    def _harvest_batch(
        self, batch: list, keys: list[str | None], resp: web.StreamResponse,
    ) -> None:
        data = self._response_json(resp)
        if not isinstance(data, list):
            return
        by_id = {
            d.get("id"): d for d in data
            if isinstance(d, dict) and d.get("error") is None and "result" in d
        }
        for member, key in zip(batch, keys):
            if key is None or not isinstance(member, dict):
                continue
            d = by_id.get(member.get("id"))
            if d is not None:
                self._put(key, d["result"])

    # -- transport -----------------------------------------------------------

    async def _forward(
        self, upstream: str, raw_body: bytes, request: web.Request
    ) -> web.StreamResponse:
        """Byte-transparent forward, preserving upstream status/errors (the same
        fail-loud semantics anvil would see talking to the provider directly)."""
        assert self._client is not None, "client session not started"
        content_type = request.headers.get("Content-Type", "application/json")

        async def _once() -> tuple[int, bytes, str]:
            async with self._client.post(
                upstream, data=raw_body, headers={"Content-Type": content_type},
            ) as resp:
                body = await resp.read()
                ct = _content_type_only(
                    resp.headers.get("Content-Type", "application/json")
                )
                return resp.status, body, ct

        # Retry TRANSIENT upstream failures (429 / -32005 / 5xx / timeout / reset)
        # before surfacing them — fork fetches are the dominant Alchemy driver and
        # a 429 here makes anvil (which --retries=10 blindly re-fires) hammer the
        # provider and can zero a benchmark read. Fork reads are immutable-by-block
        # → a retry re-fetches identical bytes. Fail-loud after retries exhaust.
        try:
            status, body, ct = await retry_async(
                _once,
                deadline_seconds=DEFAULT_FORWARD_DEADLINE_SECONDS,
                retry_on_result=lambda r: is_retryable_status(r[0])
                or body_has_retryable_rpc_error(r[1]),
                label=f"fork-cache→{_host_of(upstream)}",
            )
            return web.Response(body=body, status=status, content_type=ct)
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            logger.error("fork_cache upstream error (%s, after retries): %s",
                         _host_of(upstream), exc)
            return web.json_response(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32000, "message": "upstream unreachable"}},
                status=502,
            )

    # -- observability -------------------------------------------------------

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "chains": list(self.upstreams)})

    async def handle_stats(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "hits": self.hits,
            "misses": self.misses,
            "uncacheable": self.uncacheable,
            "nonconforming": self.nonconforming,
            "entries": len(self._cache),
            "disabled": self.disabled,
            "persist": self._snapshotter.enabled,
        })

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        app.add_routes([
            web.get("/health", self.handle_health),
            web.get("/stats", self.handle_stats),
            web.post("/{chain}", self.handle_rpc),
        ])
        return app


def make_app() -> web.Application:
    upstreams = _parse_upstreams_env(os.environ.get("UPSTREAMS"))
    disabled = os.environ.get("FORK_CACHE_DISABLE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    return ForkCache(upstreams, disabled=disabled).build_app()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        port = int(os.environ.get("LISTEN_PORT", DEFAULT_LISTEN_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_LISTEN_PORT
    web.run_app(make_app(), host=os.environ.get("LISTEN_HOST", "0.0.0.0"), port=port)


if __name__ == "__main__":
    main()
