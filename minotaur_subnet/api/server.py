"""
REST API server for the Minotaur App Intents platform.

Exposes all App Intent operations via a versioned HTTP API on port 8080.
All endpoints delegate to the existing tools.py functions -- zero business
logic duplication.

Start the server:
    python -m minotaur_subnet.api.server
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure the repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import os

# Configure logging before anything else so all modules get handlers.
# Without this, the root logger has no handlers and drops all INFO/WARNING logs.
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from minotaur_subnet.api.routes import (
    apps,
    chains,
    wallets,
    monitoring,
    submissions,
    orders,
    native_bittensor,
    identity,
)
from minotaur_subnet.store import AppIntentStore

# Import the shared context and startup module
from minotaur_subnet.api.server_context import ctx
from minotaur_subnet.api import startup as _startup

logger = logging.getLogger(__name__)

# ── shared store instance ────────────────────────────────────────────────────

_store_path = os.environ.get("APP_INTENTS_STORE_PATH")
store = AppIntentStore(store_path=Path(_store_path) if _store_path else None)
ctx.store = store

# ── backward-compatible module-level accessors ───────────────────────────────
#
# Many route files and tests do:
#     from minotaur_subnet.api.server import store, _block_loop, ...
# or  patch.object(api_server, "_benchmark_worker", ...)
#
# These properties delegate to the centralized ctx so that existing code
# continues to work without modification.  The names intentionally shadow
# the ctx fields with the old underscore-prefixed convention.
#
# NOTE: Module-level attribute access is handled via __getattr__ below for
# dynamic ctx fields.  Static attributes (store, app) are real module globals.


def __getattr__(name: str):
    """Lazy module-level attribute access that delegates to ctx or startup."""
    # Map old module-level variable names -> ctx field names
    _CTX_FIELD_MAP = {
        "_benchmark_worker": "benchmark_worker",
        "_benchmark_task": "benchmark_task",
        "_epoch_manager": "epoch_manager",
        "_solver_round_task": "solver_round_task",
        "_solver_round_metagraph_sync": "solver_round_metagraph_sync",
        "_solver_round_metagraph_task": "solver_round_metagraph_task",
        "_solver_round_role_task": "solver_round_role_task",
        "_solver_round_role": "solver_round_role",
        "_solver_round_epoch_clock": "solver_round_epoch_clock",
        "_orderbook": "orderbook",
        "_block_loop": "block_loop",
        "_block_loop_task": "block_loop_task",
        "_provenance_policy_health": "provenance_policy_health",
        "_runtime_security_policy_health": "runtime_security_policy_health",
    }
    if name in _CTX_FIELD_MAP:
        return getattr(ctx, _CTX_FIELD_MAP[name])

    # Delegate helper functions to startup module
    _STARTUP_FUNCS = {
        "_env_true",
        "_is_real_chain_url",
        "_looks_like_mainnet_bittensor_target",
        "_looks_like_local_or_test_subtensor_url",
        "_validate_native_bittensor_demo_guard",
        "_resolve_solver_round_hotkey",
        "_resolve_native_bittensor_target",
        "_build_provenance_health_snapshot",
        "_build_runtime_security_health_snapshot",
    }
    if name in _STARTUP_FUNCS:
        return getattr(_startup, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Allow tests to patch module-level ctx fields via patch.object()
# NOTE on patch.object compatibility:
# patch.object(api_server, "_benchmark_worker", mock) works because setattr
# writes to module.__dict__, shadowing __getattr__. When the patch exits,
# delattr removes the shadow and __getattr__ resumes delegating to ctx.
# PEP 562 only supports module-level __getattr__ and __dir__, NOT __setattr__.


# ── epoch helper wrappers (delegate to startup with ctx) ─────────────────────


def _solver_round_epoch_block_number() -> int | None:
    return _startup._solver_round_epoch_block_number(ctx)


def _solver_round_native_epoch_info() -> object | None:
    return _startup._solver_round_native_epoch_info(ctx)


def _solver_round_native_epoch() -> int | None:
    return _startup._solver_round_native_epoch(ctx)


def _solver_round_native_epoch_length_blocks() -> int | None:
    return _startup._solver_round_native_epoch_length_blocks(ctx)


def _solver_round_native_blocks_since_last_step() -> int | None:
    return _startup._solver_round_native_blocks_since_last_step(ctx)


def _current_solver_round_epoch() -> int:
    return _startup._current_solver_round_epoch(ctx)


def _solver_round_epoch_health() -> dict[str, object]:
    return _startup._solver_round_epoch_health(ctx)


# ── lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop background workers with the server."""
    locals_bag = await _startup.initialize(ctx)
    yield
    await _startup.shutdown(ctx, locals_bag)


