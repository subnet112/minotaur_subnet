"""Read-scoped exponential-backoff retry for web3 providers.

A web3 v7 middleware + factory that make every provider built through them ride
out transient RPC failures (429 / ``-32005`` compute-unit / 5xx / timeout /
connection reset) via the shared :mod:`minotaur_subnet.rpc_backoff` primitive —
instead of a single-shot call that surfaces the hiccup to the caller.

SCOPE: only idempotent READ methods are retried. Write / signing / state-mutation
methods (``eth_sendRawTransaction``, ``eth_sendTransaction``, signing, and the
``anvil_*`` / ``evm_*`` / ``hardhat_*`` fork-mutation family) pass through
UNTOUCHED — a blind retry could double-submit a tx or double-apply a cheatcode.
The relayer owns nonce/gas-aware send retries.
"""

from __future__ import annotations

import logging
from typing import Any

import web3  # module ref so build_retrying_web3 honours patch("web3.Web3") in tests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware, Web3Middleware

from minotaur_subnet.rpc_backoff import (
    DEFAULT_WEB3_ATTEMPTS,
    DEFAULT_WEB3_DEADLINE_SECONDS,
    is_retryable_rpc_code,
    retry_async,
    retry_sync,
)

logger = logging.getLogger(__name__)

# Attribute names for per-instance retry config, stashed on the Web3 object by
# install_rpc_retry so the middleware (instantiated by web3 as Middleware(w3))
# can read them. Lets latency-sensitive sites (e.g. on-event-loop fork-pin reads)
# request fewer attempts / a tighter deadline than the default.
_CFG_ATTEMPTS = "_rpc_retry_attempts"
_CFG_DEADLINE = "_rpc_retry_deadline"

# Exact methods that mutate state / submit or sign transactions — NEVER retried.
_NO_RETRY_METHODS = frozenset({
    "eth_sendRawTransaction",
    "eth_sendTransaction",
    "eth_sign",
    "eth_signTransaction",
    "eth_signTypedData",
    "personal_sendTransaction",
    "personal_sign",
    "personal_signTypedData",
})

# Method-name prefixes for local-node / fork mutations (anvil cheatcodes, mining,
# tx-pool control) — non-idempotent, NEVER retried.
_NO_RETRY_PREFIXES = ("anvil_", "evm_", "hardhat_", "miner_", "txpool_", "ganache_")


def _is_no_retry(method: Any) -> bool:
    m = str(method)
    return m in _NO_RETRY_METHODS or m.startswith(_NO_RETRY_PREFIXES)


def _response_is_retryable(resp: Any) -> bool:
    """A JSON-RPC error response carrying the TRANSIENT ``-32005`` rate-limit
    code — retry. A real application error (revert, bad params, unknown block,
    generic ``-32000``) is NOT retryable and passes straight through."""
    if isinstance(resp, dict):
        err = resp.get("error")
        if isinstance(err, dict):
            return is_retryable_rpc_code(err.get("code"))
    return False


class RpcRetryMiddleware(Web3Middleware):
    """Retry idempotent reads on transient provider failures; pass writes through.
    Reads per-instance attempts/deadline off the Web3 object (set by
    install_rpc_retry), defaulting to the light web3 retry budget."""

    def _cfg(self):
        w3 = self._w3
        return (
            getattr(w3, _CFG_ATTEMPTS, None) or DEFAULT_WEB3_ATTEMPTS,
            getattr(w3, _CFG_DEADLINE, None) or DEFAULT_WEB3_DEADLINE_SECONDS,
        )

    def wrap_make_request(self, make_request):
        def middleware(method, params):
            if _is_no_retry(method):
                return make_request(method, params)
            attempts, deadline = self._cfg()
            return retry_sync(
                lambda: make_request(method, params),
                attempts=attempts,
                deadline_seconds=deadline,
                retry_on_result=_response_is_retryable,
                label=f"web3:{method}",
            )

        return middleware

    async def async_wrap_make_request(self, make_request):
        async def middleware(method, params):
            if _is_no_retry(method):
                return await make_request(method, params)
            attempts, deadline = self._cfg()
            return await retry_async(
                lambda: make_request(method, params),
                attempts=attempts,
                deadline_seconds=deadline,
                retry_on_result=_response_is_retryable,
                label=f"web3:{method}",
            )

        return middleware


def install_rpc_retry(
    w3: Web3,
    *,
    attempts: int | None = None,
    deadline_seconds: float | None = None,
) -> Web3:
    """Inject the read-scoped retry middleware as the OUTERMOST layer, so it
    wraps the full request (any other middleware + the provider call). Idempotent
    reads re-run cleanly; excluded writes are untouched. ``attempts`` /
    ``deadline_seconds`` override the light web3 defaults per-instance (e.g. a
    tighter budget for reads that run on a latency-sensitive event loop).

    Best-effort: a non-standard provider without a ``middleware_onion`` (a test
    stub, an exotic client) is left as-is rather than crashing — the retry is an
    enhancement, and the w3 still works without it. A real ``Web3`` always has
    the onion, so production always gets the middleware."""
    if attempts is not None:
        setattr(w3, _CFG_ATTEMPTS, attempts)
    if deadline_seconds is not None:
        setattr(w3, _CFG_DEADLINE, deadline_seconds)
    try:
        w3.middleware_onion.inject(RpcRetryMiddleware, layer=0)
    except Exception as exc:  # noqa: BLE001 — non-standard/mock provider: skip
        logger.debug("rpc-retry middleware not installed on %r: %s", type(w3), exc)
    return w3


def build_retrying_web3(
    rpc_url: str,
    *,
    poa: bool = False,
    request_kwargs: dict[str, Any] | None = None,
    attempts: int | None = None,
    deadline_seconds: float | None = None,
) -> Web3:
    """Build a ``Web3`` HTTP client with transient-read retry already installed.

    Drop-in for the ad-hoc ``Web3(Web3.HTTPProvider(url))`` sites so every direct
    provider path gets the same backoff. Pass ``poa=True`` for L2/PoA chains and
    ``request_kwargs`` (e.g. ``{"timeout": 5}``) as you would to ``HTTPProvider``.
    ``attempts`` / ``deadline_seconds`` tighten the retry for latency-sensitive
    callers (e.g. on-event-loop fork-pin reads)."""
    # Construct via the ``web3`` module attribute (not a local ``Web3`` binding)
    # so a test that patches ``web3.Web3`` still intercepts these ad-hoc sites.
    provider = (
        web3.Web3.HTTPProvider(rpc_url, request_kwargs=request_kwargs)
        if request_kwargs is not None
        else web3.Web3.HTTPProvider(rpc_url)
    )
    w3 = web3.Web3(provider)
    if poa:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    install_rpc_retry(w3, attempts=attempts, deadline_seconds=deadline_seconds)
    return w3


__all__ = [
    "RpcRetryMiddleware",
    "install_rpc_retry",
    "build_retrying_web3",
]