# ── FastAPI app ──────────────────────────────────────────────────────────────

# H4 (2026-05-25 audit): /docs, /redoc, /openapi.json publicly enumerate
# every admin/internal endpoint — exploit catalog. Disabled by default;
# only LOCAL_TESTNET=1 (dev) or EXPOSE_OPENAPI=1 (operator opt-in) re-enables.
_expose_openapi = (
    os.environ.get("LOCAL_TESTNET", "").strip() == "1"
    or os.environ.get("EXPOSE_OPENAPI", "").strip() == "1"
)
_openapi_kwargs: dict[str, str | None] = (
    {}  # FastAPI's defaults: /docs, /redoc, /openapi.json
    if _expose_openapi
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)

app = FastAPI(
    title="Minotaur App Intents API",
    version="0.2.0",
    description=(
        "REST API for Minotaur Subnet 112 -- a distributed intent execution platform on Bittensor.\n\n"
        "## Core Flow\n\n"
        "1. **Create** an App Intent with JS scoring + Solidity contract\n"
        "2. **Deploy** the app to a supported chain\n"
        "3. **Prepare** an order (resolves token symbols, chain, nonce)\n"
        "4. **Quote** to get estimated output and slippage protection\n"
        "5. **Submit** the order -- the Solving Engine finds optimal execution\n"
        "6. **Monitor** order status as it progresses: pending -> solved -> scored -> consensus -> filled\n\n"
        "## Authentication\n\n"
        "No authentication required for the local testnet. "
        "Production endpoints will require validator signatures."
    ),
    lifespan=lifespan,
    **_openapi_kwargs,
)

# H5 (2026-05-25 audit): default ``["*"]`` lets any origin hit any endpoint
# from a browser. Restrict to the published frontend; allow override via
# ``CORS_ALLOW_ORIGINS`` (comma-separated) for dev/staging. LOCAL_TESTNET=1
# preserves the old open-by-default behavior for local development.
def _resolve_cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    if os.environ.get("LOCAL_TESTNET", "").strip() == "1":
        return ["*"]
    return ["https://app.minotaursubnet.com"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_origins(),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    worker_running = ctx.benchmark_worker is not None and ctx.benchmark_worker._running
    loop_running = ctx.block_loop is not None and ctx.block_loop.running
    coordinator_running = ctx.solver_round_task is not None and not ctx.solver_round_task.done()
    # MINOTAUR_IMAGE_SHA is baked at build time (see Dockerfile + the
    # docker-publish.yml build-args). 7-char prefix matches GHCR's
    # sha-XXXXXXX tag scheme; "dev" for local builds without --build-arg.
    image_sha = os.environ.get("MINOTAUR_IMAGE_SHA", "dev")[:7]
    # Live champion solver state — surfaces whether the Docker session
    # backing /v1/apps/*/quote and /v1/apps/*/orders is currently usable.
    # The session can die transparently on a per-command timeout (orchestrator
    # kills it to preserve stdio protocol sync); pre-respawn, the api would
    # 500 every subsequent quote until the operator manually restarted the
    # api process. Now the runtime auto-respawns on next call, but operators
    # + the validator-health workflow still need a way to spot wedge states.
    #
    # ``live_solver_running`` is the simple bool the workflow's classifier
    # uses: False ⇒ solver crashed and respawn pending OR no genesis solver
    # configured. None ⇒ this api has no block_loop wired (only the leader's
    # api has one), so the field is N/A.
    #
    # ``live_solver`` carries the diagnostic snapshot: respawn count over
    # the runtime's lifetime, timestamp of the most recent respawn, and the
    # truncated reason for the last crash. A non-zero ``respawn_count`` is
    # informational; a fast-rising count is a crash-loop signal.
    live_solver_running: bool | None = None
    live_solver_diagnostics: dict | None = None
    if ctx.block_loop is not None and getattr(ctx.block_loop, "solver", None) is not None:
        _solver = ctx.block_loop.solver
        if hasattr(_solver, "is_alive"):
            live_solver_running = _solver.is_alive()
        if hasattr(_solver, "respawn_state"):
            live_solver_diagnostics = _solver.respawn_state()

    data = {
        "status": "ok",
        "service": "app-intents-api",
        "image_sha": image_sha,
        "benchmark_worker": "running" if worker_running else "disabled",
        "solver_round_coordinator": "running" if coordinator_running else "disabled",
        "solver_round_role": ctx.solver_round_role,
        "solver_round_epoch": _current_solver_round_epoch(),
        "solver_round_epoch_clock": _solver_round_epoch_health(),
        "block_loop": "running" if loop_running else "disabled",
        "live_solver_running": live_solver_running,
        "live_solver": live_solver_diagnostics,
        "provenance_policy": dict(ctx.provenance_policy_health),
        "runtime_security_policy": dict(ctx.runtime_security_policy_health),
    }
    if ctx.epoch_manager is not None:
        data["solver_epoch"] = max(ctx.epoch_manager.current_epoch, _current_solver_round_epoch())
        # Most recent EpochManager queue POST. Schema matches the
        # validator daemon's /health.last_emit so the validator-health
        # workflow can read either /health endpoint and apply the same
        # classifier. Result is "queued" / "empty" / "error" — the
        # actual chain emit happens in the validator daemon (which
        # records its own _last_emit_state under source="queued_from_api").
        data["last_emit"] = getattr(ctx.epoch_manager, "_last_emit_state", None)
    champion_consensus = submissions.get_champion_consensus_manager()
    champion_peer_network = submissions.get_champion_peer_network()
    if champion_consensus is not None:
        data["champion_consensus"] = {
            "enabled": True,
            "validator_id": champion_consensus.validator_id,
            "quorum_required": champion_consensus.quorum_required,
            "validator_count": len(champion_consensus.validators),
            "peer_count": len(champion_peer_network.peers) if champion_peer_network is not None else 0,
            "internal_round_auth_configured": bool(
                os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
                or os.environ.get("SUBMISSIONS_API_KEY", "").strip()
            ),
        }
    else:
        data["champion_consensus"] = {
            "enabled": False,
            "internal_round_auth_configured": bool(
                os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
                or os.environ.get("SUBMISSIONS_API_KEY", "").strip()
            ),
        }
    if ctx.solver_round_metagraph_sync is not None and ctx.solver_round_metagraph_sync.state is not None:
        epoch_info = getattr(ctx.solver_round_metagraph_sync.state, "epoch", None)
        data["solver_round_metagraph"] = {
            "block": ctx.solver_round_metagraph_sync.state.block,
            "validator_count": len(ctx.solver_round_metagraph_sync.state.validators),
            "leader_hotkey": (
                ctx.solver_round_metagraph_sync.state.leader.hotkey
                if ctx.solver_round_metagraph_sync.state.leader is not None
                else None
            ),
            "native_epoch": (epoch_info.epoch_index if epoch_info is not None else None),
            "tempo_blocks": (epoch_info.tempo_blocks if epoch_info is not None else None),
            "epoch_length_blocks": (
                epoch_info.epoch_length_blocks if epoch_info is not None else None
            ),
            "blocks_since_last_step": (
                epoch_info.blocks_since_last_step if epoch_info is not None else None
            ),
        }
    try:
        current_round = submissions.get_round_store().get_current_round()
        if current_round is not None:
            data["solver_round"] = {
                "round_id": current_round.round_id,
                "status": current_round.status.value,
                "accepting_submissions": current_round.accepting_submissions(),
                "opened_epoch": current_round.opened_epoch,
            }
    except Exception:
        logger.warning("Failed to load solver round for /health", exc_info=True)
    return data


# ── mount routers ────────────────────────────────────────────────────────────

app.include_router(apps.router, prefix="/v1")
app.include_router(chains.router, prefix="/v1")
app.include_router(wallets.router, prefix="/v1")
app.include_router(monitoring.router, prefix="/v1")
app.include_router(submissions.router, prefix="/v1")
app.include_router(orders.router, prefix="/v1")
app.include_router(native_bittensor.router, prefix="/v1")

# Local-testnet routes: Anvil faucet, direct subtensor stake, arbitrary-Python
# strategy replay. Off by default; opt-in via LOCAL_TESTNET=1. The local
# testnet compose sets this; production deployments leave it unset so these
# handlers are never registered on the route table — defense in depth beyond
# the per-handler auth gates.
if os.environ.get("LOCAL_TESTNET", "").strip() == "1":
    from minotaur_subnet.api.routes import local_testnet
    app.include_router(local_testnet.router, prefix="/v1")
    logger.info("LOCAL_TESTNET=1: mounting dev-only routes (faucet, direct-stake, replay-debug)")

# /identity is registered WITHOUT the /v1 prefix to mirror the validator
# daemon's convention (port 9100). Peer-discovery code can probe the same
# path regardless of which port it's hitting.
app.include_router(identity.router)


def main() -> None:
    """Run the API server with uvicorn."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="App Intents REST API")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="Listen port")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    # Prevent dual-module issue: when run via `python -m`, the module loads as
    # __main__ but other modules import it by its full name, creating a second
    # instance with a separate store.  Alias __main__ so all imports resolve to
    # the same module and the same store object.
    sys.modules.setdefault("minotaur_subnet.api.server", sys.modules[__name__])
    main()
