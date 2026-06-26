"""
Server initialization and shutdown logic.

Extracted from server.py to keep the main module slim.  All mutable state
is written to the ServerContext instance (``ctx``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from minotaur_subnet.api.server_context import ServerContext
from minotaur_subnet.epoch import SolverRoundEpochClock

logger = logging.getLogger(__name__)


# ── small helpers (pure functions, no state) ─────────────────────────────────


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _is_real_chain_url(url: str) -> bool:
    """Return True if *url* points to a real chain (not Anvil/localhost)."""
    lower = url.lower()
    return not any(kw in lower for kw in ("anvil", "localhost", "127.0.0.1", "0.0.0.0"))


def _champion_axon_to_api_url(axon_url: str) -> str:
    """Swap a discovered validator axon (:9100 daemon) to its co-located api port
    for champion-consensus broadcasts. Prod runs api and daemon on the same host."""
    import urllib.parse
    try:
        port = int(os.environ.get("CHAMPION_CONSENSUS_PEER_PORT", "8080"))
        parts = urllib.parse.urlsplit(axon_url)
        host = parts.hostname or ""
        netloc = f"{host}:{port}"
        if parts.username:
            netloc = f"{parts.username}@{netloc}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        logger.warning("champion peer url transform failed for %s; using original", axon_url)
        return axon_url


def _looks_like_mainnet_bittensor_target(target: str) -> bool:
    """Return whether a target string appears to reference Bittensor mainnet."""
    raw = (target or "").strip().lower()
    if not raw:
        return False
    if raw in {"finney", "mainnet"}:
        return True
    return any(
        marker in raw
        for marker in (
            "entrypoint-finney.opentensor.ai",
            "lite.chain.opentensor.ai",
        )
    )


def _looks_like_local_or_test_subtensor_url(url: str) -> bool:
    """Return whether a SUBTENSOR_URL looks like a local or test endpoint."""
    from urllib.parse import urlparse

    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "0.0.0.0", "::1"}:
        return True
    if hostname.startswith("127."):
        return True
    return any(marker in hostname for marker in ("test", "local"))


def _validate_native_bittensor_demo_guard(
    *,
    mvp_demo_mode: bool,
    native_proxy_requested: bool,
    subtensor_url: str,
    resolved_target: str,
) -> tuple[bool, str]:
    """Validate MVP demo-mode safety guard for native Bittensor execution."""
    if not mvp_demo_mode or not native_proxy_requested:
        return True, ""
    if not subtensor_url.strip():
        return (
            False,
            "MVP_DEMO_MODE requires explicit SUBTENSOR_URL when native Bittensor "
            "proxy execution is enabled",
        )
    if not _looks_like_local_or_test_subtensor_url(subtensor_url):
        return (
            False,
            "MVP_DEMO_MODE requires SUBTENSOR_URL to point to a local or test "
            "subtensor endpoint",
        )
    if _looks_like_mainnet_bittensor_target(resolved_target):
        return (
            False,
            "MVP_DEMO_MODE forbids native Bittensor proxy execution against "
            f"mainnet/finney target: {resolved_target}",
        )
    return True, ""


def _resolve_solver_round_hotkey() -> str | None:
    """Resolve the hotkey SS58 used for solver-round leader election."""
    explicit = os.environ.get("VALIDATOR_HOTKEY_SS58", "").strip()
    if explicit:
        return explicit

    wallet_name = os.environ.get("WALLET_NAME", "").strip()
    hotkey_name = os.environ.get("HOTKEY_NAME", "default").strip() or "default"
    if not wallet_name:
        return None

    try:
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name)
        return wallet.hotkey.ss58_address
    except Exception:
        logger.warning(
            "Failed to resolve solver-round hotkey from wallet %s/%s",
            wallet_name,
            hotkey_name,
            exc_info=True,
        )
        return None


def _resolve_native_bittensor_target() -> str:
    """Return the effective native Bittensor target using executor precedence."""
    return (
        os.environ.get("NATIVE_BITTENSOR_NETWORK", "").strip()
        or os.environ.get("SUBTENSOR_URL", "").strip()
        or os.environ.get("SUBTENSOR_NETWORK", "finney").strip()
        or "finney"
    )


def _round_anchor_chains() -> list[int]:
    """Benchmark chains to pin — the fleet-uniform CODE constant (Base only).

    No longer an env: the chain set folds into the pin and thus the pack hash, so
    it must be identical fleet-wide (a 3rd-party override would split the fleet).
    See ``consensus.round_anchor.ROUND_ANCHOR_CHAINS`` (the single source of truth).
    """
    from minotaur_subnet.consensus.round_anchor import ROUND_ANCHOR_CHAINS
    return list(ROUND_ANCHOR_CHAINS)


def _round_anchor_rpc_timeout() -> float:
    """Per-request timeout (seconds) for fork-pin RPC reads. Default 10s."""
    try:
        return max(1.0, float(os.environ.get("ROUND_ANCHOR_RPC_TIMEOUT", "10")))
    except ValueError:
        return 10.0


def _derive_round_fork_pins(anchor_epoch: int) -> dict[int, int] | None:
    """Canonical per-chain fork pins for the round's epoch anchor, or None.

    Anchor timestamp = ``anchor_epoch * epoch_seconds`` (deterministic, no chain
    read). Reads each chain's LIVE upstream RPC (never the sim fork) via
    ``_chain_rpc_env`` — the same source chain_corpus uses. All determinism lives
    in the pure ``consensus.round_anchor``; this is only the live adapter. Returns
    None (defer / live-head) on any unavailability — never a guess.
    """
    from minotaur_subnet.epoch.clock import SolverRoundEpochClock
    from minotaur_subnet.consensus.app_registry_cache import _chain_rpc_env
    from minotaur_subnet.consensus.round_anchor import (
        ROUND_ANCHOR_CONFIRMATIONS,
        ForkPinUnavailable,
        derive_fork_pins,
        epoch_anchor_ts,
    )

    epoch_seconds = SolverRoundEpochClock.from_env().epoch_seconds
    anchor_ts = epoch_anchor_ts(anchor_epoch, epoch_seconds)
    chains = _round_anchor_chains()
    confirmations = ROUND_ANCHOR_CONFIRMATIONS  # fleet-uniform code constant (was env)

    from web3 import Web3

    w3_cache: dict[int, object] = {}

    timeout_s = _round_anchor_rpc_timeout()

    def _w3(chain_id: int):
        if chain_id not in w3_cache:
            rpc = _chain_rpc_env(chain_id)
            if not rpc:
                raise ForkPinUnavailable(
                    f"no live RPC for chain {chain_id} (set *_UPSTREAM_RPC_URL)"
                )
            # Bounded per-request timeout: this runs synchronously, and on the
            # leader the same event loop also drives the order-execution
            # BlockLoop. An unbounded HTTP read on a stuck RPC would block the
            # loop (and stall order proposing); the timeout caps the worst case.
            w3_cache[chain_id] = Web3(
                Web3.HTTPProvider(rpc, request_kwargs={"timeout": timeout_s})
            )
        return w3_cache[chain_id]

    try:
        return derive_fork_pins(
            anchor_ts,
            chains,
            head_of=lambda c: int(_w3(c).eth.block_number),
            block_timestamp_of=lambda c, b: int(_w3(c).eth.get_block(b)["timestamp"]),
            confirmations=confirmations,
        )
    except ForkPinUnavailable as exc:
        logger.info("fork-pins: deferring for epoch %s: %s", anchor_epoch, exc)
        return None
    except Exception as exc:
        logger.warning("fork-pins: derivation failed for epoch %s: %s", anchor_epoch, exc)
        return None


def _maybe_populate_round_fork_pins(round_id: str, anchor_epoch: int) -> None:
    """Leader-side: derive + store the round's canonical fork pins (gated).

    Called before the leader builds ``benchmark_pack_hash`` so the pins enter the
    hash. Default-off and best-effort: with the gate off, or on any derivation
    failure, ``fork_pins`` stays unset → the pack hash is unchanged and the
    benchmark runs at live head (inert). Followers derive their own independently
    (P3); divergence surfaces as PACK_HASH_MISMATCH, never a silent mis-score.
    """
    from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
    if not round_anchored_pin_enabled():
        return
    pins = _derive_round_fork_pins(anchor_epoch)
    if not pins:
        return
    try:
        from minotaur_subnet.api.routes import submissions
        submissions.get_round_store().set_round_fork_pins(round_id, pins)
        logger.info(
            "fork-pins: round %s pinned %s (anchor epoch %s)", round_id, pins, anchor_epoch,
        )
    except Exception as exc:
        logger.warning("fork-pins: store failed for round %s: %s", round_id, exc)


def _resolve_round_fork_pins(round_id: str) -> dict[int, int] | None:
    """Resolve the round's canonical fork pins, deriving + caching if absent.

    Gated by ``ROUND_ANCHORED_PIN``. Returns ``RoundState.fork_pins`` when already
    set (leader populated at close, or a prior resolve); otherwise derives them
    independently from the round's ``close_epoch`` anchor and caches them. Returns
    None (defer / live head) when the gate is off, the round is unknown or not yet
    closed, or derivation defers.

    This is what gives followers Option-b parity: each validator derives the same
    pin from the same anchor, with no trust in a leader-asserted number.
    """
    from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
    if not round_anchored_pin_enabled():
        return None
    try:
        from minotaur_subnet.api.routes import submissions
        store = submissions.get_round_store()
        round_state = store.get_round(round_id)
    except Exception as exc:
        logger.warning("fork-pins: round lookup failed for %s: %s", round_id, exc)
        return None
    if round_state is None:
        return None
    cached = getattr(round_state, "fork_pins", None)
    if cached:
        return cached
    close_epoch = getattr(round_state, "close_epoch", None)
    if close_epoch is None:
        return None  # not closed yet -> no anchor
    pins = _derive_round_fork_pins(int(close_epoch))
    if pins:
        try:
            store.set_round_fork_pins(round_id, pins)  # cache for reuse
        except Exception as exc:
            logger.warning("fork-pins: cache store failed for %s: %s", round_id, exc)
    return pins


def _leader_fork_pin_resolver(round_id: str) -> int | None:
    """Benchmark-chain fork block for the leader's run_once, or None.

    Thin adapter over `_resolve_round_fork_pins` for injection into the
    BenchmarkWorker (keeps the harness worker free of any API-layer import).
    Returns the pin for the primary benchmark chain; None when the gate is off or
    unresolved. The leader and every follower derive the same value from the same
    anchor — that is the parity.
    """
    pins = _resolve_round_fork_pins(round_id)
    if not pins:
        return None
    return pins.get(_round_anchor_chains()[0])


def _round_anchored_pin_segment(round_id: str) -> str:
    """Canonical per-chain fork pins for the round, serialized for the pack hash.

    Returns ``""`` — leaving ``benchmark_pack_hash`` byte-for-byte unchanged —
    unless ``ROUND_ANCHORED_PIN`` is on and pins resolve. The resolver derives
    them if absent, so a *follower* computing its pre-flight pack hash gets the
    same pins the leader did (independently); divergence surfaces as
    PACK_HASH_MISMATCH (fail-loud) rather than a silent mis-score.
    """
    pins = _resolve_round_fork_pins(round_id)
    if not pins:
        return ""
    from minotaur_subnet.consensus.round_anchor import serialize_fork_pins
    return serialize_fork_pins(pins)


def _maybe_shadow_log_round_fork_pins(
    ctx: "ServerContext",
    round_id: str,
    *,
    role: str,
    anchor_epoch: int | None = None,
) -> None:
    """Shadow phase (spec §6 step 2): derive + log the round-anchored fork pins
    and the ``benchmark_pack_hash`` they *would* produce, with **zero consensus
    effect**.

    Enabled by ``ROUND_ANCHOR_SHADOW`` (default-off) and only active while the
    real gate ``ROUND_ANCHORED_PIN`` is OFF — when the gate is on the live path
    already derives, binds and logs the pins, so shadow would be redundant. This
    closes the rollout gap where, with the gate off, ``_derive_round_fork_pins``
    is never called, so operators have no way to confirm every validator computes
    the identical pin *before* flipping the gate.

    Strictly observational and best-effort:

    * never stores ``RoundState.fork_pins`` (so the real pack hash is unchanged),
    * never feeds the worker or corpus,
    * derives via the same live adapter (:func:`_derive_round_fork_pins`) every
      node uses, so ``[round-anchor-shadow]`` lines can be diffed across the
      fleet to prove pin parity,
    * swallows every error — a shadow log must never perturb the round.

    ``anchor_epoch`` is the round's close epoch when the caller already has it
    (leader at close). When ``None`` (follower path) it is resolved from the
    round store, mirroring :func:`_resolve_round_fork_pins`.
    """
    from minotaur_subnet.consensus.round_anchor import round_anchored_pin_enabled
    if not _env_true("ROUND_ANCHOR_SHADOW", default=False):
        return
    if round_anchored_pin_enabled():
        return  # live path already derives/binds/logs — shadow is redundant
    try:
        if anchor_epoch is None:
            from minotaur_subnet.api.routes import submissions
            round_state = submissions.get_round_store().get_round(round_id)
            anchor_epoch = (
                getattr(round_state, "close_epoch", None)
                if round_state is not None
                else None
            )
        if anchor_epoch is None:
            return  # round not closed yet → no anchor to derive from
        pins = _derive_round_fork_pins(int(anchor_epoch))
        if not pins:
            logger.info(
                "[round-anchor-shadow] role=%s round=%s anchor_epoch=%s pins=deferred "
                "(unavailable/derivation-skipped)",
                role, round_id, anchor_epoch,
            )
            return
        from minotaur_subnet.consensus.round_anchor import serialize_fork_pins
        pin_segment = serialize_fork_pins(pins)
        actual_pack_hash = _build_solver_round_benchmark_pack_hash(ctx, round_id)
        would_be_pack_hash = _build_solver_round_benchmark_pack_hash(
            ctx, round_id, shadow_pin_segment=pin_segment
        )
        logger.info(
            "[round-anchor-shadow] role=%s round=%s anchor_epoch=%s pins=%s "
            "pin_segment=%s actual_pack_hash=%s would_be_pack_hash=%s",
            role, round_id, anchor_epoch, pins, pin_segment,
            actual_pack_hash, would_be_pack_hash,
        )
    except Exception as exc:  # observe-only — must never break the round
        logger.warning(
            "[round-anchor-shadow] logging failed for round %s (ignored): %s",
            round_id, exc,
        )


def _round_anchor_parity_enabled() -> bool:
    """Whether the /health parity probe runs. Default-ON (opt-out)."""
    return _env_true("ROUND_ANCHOR_PARITY", default=True)


def _fetch_pin_block_hashes(pins: dict[int, int]) -> dict[str, str]:
    """Block hash at each pinned block, per chain — the fleet determinism probe.

    Two validators that derive the same pin AND read the same block hash are
    forking byte-identical chain state, so their on-chain sims are deterministic;
    a hash mismatch means their upstream RPCs disagree (reorg / archive
    inconsistency) and ``ADOPT_RULE=p2oc`` must NOT be flipped. Unlike the pack
    hash, this needs no corpus flag, so it is comparable across the fleet by
    polling /health alone. One ``eth_getBlockByNumber`` per chain against the
    same live upstream RPC the pin derivation uses, bounded timeout, best-effort
    (a missing/failed hash is omitted, never raises — a probe must not break
    /health).
    """
    from web3 import Web3
    from minotaur_subnet.consensus.app_registry_cache import _chain_rpc_env

    timeout_s = _round_anchor_rpc_timeout()
    out: dict[str, str] = {}
    for chain_id, block in sorted(pins.items()):
        try:
            rpc = _chain_rpc_env(int(chain_id))
            if not rpc:
                continue
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": timeout_s}))
            block_hash = w3.eth.get_block(int(block)).get("hash")
            if block_hash is not None:
                out[str(chain_id)] = (
                    block_hash.hex() if hasattr(block_hash, "hex") else str(block_hash)
                )
        except Exception:  # best-effort — never break the /health probe
            continue
    return out


def _compute_round_anchor_parity_snapshot(anchor_epoch: int) -> dict:
    """Derive the current-epoch fork pins and package a /health-ready snapshot.

    Pure observability: the result is NEVER stored on a round nor bound into a
    pack hash. Runs in a worker thread (see :func:`_round_anchor_parity_loop`)
    so its synchronous RPC reads never touch the event loop. ``status`` is
    ``ok`` (pins derived), ``deferred`` (anchor not yet confirmation-bracketed
    or no RPC), and the caller maps timeouts/errors to their own statuses.
    """
    from minotaur_subnet.consensus.round_anchor import (
        ROUND_ANCHOR_CONFIRMATIONS,
        round_anchored_pin_enabled,
        serialize_fork_pins,
    )

    chains = _round_anchor_chains()
    confirmations = ROUND_ANCHOR_CONFIRMATIONS  # fleet-uniform code constant (was env)
    pins = _derive_round_fork_pins(anchor_epoch)
    if pins:
        pin_map = {str(chain): int(block) for chain, block in sorted(pins.items())}
        pin_segment = serialize_fork_pins(pins)
        pin_hashes = _fetch_pin_block_hashes(pins)
        status = "ok"
    else:
        pin_map = {}
        pin_segment = ""
        pin_hashes = {}
        status = "deferred"
    return {
        "status": status,
        "anchor_epoch": int(anchor_epoch),
        "chains": chains,
        "confirmations": confirmations,
        "pins": pin_map,
        "pin_hashes": pin_hashes,
        "pin_segment": pin_segment,
        "gate_enabled": round_anchored_pin_enabled(),
        "derived_at": int(time.time()),
    }


async def _round_anchor_parity_loop(ctx: "ServerContext") -> None:
    """Background probe keeping ``ctx.round_anchor_parity`` fresh for /health.

    Every validator (leader and follower) independently derives the canonical
    fork pin for the current epoch anchor and publishes it on /health, so fleet
    pin parity can be confirmed by polling /health — no log access, no operator
    action, and decoupled from the (possibly dormant) champion-consensus path.

    Safety: derivation runs in a thread with a bounded RPC timeout, so it can
    never block the event loop or the order-execution BlockLoop sharing it. The
    pin for an anchor epoch is immutable once confirmed, so we re-derive only
    when the epoch advances (keeps RPC load to ~one derivation per epoch).
    """
    from minotaur_subnet.epoch.clock import SolverRoundEpochClock

    try:
        refresh = max(5.0, float(os.environ.get("ROUND_ANCHOR_PARITY_INTERVAL", "60")))
    except ValueError:
        refresh = 60.0
    overall_timeout = max(refresh, _round_anchor_rpc_timeout() * 4)
    loop = asyncio.get_running_loop()
    while True:
        anchor_epoch: int | None = None
        try:
            epoch_seconds = max(1, int(SolverRoundEpochClock.from_env().epoch_seconds))
            # One epoch back → the anchor is comfortably confirmation-bracketed
            # (the current epoch's boundary is ~now and would usually defer).
            anchor_epoch = max(int(time.time()) // epoch_seconds - 1, 0)
            prev = ctx.round_anchor_parity or {}
            already_good = (
                prev.get("anchor_epoch") == anchor_epoch and prev.get("status") == "ok"
            )
            if not already_good:
                snapshot = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, _compute_round_anchor_parity_snapshot, anchor_epoch
                    ),
                    timeout=overall_timeout,
                )
                ctx.round_anchor_parity = snapshot
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            ctx.round_anchor_parity = {
                "status": "timeout",
                "anchor_epoch": anchor_epoch,
                "derived_at": int(time.time()),
            }
            logger.warning(
                "[round-anchor-parity] derivation timed out for epoch %s", anchor_epoch
            )
        except Exception as exc:
            logger.warning("[round-anchor-parity] probe iteration failed: %s", exc)
        await asyncio.sleep(refresh)


def _build_solver_round_benchmark_pack_hash(
    ctx: ServerContext,
    round_id: str,
    *,
    shadow_pin_segment: str | None = None,
) -> str:
    """Build a deterministic benchmark-pack hash covering:
    1. Round metadata (app list, submissions, policy)
    2. Stage 1 synthetic scenarios from every app's manifest
    3. Stage 2 historical order IDs (deterministic sample from round_id)

    All validators compute the same hash from the same round_id and local
    state. If any validator's manifests or order history differ, pack hashes
    will diverge and consensus will fail — forcing resync before adoption.
    """
    from minotaur_subnet.api.routes import submissions
    from minotaur_subnet.harness.benchmark_pack import (
        compute_pack_hash,
        collect_synthetic_scenarios,
    )
    from minotaur_subnet.harness.order_sampler import sample_historical_orders

    submission_store = submissions.get_store()
    round_subs = submission_store.list_by_round(round_id)
    apps_payload = [
        {
            "app_id": app.app_id,
            "name": app.name,
            "intent_type": getattr(app, "intent_type", ""),
            "supported_chains": list(getattr(app.config, "supported_chains", []) or []),
            "trigger_type": (
                getattr(getattr(app.config, "trigger_type", None), "value", None)
                or str(getattr(app.config, "trigger_type", "") or "")
            ),
        }
        for app in sorted(ctx.store.list_apps(), key=lambda item: item.app_id)
    ]
    submissions_payload = [
        {
            "submission_id": sub.submission_id,
            "hotkey": sub.hotkey,
            "repo_url": sub.repo_url,
            "commit_hash": sub.commit_hash,
            "image_id": sub.image_id,
            "solver_name": sub.solver_name,
            "solver_version": sub.solver_version,
            "status": sub.status.value,
        }
        for sub in sorted(round_subs, key=lambda item: item.submission_id)
    ]
    # Canonical hash of scenarios (Stage 1 + Stage 2)
    try:
        synthetic_scenarios = collect_synthetic_scenarios(ctx.store)
    except Exception as exc:
        logger.warning("pack_hash: synthetic scenario collection failed: %s", exc)
        synthetic_scenarios = []

    try:
        historical_orders = sample_historical_orders(
            app_store=ctx.store,
            round_id=round_id,
        )
        historical_order_ids = [o.get("order_id", "") for o in historical_orders]
    except Exception as exc:
        logger.warning("pack_hash: historical sampling failed: %s", exc)
        historical_order_ids = []

    # Fold the block-pin rewrite-table version AND the deterministic compute
    # budget into the pack hash when (and only when) this round routes solver
    # reads through the proxy (proxy configured + round pinned), with the budget
    # folded additionally only when it's enforced (positive). Inert otherwise —
    # byte-identical to a non-proxy / non-budget fleet, so a divergent budget or
    # rewrite version drops out of quorum (PACK_HASH_MISMATCH, loud).
    from minotaur_subnet.harness.solver_read_proxy import (
        pack_hash_block_rewrite,
        pack_hash_compute_budget,
    )

    scenario_hash = compute_pack_hash(
        round_id,
        synthetic_scenarios,
        historical_order_ids,
        compute_budget=pack_hash_compute_budget(),
        block_rewrite=pack_hash_block_rewrite(),
    )

    payload = {
        "round_id": round_id,
        "apps": apps_payload,
        "submissions": submissions_payload,
        "scenario_hash": scenario_hash,
        "historical_order_count": len(historical_order_ids),
        "synthetic_scenario_count": len(synthetic_scenarios),
        "policy": {
            "allow_subprocess_benchmark": _env_true("ALLOW_SUBPROCESS_BENCHMARK", default=False),
            "require_signed_provenance": _env_true("REQUIRE_SIGNED_PROVENANCE", default=False),
            "require_asymmetric_provenance": _env_true("REQUIRE_ASYMMETRIC_PROVENANCE", default=False),
        },
    }
    # Bind the round-anchored fork pins into the pack hash (gated, default-off).
    # Added only when present so the default hash is byte-for-byte unchanged.
    #
    # ``shadow_pin_segment`` lets the shadow logger
    # (:func:`_maybe_shadow_log_round_fork_pins`) compute the *would-be* hash for
    # an explicit segment WITHOUT activating the gate. Real call sites never pass
    # it, so production behavior is unchanged.
    if shadow_pin_segment is not None:
        pin_segment = shadow_pin_segment
    else:
        pin_segment = _round_anchored_pin_segment(round_id)
    if pin_segment:
        payload["fork_pins"] = pin_segment
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "0x" + hashlib.sha256(encoded).hexdigest()


def _build_provenance_health_snapshot(
    *,
    require_signed: bool,
    require_asymmetric: bool,
    submissions_accepting: bool,
    hmac_key: str,
    allowed_signers: set[str],
    signing_private_key: str,
    policy_ok: bool,
    policy_error: str,
    startup_validated: bool,
) -> dict:
    verifier_configured = bool(allowed_signers) if require_asymmetric else bool(
        hmac_key or allowed_signers
    )
    signer_configured = bool(signing_private_key) if require_asymmetric else bool(
        signing_private_key or hmac_key
    )
    mode = (
        "asymmetric_only"
        if require_asymmetric
        else "signed_required"
        if require_signed
        else "optional"
    )
    return {
        "valid": bool(policy_ok),
        "startup_validated": bool(startup_validated),
        "mode": mode,
        "require_signed": bool(require_signed),
        "require_asymmetric": bool(require_asymmetric),
        "submissions_accepting": bool(submissions_accepting),
        "signer_configured": bool(signer_configured),
        "verifier_configured": bool(verifier_configured),
        "allowed_signers_count": len(allowed_signers),
        "hmac_configured": bool(hmac_key),
        "error": policy_error if not policy_ok else "",
    }


def _build_runtime_security_health_snapshot(
    *,
    enforce: bool,
    violations: list[str],
    enable_source_submissions: bool,
    allow_subprocess_benchmark: bool,
    require_signed_provenance: bool,
    require_asymmetric_provenance: bool,
    allowed_signers: set[str],
    hmac_key: str,
    submissions_accepting: bool,
    submissions_api_key_configured: bool,
    submissions_rate_limit_per_minute: int,
    startup_validated: bool,
) -> dict:
    return {
        "valid": len(violations) == 0,
        "startup_validated": bool(startup_validated),
        "enforced": bool(enforce),
        "violations": list(violations),
        "enable_source_submissions": bool(enable_source_submissions),
        "allow_subprocess_benchmark": bool(allow_subprocess_benchmark),
        "require_signed_provenance": bool(require_signed_provenance),
        "require_asymmetric_provenance": bool(require_asymmetric_provenance),
        "allowed_signers_count": len(allowed_signers),
        "hmac_configured": bool(hmac_key),
        "submissions_accepting": bool(submissions_accepting),
        "submissions_api_key_configured": bool(submissions_api_key_configured),
        "submissions_rate_limit_per_minute": int(submissions_rate_limit_per_minute),
    }


# ── epoch helpers ────────────────────────────────────────────────────────────


def _solver_round_epoch_block_number(ctx: ServerContext) -> int | None:
    """Return the latest metagraph block if available."""
    if ctx.solver_round_metagraph_sync is None or ctx.solver_round_metagraph_sync.state is None:
        return None
    return int(ctx.solver_round_metagraph_sync.state.block)


def _solver_round_native_epoch_info(ctx: ServerContext) -> object | None:
    """Return native subnet epoch metadata from metagraph sync when available."""
    if ctx.solver_round_metagraph_sync is None or ctx.solver_round_metagraph_sync.state is None:
        return None
    return getattr(ctx.solver_round_metagraph_sync.state, "epoch", None)


def _solver_round_native_epoch(ctx: ServerContext) -> int | None:
    """Return the exact subnet epoch index when metagraph sync provides it."""
    epoch_info = _solver_round_native_epoch_info(ctx)
    if epoch_info is None:
        return None
    value = getattr(epoch_info, "epoch_index", None)
    return int(value) if value is not None else None


def _solver_round_native_epoch_length_blocks(ctx: ServerContext) -> int | None:
    """Return the exact subnet epoch length in blocks when available."""
    epoch_info = _solver_round_native_epoch_info(ctx)
    if epoch_info is None:
        return None
    value = getattr(epoch_info, "epoch_length_blocks", None)
    return int(value) if value is not None else None


def _solver_round_native_blocks_since_last_step(ctx: ServerContext) -> int | None:
    """Return the native block progress within the current subnet epoch."""
    epoch_info = _solver_round_native_epoch_info(ctx)
    if epoch_info is None:
        return None
    value = getattr(epoch_info, "blocks_since_last_step", None)
    return int(value) if value is not None else None


def _current_solver_round_epoch(ctx: ServerContext) -> int:
    """Return the current solver round epoch from the configured clock.

    Uses wall-clock time exclusively for round management. Native subtensor
    epochs cause instability when the connection is intermittent (flipping
    between epoch 200 and epoch 29M), causing rounds to close immediately.
    Native epoch data is still available in the health snapshot for debugging.
    """
    clock = ctx.solver_round_epoch_clock or SolverRoundEpochClock.from_env()
    # Force time-based epoch -- don't pass native_epoch or block_number
    return clock.current_epoch()


def _solver_round_epoch_health(ctx: ServerContext) -> dict[str, object]:
    """Return clock metadata for health/debug endpoints."""
    clock = ctx.solver_round_epoch_clock or SolverRoundEpochClock.from_env()
    return clock.health_snapshot(
        block_number=_solver_round_epoch_block_number(ctx),
        native_epoch=_solver_round_native_epoch(ctx),
        native_epoch_length_blocks=_solver_round_native_epoch_length_blocks(ctx),
        native_blocks_since_last_step=_solver_round_native_blocks_since_last_step(ctx),
    )


# ── initialization ───────────────────────────────────────────────────────────


async def initialize(ctx: ServerContext) -> dict:
    """Run all startup initialization.  Populates ``ctx`` fields.

    Returns a dict of local-only objects needed during shutdown but NOT
    stored on ctx (bridge_tracker_task, champion_peer_network, etc.).
    """
    # Step 0a: hydrate signing keys from AWS Secrets Manager before any
    # other module reads os.environ. Env values that are already set are
    # preserved — SM is fallback, not override. No-op if boto3 is missing
    # or the instance lacks the IAM role.
    from minotaur_subnet.api.secrets_loader import hydrate_env_from_secrets_manager
    _outcome = hydrate_env_from_secrets_manager()
    # Step 0b: required-env sanity check. Runs AFTER Secrets Manager
    # hydration so SM-provided values aren't flagged as missing, but
    # BEFORE any other startup logic — operators who forgot
    # ``cp .env.example .env`` get a single actionable diagnostic
    # instead of the cryptic "Champion consensus enabled but no
    # VALIDATOR_REGISTRY_964 configured" error several hundred lines
    # deep into startup.
    from minotaur_subnet.shared.env_check import (
        REQUIRED_REGISTRY_ENV,
        check_required_env_or_exit,
    )
    check_required_env_or_exit(REQUIRED_REGISTRY_ENV, process_name="api")
    if _outcome.env_vars_set:
        logger.info(
            "[secrets] hydrated %d signing key(s) from Secrets Manager",
            _outcome.env_vars_set,
        )

    from minotaur_subnet.api.routes import (
        apps,
        orders,
        submissions,
    )
    from minotaur_subnet.harness.provenance import (
        parse_allowed_signers,
        validate_provenance_policy,
        validate_runtime_security_profile,
    )

    # Locals that only live for the lifespan scope (shutdown needs them).
    locals_bag: dict = {
        "bridge_tracker": None,
        "bridge_tracker_task": None,
        "champion_peer_network": None,
        "order_peer_network": None,
    }

    # Refuse to boot if a configured on-chain contract has no bytecode at the
    # referenced address/chain. Skips unset env vars (feature disabled is fine).
    # Opt out for local dev where contracts get deployed after the API starts:
    #   SKIP_CONTRACT_PRESENCE_CHECK=1
    if not _env_true("SKIP_CONTRACT_PRESENCE_CHECK", default=False):
        from minotaur_subnet.api.contract_checks import (
            ContractPresenceError,
            verify_required_contracts,
        )
        try:
            verified = verify_required_contracts()
            for v in verified:
                logger.info("[contract-check] %s", v)
        except ContractPresenceError:
            logger.error("[contract-check] boot refused — fix the configured "
                         "addresses or set SKIP_CONTRACT_PRESENCE_CHECK=1 for dev")
            raise

    round_store = submissions.get_round_store()
    submissions.set_epoch_manager(None)
    submissions.set_champion_consensus_manager(None)
    submissions.set_champion_peer_network(None)
    submissions.set_solver_round_epoch_provider(None)
    ctx.solver_round_metagraph_sync = None
    ctx.solver_round_metagraph_task = None
    ctx.solver_round_role_task = None
    ctx.solver_round_role = "standalone"
    ctx.solver_round_epoch_clock = SolverRoundEpochClock.from_env()
    submissions.set_solver_round_epoch_provider(
        lambda: _current_solver_round_epoch(ctx),
    )

    # ── provenance policy ────────────────────────────────────────────────
    require_signed_provenance = _env_true("REQUIRE_SIGNED_PROVENANCE", default=False)
    require_asymmetric_provenance = _env_true(
        "REQUIRE_ASYMMETRIC_PROVENANCE",
        default=False,
    )
    submissions_accepting = _env_true("SUBMISSIONS_ACCEPTING", default=True)
    provenance_hmac_key = os.environ.get("SUBMISSION_PROVENANCE_HMAC_KEY", "").strip()
    provenance_signing_private_key = os.environ.get(
        "SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY",
        "",
    ).strip()
    provenance_signing_address = os.environ.get(
        "SUBMISSION_PROVENANCE_SIGNING_ADDRESS",
        "",
    ).strip()
    provenance_allowed_signers = parse_allowed_signers(
        os.environ.get("SUBMISSION_PROVENANCE_ALLOWED_SIGNERS", "").strip(),
    )
    policy_ok, policy_error = validate_provenance_policy(
        require_signed=require_signed_provenance,
        require_asymmetric=require_asymmetric_provenance,
        hmac_key=provenance_hmac_key,
        allowed_signers=provenance_allowed_signers,
        signing_private_key=provenance_signing_private_key,
        signing_address=provenance_signing_address,
        submissions_accepting=submissions_accepting,
    )
    ctx.provenance_policy_health = _build_provenance_health_snapshot(
        require_signed=require_signed_provenance,
        require_asymmetric=require_asymmetric_provenance,
        submissions_accepting=submissions_accepting,
        hmac_key=provenance_hmac_key,
        allowed_signers=provenance_allowed_signers,
        signing_private_key=provenance_signing_private_key,
        policy_ok=policy_ok,
        policy_error=policy_error,
        startup_validated=True,
    )
    if not policy_ok:
        raise RuntimeError(f"Invalid provenance policy configuration: {policy_error}")

    # ── runtime security profile ─────────────────────────────────────────
    enforce_runtime_security = _env_true("ENFORCE_RUNTIME_SECURITY_PROFILE", default=False)
    enable_source_submissions = _env_true("ENABLE_SOURCE_SUBMISSIONS", default=False)
    allow_subprocess_benchmark = _env_true("ALLOW_SUBPROCESS_BENCHMARK", default=False)
    submissions_api_key = os.environ.get("SUBMISSIONS_API_KEY", "").strip()
    raw_rate_limit = os.environ.get("SUBMISSIONS_RATE_LIMIT_PER_MINUTE", "60").strip()
    try:
        submissions_rate_limit_per_minute = int(raw_rate_limit)
    except ValueError:
        submissions_rate_limit_per_minute = 60

    runtime_ok, runtime_violations = validate_runtime_security_profile(
        enforce=enforce_runtime_security,
        enable_source_submissions=enable_source_submissions,
        allow_subprocess_benchmark=allow_subprocess_benchmark,
        require_signed=require_signed_provenance,
        require_asymmetric=require_asymmetric_provenance,
        hmac_key=provenance_hmac_key,
        allowed_signers=provenance_allowed_signers,
        submissions_accepting=submissions_accepting,
        submissions_api_key=submissions_api_key,
        submissions_rate_limit_per_minute=submissions_rate_limit_per_minute,
    )
    ctx.runtime_security_policy_health = _build_runtime_security_health_snapshot(
        enforce=enforce_runtime_security,
        violations=runtime_violations,
        enable_source_submissions=enable_source_submissions,
        allow_subprocess_benchmark=allow_subprocess_benchmark,
        require_signed_provenance=require_signed_provenance,
        require_asymmetric_provenance=require_asymmetric_provenance,
        allowed_signers=provenance_allowed_signers,
        hmac_key=provenance_hmac_key,
        submissions_accepting=submissions_accepting,
        submissions_api_key_configured=bool(submissions_api_key),
        submissions_rate_limit_per_minute=submissions_rate_limit_per_minute,
        startup_validated=True,
    )
    if not runtime_ok:
        raise RuntimeError(
            "Invalid runtime security profile: " + "; ".join(runtime_violations),
        )

    # ── block-pin RPC proxy (api-managed) ────────────────────────────────
    # Launch the deterministic-read proxy as a managed container BEFORE the
    # benchmark worker / reactive verification so the read path is wired when the
    # first benchmark runs. The api owns it (not a compose service) so the whole
    # determinism rollout rides the normal :stable image update — zero operator
    # action. Runs on leaders (proactive bench) AND followers (reactive champion
    # verification); both score through the same run_benchmark read path. Never
    # blocks startup — on failure the read path stays unwired and benchmarks fail
    # loud (drop from quorum) rather than mis-score.
    from minotaur_subnet.api.read_proxy_manager import ensure_read_proxy_container
    try:
        await ensure_read_proxy_container()
    except Exception as exc:
        logger.error("[read-proxy] manager raised (continuing startup): %s", exc)

    # ── benchmark worker ─────────────────────────────────────────────────
    sub_store = None
    benchmark_worker_enabled = os.environ.get(
        "ENABLE_BENCHMARK_WORKER", "",
    ).lower() in ("1", "true", "yes")
    if os.environ.get("DISABLE_BENCHMARK_WORKER", "").lower() in ("1", "true", "yes"):
        benchmark_worker_enabled = False

    if benchmark_worker_enabled:
        from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker

        sub_store = submissions.get_store()
        submissions._sync_round_incumbent_from_submission_store(round_store, sub_store)
        current_round = round_store.get_current_round()
        if current_round is None:
            active_champion = round_store.get_active_champion()
            initial_epoch = (
                active_champion.activated_epoch + 1
                if active_champion.submission_id
                else 0
            )
            round_store.ensure_open_round(
                opened_epoch=initial_epoch,
                incumbent=active_champion if active_champion.submission_id else None,
            )

        poll_interval = float(os.environ.get("BENCHMARK_POLL_INTERVAL", "30"))
        _genesis_solver_image = os.environ.get("GENESIS_SOLVER_IMAGE", "").strip() or None
        from minotaur_subnet.harness.orchestrator import require_real_sim_default
        _require_real_sim = require_real_sim_default()
        ctx.benchmark_worker = BenchmarkWorker(
            submission_store=sub_store,
            app_store=ctx.store,
            round_store=round_store,
            genesis_solver_image=_genesis_solver_image,
            require_real_sim=_require_real_sim,
            pin_resolver=_leader_fork_pin_resolver,
            validator_identity=_resolve_solver_round_hotkey(),
        )
        ctx.benchmark_task = asyncio.create_task(
            ctx.benchmark_worker.run_loop(interval=poll_interval),
        )
        logger.info("Benchmark worker started (poll every %ds)", poll_interval)

    # ── relayer ──────────────────────────────────────────────────────────
    # Two modes, picked by env:
    #
    #   RELAYER_URL set  → HttpRelayer client. Validators sign quorum
    #     approvals; the api process POSTs the signed bundle to the
    #     subnet team's singleton relayer service, which verifies sigs
    #     against the on-chain ValidatorRegistry and pays gas. The api
    #     process never holds RELAYER_PRIVATE_KEY. This is the path for
    #     third-party validators (and the new prod cutover target —
    #     gas budget stays with the singleton even if leadership rotates).
    #
    #   USE_EVM_RELAYER set + RELAYER_URL unset → embedded EvmRelayer.
    #     The legacy path: the api process holds RELAYER_PRIVATE_KEY
    #     and submits directly via web3. Used by local-testnet, existing
    #     tests, and prod during the transition until we cut over to
    #     RELAYER_URL.
    #
    #   Neither set → no relayer (api stays in read-only mode, useful
    #     for the third-party canonical compose's pre-cutover phase).
    relayer_instance = None
    relayer_url = os.environ.get("RELAYER_URL", "").strip()
    use_embedded = os.environ.get("USE_EVM_RELAYER", "").lower() in ("1", "true", "yes")

    if relayer_url:
        from minotaur_subnet.relayer.http_relayer import HttpRelayer
        # The api signs a freshness wrapper around each submission. The
        # relayer rejects wrappers whose recovered signer isn't in the
        # on-chain ValidatorRegistry, so we MUST use a validator key
        # here. Same key the api uses for consensus signing.
        validator_key_for_wrapper = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
        if not validator_key_for_wrapper:
            logger.warning(
                "RELAYER_URL set but VALIDATOR_PRIVATE_KEY missing — "
                "wrapper signing will fail; submissions will be rejected by the relayer",
            )
        relayer_instance = HttpRelayer(
            url=relayer_url,
            signing_key=validator_key_for_wrapper,
        )
        logger.info(
            "Relayer mode: HTTP client → %s (no local gas wallet, wrapper-signed)",
            relayer_url,
        )
    elif use_embedded:
        from minotaur_subnet.relayer import EvmRelayer
        from minotaur_subnet.relayer.chain_config import get_supported_chains
        relayer_key = os.environ.get("RELAYER_PRIVATE_KEY", "")
        relayer_instance = EvmRelayer(
            chains=get_supported_chains(),
            private_key=relayer_key,
        )
        logger.info(
            "Relayer mode: embedded EvmRelayer (RELAYER_PRIVATE_KEY in this process). "
            "Set RELAYER_URL to use a remote singleton relayer instead.",
        )

    # ── DeployService — admin-gated contract deploys ─────────────────────
    # Reuses whichever relayer the BlockLoop is wired to. Both HttpRelayer
    # and EvmRelayer expose ``chains`` + ``deploy_contract``, so DeployService
    # is agnostic to the underlying transport. HttpRelayer routes the
    # deploy through the subnet team's singleton relayer service over HTTP
    # — the api process never holds RELAYER_PRIVATE_KEY in that path.
    #
    # No relayer wired (third-party stacks pre-RELAYER_URL, read-only API)
    # → no DeployService → admin /deploy endpoints return 503. That's
    # intentional: validators don't deploy apps.
    if relayer_instance is not None:
        from minotaur_subnet.deployment.compiler import ForgeCompiler
        from minotaur_subnet.deployment.deployer import DeployService
        from minotaur_subnet.api.services import set_deploy_service

        registry_address = os.environ.get("VALIDATOR_REGISTRY_ADDRESS", "")
        if not registry_address:
            registry_address = os.environ.get("VALIDATOR_REGISTRY_31337", "")
        # Quorum is no longer a deploy-time arg — AppIntentBase reads it
        # from the ValidatorRegistry at execution time.
        deploy_service = DeployService(
            ForgeCompiler(),
            relayer_instance,
            registry_address,
        )
        set_deploy_service(deploy_service)
        logger.info(
            "DeployService configured (registry=%s, relayer=%s)",
            registry_address[:20] if registry_address else "none",
            "HttpRelayer" if relayer_url else "EvmRelayer",
        )

    # ── chain info ───────────────────────────────────────────────────────
    # GET /v1/chains is the public chain list users route through. The
    # underlying chain_config map ALSO includes internal-only RPCs (e.g.
    # the simulation Anvil fork in prod, reached via ANVIL_RPC_URL) which
    # have no ValidatorRegistry deployed. build_public_chain_info filters
    # those out — a chain is exposed publicly only if we've stood up the
    # consensus stack on it. Local testnet still works because in that env
    # ValidatorRegistry is deployed to chain 31337 too.
    from minotaur_subnet.relayer.chain_config import get_supported_chains as _get_chains
    from minotaur_subnet.api.services import set_chain_info
    from minotaur_subnet.api.services.chain_service import build_public_chain_info
    _chains = _get_chains()
    set_chain_info(build_public_chain_info(_chains.values()))
    logger.info(
        "Chain info: public=%s, internal-only=%s",
        [c.chain_id for c in _chains.values() if c.validator_registry_address],
        [c.chain_id for c in _chains.values() if not c.validator_registry_address],
    )

    # ── wallet manager ───────────────────────────────────────────────────
    lit_bridge_url = os.environ.get("LIT_BRIDGE_URL")
    if lit_bridge_url:
        try:
            from minotaur_subnet.wallet.lit_wallet import LitMpcWallet
            from minotaur_subnet.api.services import set_wallet_manager
            wallet_mgr = LitMpcWallet(bridge_url=lit_bridge_url)
            set_wallet_manager(wallet_mgr)
            logger.info("[startup] LitMpcWallet configured (bridge=%s)", lit_bridge_url)
        except Exception as exc:
            logger.warning("[startup] LitMpcWallet init failed: %s", exc)

    # ── native Bittensor delegated execution ─────────────────────────────
    from minotaur_subnet.api.services import (
        set_native_bittensor_delegate_allocator,
        set_native_bittensor_executor,
    )

    set_native_bittensor_delegate_allocator(None)
    set_native_bittensor_executor(None)

    native_proxy_enabled = _env_true("ENABLE_NATIVE_BITTENSOR_PROXY", default=False)
    mvp_demo_mode = _env_true("MVP_DEMO_MODE", default=False)
    subtensor_url = os.environ.get("SUBTENSOR_URL", "").strip()
    delegate_assignments_raw = os.environ.get(
        "NATIVE_BITTENSOR_DELEGATE_ASSIGNMENTS_JSON",
        "",
    ).strip()
    delegate_wallets_raw = os.environ.get(
        "NATIVE_BITTENSOR_DELEGATE_WALLETS_JSON",
        "",
    ).strip()
    if native_proxy_enabled or delegate_assignments_raw or delegate_wallets_raw:
        native_target = _resolve_native_bittensor_target()
        native_guard_ok, native_guard_error = _validate_native_bittensor_demo_guard(
            mvp_demo_mode=mvp_demo_mode,
            native_proxy_requested=True,
            subtensor_url=subtensor_url,
            resolved_target=native_target,
        )
        if not native_guard_ok:
            raise RuntimeError(native_guard_error)

        from minotaur_subnet.blockchain import BittensorProxyExecutor

        assignment_map: dict[str, str] = {}
        wallet_map: dict[str, dict[str, str]] = {}

        if delegate_assignments_raw:
            try:
                parsed = json.loads(delegate_assignments_raw)
                if isinstance(parsed, dict):
                    assignment_map = {
                        str(owner): str(delegate)
                        for owner, delegate in parsed.items()
                        if delegate
                    }
                else:
                    logger.warning("NATIVE_BITTENSOR_DELEGATE_ASSIGNMENTS_JSON must be an object")
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse native delegate assignments JSON: %s", exc)

        if delegate_wallets_raw:
            try:
                parsed = json.loads(delegate_wallets_raw)
                if isinstance(parsed, dict):
                    wallet_map = {
                        str(delegate): dict(cfg)
                        for delegate, cfg in parsed.items()
                        if isinstance(cfg, dict)
                    }
                else:
                    logger.warning("NATIVE_BITTENSOR_DELEGATE_WALLETS_JSON must be an object")
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse native delegate wallets JSON: %s", exc)

        def _delegate_allocator(owner_ss58: str) -> str | None:
            return assignment_map.get(owner_ss58)

        def _delegate_wallet_loader(delegate_ss58: str):
            config = wallet_map.get(delegate_ss58)
            if not config:
                return None
            import bittensor as bt

            wallet_name = config.get("wallet_name") or config.get("name")
            if not wallet_name:
                raise KeyError(f"Missing wallet_name for delegate {delegate_ss58}")
            kwargs: dict[str, str] = {}
            wallet_path = config.get("wallet_path") or config.get("path")
            wallet_hotkey = config.get("wallet_hotkey") or config.get("hotkey")
            if wallet_path:
                kwargs["path"] = wallet_path
            if wallet_hotkey:
                kwargs["hotkey"] = wallet_hotkey
            return bt.Wallet(name=wallet_name, **kwargs)

        native_network = native_target
        executor = BittensorProxyExecutor(
            network=native_network,
            wallet_loader=_delegate_wallet_loader if wallet_map else None,
        )
        set_native_bittensor_executor(executor)
        if assignment_map:
            set_native_bittensor_delegate_allocator(_delegate_allocator)
        logger.info(
            "Native Bittensor proxy executor configured (network=%s, assignments=%d, delegate_wallets=%d)",
            native_network,
            len(assignment_map),
            len(wallet_map),
        )

    # ── faucet RPC URLs ──────────────────────────────────────────────────
    from minotaur_subnet.api.services import set_faucet_rpc_urls
    _faucet_urls: dict[int, str] = {}
    _anvil_url_early = os.environ.get("ANVIL_RPC_URL")
    _base_url_early = os.environ.get("BASE_RPC_URL")
    if _anvil_url_early:
        _faucet_urls[31337] = _anvil_url_early
        _faucet_urls[1] = _anvil_url_early
    if _base_url_early and not _is_real_chain_url(_base_url_early):
        _faucet_urls[8453] = _base_url_early
    elif _base_url_early:
        logger.warning("Real Base RPC detected -- faucet disabled for chain 8453")
    _btevm_url_early = os.environ.get("BITTENSOR_EVM_RPC_URL")
    if _btevm_url_early:
        _faucet_urls[964] = _btevm_url_early
    if _faucet_urls:
        set_faucet_rpc_urls(_faucet_urls)
        logger.info("Faucet RPC URLs: %s", list(_faucet_urls.keys()))

    # ── orderbook + block loop ───────────────────────────────────────────
    if os.environ.get("DISABLE_BLOCK_LOOP", "").lower() not in ("1", "true", "yes"):
        from minotaur_subnet.orderbook import IntentOrderBook
        from minotaur_subnet.blockloop import BlockLoop
        from minotaur_subnet.relayer import MockRelayer

        ctx.orderbook = IntentOrderBook()
        orders.set_orderbook(ctx.orderbook)
        orders.set_app_store(ctx.store)

        tick_interval = float(os.environ.get("BLOCK_LOOP_TICK_INTERVAL", "12"))
        score_threshold = float(os.environ.get("BLOCK_LOOP_SCORE_THRESHOLD", "0.5"))

        if relayer_instance is None:
            relayer_instance = MockRelayer()

        # JS engine
        js_engine = None
        if os.environ.get("DISABLE_JS_ENGINE", "").lower() not in ("1", "true", "yes"):
            try:
                from minotaur_subnet.engine import JsExecutionEngine
                js_engine = JsExecutionEngine()
                logger.info("JsExecutionEngine initialized")
            except Exception as exc:
                logger.warning("JsExecutionEngine unavailable: %s", exc)

        if js_engine is not None:
            orders.set_js_engine(js_engine)
            apps.set_js_engine(js_engine)

        # Bridge registry
        bridge_registry = None
        try:
            from minotaur_subnet.bridge import BridgeRegistry
            from minotaur_subnet.bridge.mock import MockBridgeAdapter
            from minotaur_subnet.bridge.tensorplex import TensorplexAdapter
            from minotaur_subnet.bridge.hyperlane import HyperlaneAdapter
            bridge_registry = BridgeRegistry()
            bridge_registry.register(MockBridgeAdapter())
            bridge_registry.register(TensorplexAdapter())
            bridge_registry.register(HyperlaneAdapter())
            logger.info("BridgeRegistry initialized (mock + tensorplex + hyperlane adapters)")
        except Exception as exc:
            logger.warning("BridgeRegistry unavailable: %s", exc)

        # RPC URLs
        anvil_url = os.environ.get("ANVIL_RPC_URL")
        base_url = os.environ.get("BASE_RPC_URL")
        rpc_urls: dict[int, str] = {}
        chain_ids: list[int] = [1, 31337]
        if anvil_url:
            rpc_urls[31337] = anvil_url
            rpc_urls[1] = anvil_url
        if base_url:
            rpc_urls[8453] = base_url
            chain_ids.append(8453)
        btevm_url = os.environ.get("BITTENSOR_EVM_RPC_URL")
        if btevm_url:
            rpc_urls[964] = btevm_url
            chain_ids.append(964)

        # Solver — boot from a Docker image. FORCE_SOLVER_IMAGE (operator break-glass)
        # wins over GENESIS_SOLVER_IMAGE; otherwise genesis.
        solver = None
        from minotaur_subnet.harness.runtime_solver import resolve_boot_solver_image
        _boot_image, _boot_forced = resolve_boot_solver_image()
        if _boot_image:
            try:
                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
                solver = await DockerRuntimeSolver.create(
                    image_ref=_boot_image,
                    chain_ids=chain_ids,
                    rpc_urls=rpc_urls,
                    bridge_registry=bridge_registry,
                )
                if _boot_forced:
                    logger.warning(
                        "FORCE_SOLVER_IMAGE override ACTIVE — live solver pinned to %s "
                        "(break-glass; clear FORCE_SOLVER_IMAGE to resume normal "
                        "champion/genesis resolution). chains=%s",
                        _boot_image, list(rpc_urls.keys()),
                    )
                else:
                    logger.info(
                        "Genesis solver initialized via Docker (%s, chains=%s)",
                        _boot_image, list(rpc_urls.keys()),
                    )
            except Exception as exc:
                logger.warning("Live solver Docker boot unavailable (%s): %s", _boot_image, exc)
        else:
            logger.info(
                "No FORCE_SOLVER_IMAGE / GENESIS_SOLVER_IMAGE set — solver unavailable "
                "until champion is adopted",
            )

        # Simulator
        simulator = None
        sim_rpc_urls: dict[int, str] = {}
        if anvil_url:
            sim_rpc_urls[31337] = anvil_url
            sim_rpc_urls[1] = anvil_url
        base_sim_url = os.environ.get("BASE_SIM_RPC_URL") or base_url
        if base_sim_url:
            sim_rpc_urls[8453] = base_sim_url
        if btevm_url:
            sim_rpc_urls[964] = btevm_url

        # Upstream RPC URLs — the same endpoints the anvil containers
        # are forking from. Used by AnvilSimulator._reset_fork to advance
        # each fork to the current upstream head before every simulation
        # (otherwise anvil_reset is a no-op and sims run against stale
        # fork-time state — pool prices that no longer match real chain).
        # Optional: when unset for a given chain, that chain's fork stays
        # static (acceptable for local-testnet chain 31337 which isn't
        # forked from anything).
        upstream_rpc_urls: dict[int, str] = {}
        eth_upstream = (os.environ.get("ETH_UPSTREAM_RPC_URL") or "").strip()
        if eth_upstream:
            upstream_rpc_urls[1] = eth_upstream
        base_upstream = (os.environ.get("BASE_UPSTREAM_RPC_URL") or "").strip()
        if base_upstream:
            upstream_rpc_urls[8453] = base_upstream
        btevm_upstream = (os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL") or "").strip()
        if btevm_upstream:
            upstream_rpc_urls[964] = btevm_upstream

        if sim_rpc_urls:
            try:
                from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
                simulator = MultiChainSimulator(
                    sim_rpc_urls,
                    upstream_rpc_urls=upstream_rpc_urls,
                )
                logger.info(
                    "MultiChainSimulator initialized (chains=%s, upstreams=%s)",
                    list(sim_rpc_urls.keys()),
                    [c for c in sim_rpc_urls if c in upstream_rpc_urls],
                )
            except Exception as exc:
                logger.warning("MultiChainSimulator unavailable: %s", exc)

        if simulator is not None:
            apps.set_simulator(simulator)
            # Also wire the simulator into the local-testnet replay-debug
            # handler when its router was mounted. Importing the module is
            # cheap (no side effects); the route is what's conditional.
            if os.environ.get("LOCAL_TESTNET", "").strip() == "1":
                from minotaur_subnet.api.routes import local_testnet
                local_testnet.set_simulator(simulator)

        if simulator is not None and ctx.benchmark_worker is not None:
            ctx.benchmark_worker._simulator = simulator
            logger.info("BenchmarkWorker using real Anvil simulation")
        elif (
            ctx.benchmark_worker is not None
            and getattr(ctx.benchmark_worker, "_require_real_sim", False)
        ):
            # Fail-closed misconfiguration: the operator demanded real sims but
            # none materialized. Surface it loudly at boot; the worker will also
            # refuse (RealSimulationUnavailable) each tick rather than scoring
            # solvers on the fabricated mock.
            logger.error(
                "BENCHMARK_REQUIRE_REAL_SIM is set but no Anvil simulator is "
                "available — the benchmark worker will refuse to score "
                "(fail-closed) until a simulator is configured."
            )

        # Bridge tracker
        bridge_tracker = None
        bridge_tracker_task = None
        if bridge_registry is not None:
            from minotaur_subnet.relayer.bridge_tracker import BridgeTracker
            bridge_tracker = BridgeTracker(
                bridge_registry=bridge_registry,
                orderbook=ctx.orderbook,
                relayer=relayer_instance,
                simulator=simulator,
            )
            bridge_tracker_task = asyncio.create_task(bridge_tracker.run_loop())
            logger.info("BridgeTracker started")
        locals_bag["bridge_tracker"] = bridge_tracker
        locals_bag["bridge_tracker_task"] = bridge_tracker_task

        # Order execution consensus
        consensus = None
        order_peer_network = None
        consensus_mode = os.environ.get("CONSENSUS_MODE", "local").strip().lower()
        validator_keys_env = os.environ.get("VALIDATOR_PRIVATE_KEYS", "")
        validator_addrs_env = os.environ.get("VALIDATOR_ADDRESSES", "")
        # Bootstrap when either env is set. ``VALIDATOR_ADDRESSES`` is the
        # preferred public-only shape for real consensus mode; the older
        # ``VALIDATOR_PRIVATE_KEYS`` is kept for local-testnet (where the
        # api process genuinely signs as every validator) and for backward
        # compatibility in real mode (with a deprecation warning).
        if validator_keys_env or validator_addrs_env:
            try:
                from eth_account import Account

                raw_keys = [k.strip() for k in validator_keys_env.split(",") if k.strip()]
                validator_pairs: list[tuple[str, str]] = []
                for key in raw_keys:
                    addr = Account.from_key(key).address
                    validator_pairs.append((addr, key))

                pinned_addrs_only: list[str] = [
                    a.strip() for a in validator_addrs_env.split(",") if a.strip()
                ]

                if not validator_pairs and not pinned_addrs_only and consensus_mode == "real":
                    logger.warning(
                        "consensus envs set but no validator addresses parsed "
                        "from VALIDATOR_ADDRESSES or VALIDATOR_PRIVATE_KEYS"
                    )
                elif not validator_pairs and consensus_mode == "local":
                    logger.warning(
                        "VALIDATOR_PRIVATE_KEYS empty in CONSENSUS_MODE=local — "
                        "LocalTestnetConsensus signs in-process and needs the keys"
                    )
                elif consensus_mode == "real":
                    from minotaur_subnet.consensus import ConsensusManager
                    from minotaur_subnet.consensus.peer_network import (
                        ValidatorPeerNetwork,
                        parse_peers_env,
                    )

                    leader_key_env = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
                    if leader_key_env:
                        leader_key = leader_key_env
                        leader_addr = Account.from_key(leader_key).address
                    elif validator_pairs:
                        leader_addr, leader_key = validator_pairs[0]
                    else:
                        raise RuntimeError(
                            "CONSENSUS_MODE=real but no leader signing key — set "
                            "VALIDATOR_PRIVATE_KEY (singular) to the leader's key"
                        )

                    # Build the env-pinned trusted set, preferring the public
                    # ``VALIDATOR_ADDRESSES`` shape over ``VALIDATOR_PRIVATE_KEYS``.
                    # In real mode the api only signs as the leader; peer
                    # validators sign in their own processes. Holding their
                    # private keys here just to derive their addresses widens
                    # the blast radius for no operational benefit.
                    if pinned_addrs_only:
                        all_validator_addrs = list(pinned_addrs_only)
                        if validator_keys_env and consensus_mode == "real":
                            logger.warning(
                                "VALIDATOR_ADDRESSES is set; ignoring "
                                "VALIDATOR_PRIVATE_KEYS (deprecated for "
                                "CONSENSUS_MODE=real — peer keys never sign "
                                "here, only their addresses were used)"
                            )
                    else:
                        all_validator_addrs = [addr for addr, _ in validator_pairs]
                        if validator_keys_env:
                            logger.warning(
                                "VALIDATOR_PRIVATE_KEYS is deprecated in "
                                "CONSENSUS_MODE=real — peer private keys are "
                                "never used for signing here, only their "
                                "addresses. Migrate to VALIDATOR_ADDRESSES "
                                "(comma-separated 0x... EVM addresses)"
                            )
                    if leader_addr not in all_validator_addrs:
                        all_validator_addrs.insert(0, leader_addr)

                    # ORDER_CONSENSUS_PEERS is a named manual override used by
                    # tests + local-testnet to pin a specific peer set. In
                    # production it stays unset and ProtocolConfig.refresh_loop
                    # discovers peers from the metagraph + on-chain
                    # ValidatorRegistry. (The older VALIDATOR_PEERS fallback
                    # was removed during the registry-consolidation refactor.)
                    order_peers_env = os.environ.get("ORDER_CONSENSUS_PEERS", "")
                    order_peer_endpoints = parse_peers_env(order_peers_env)

                    for ep in order_peer_endpoints:
                        if ep.validator_id not in all_validator_addrs:
                            all_validator_addrs.append(ep.validator_id)

                    consensus_timeout = float(
                        os.environ.get("CHAMPION_CONSENSUS_TIMEOUT_SECONDS", "30")
                    )
                    chain_id = int(os.environ.get("CHAIN_ID", "31337"))

                    # Load canonical quorum from the on-chain ValidatorRegistry.
                    # The same registry is read by AppIntentBase at verification
                    # time, so daemon and contract always agree.
                    from minotaur_subnet.consensus.protocol_config import ProtocolConfig
                    order_registry_address = (
                        os.environ.get("VALIDATOR_REGISTRY_ADDRESS", "")
                        or os.environ.get(f"VALIDATOR_REGISTRY_{chain_id}", "")
                    )
                    if not order_registry_address:
                        raise RuntimeError(
                            f"Real consensus enabled but no ValidatorRegistry "
                            f"address for chain {chain_id}; set "
                            f"VALIDATOR_REGISTRY_ADDRESS"
                        )
                    # Read consensus state from the live upstream chain —
                    # never from the local Anvil fork. Forks snapshot at
                    # their fork point and don't see post-fork
                    # updateValidators / setQuorumBps writes until they're
                    # recycled (~6 h on prod's cron). consensus_chain_rpc_url
                    # picks the correct *_UPSTREAM_RPC_URL by chain id.
                    from minotaur_subnet.consensus.protocol_config import (
                        consensus_chain_rpc_url,
                    )
                    order_rpc_url = consensus_chain_rpc_url(chain_id)
                    # ``my_evm_address`` is the discovery gate inside
                    # ``ProtocolConfig.refresh_loop`` — without it, the
                    # ``if metagraph_provider and my_evm_address`` check is
                    # falsy and peer discovery is silently skipped. So
                    # ``protocol_config.peers`` stays empty forever, the leader
                    # broadcasts to env-pinned peers only, and quorum never
                    # rises above the in-cluster count even after third-party
                    # validators register on-chain. See ``validator/main.py``
                    # for the same wiring on the third-party stack.
                    order_protocol_config = ProtocolConfig.from_validator_registry(
                        rpc_url=order_rpc_url,
                        registry_address=order_registry_address,
                        # Order consensus's quorum source IS its own
                        # ValidatorRegistry — pass it explicitly (no silent
                        # fallback inside from_validator_registry).
                        quorum_address=order_registry_address,
                        my_evm_address=leader_addr,
                    )
                    # Stash on ctx so the refresh_loop task can be wired
                    # later, after solver_round_metagraph_sync is up.
                    ctx.order_protocol_config = order_protocol_config

                    consensus = ConsensusManager(
                        validator_id=leader_addr,
                        private_key=leader_key,
                        protocol_config=order_protocol_config,
                        validators=all_validator_addrs,
                        timeout=consensus_timeout,
                        chain_id=chain_id,
                        score_threshold_bps=int(score_threshold * 10000),
                    )
                    # Pin the peer list only when explicit env was provided.
                    # Otherwise pass protocol_config so the peer network reads
                    # through to the discovery loop's verified set.
                    use_pinned = bool(order_peer_endpoints)
                    order_peer_network = ValidatorPeerNetwork(
                        validator_id=leader_addr,
                        private_key=leader_key,
                        consensus=consensus,
                        peers=order_peer_endpoints if use_pinned else None,
                        protocol_config=order_protocol_config,
                        timeout=consensus_timeout,
                    )
                    logger.info(
                        "Real order consensus: leader=%s, peer-mode=%s, "
                        "quorum=%d bps (from ValidatorRegistry %s), chain=%d",
                        leader_addr[:10],
                        "pinned" if use_pinned else "discovered",
                        order_protocol_config.quorum_bps,
                        order_registry_address[:20],
                        chain_id,
                    )
                else:
                    from minotaur_subnet.consensus.local_consensus import LocalTestnetConsensus

                    consensus = LocalTestnetConsensus(
                        validator_keys=validator_pairs,
                        score_threshold_bps=int(score_threshold * 10000),
                    )
                    logger.info(
                        "LocalTestnetConsensus: %d validators (dynamic domain)",
                        len(validator_pairs),
                    )
            except Exception as exc:
                logger.warning("Order consensus init failed: %s", exc, exc_info=True)
        locals_bag["order_peer_network"] = order_peer_network

        from minotaur_subnet.api.services import get_wallet_manager as _get_wallet_mgr

        # Substrate relayer
        substrate_relayer_instance = None
        try:
            from minotaur_subnet.relayer.substrate_relayer import SubstrateRelayer
            from minotaur_subnet.blockchain.bittensor_proxy_executor import BittensorProxyExecutor
            subtensor_url = os.environ.get("SUBTENSOR_URL", "").strip()
            if subtensor_url:
                proxy_executor = BittensorProxyExecutor(network=subtensor_url)
                substrate_relayer_instance = SubstrateRelayer(proxy_executor)
                logger.info("SubstrateRelayer initialized (subtensor=%s)", subtensor_url)
        except Exception as exc:
            logger.info("SubstrateRelayer not available: %s", exc)

        ctx.block_loop = BlockLoop(
            orderbook=ctx.orderbook,
            app_store=ctx.store,
            js_engine=js_engine,
            solver=solver,
            simulator=simulator,
            relayer=relayer_instance,
            tick_interval=tick_interval,
            score_threshold=score_threshold,
            bridge_registry=bridge_registry,
            bridge_tracker=bridge_tracker,
            consensus=consensus,
            wallet_manager=_get_wallet_mgr(),
            substrate_relayer=substrate_relayer_instance,
        )
        if bridge_tracker is not None and consensus is not None:
            bridge_tracker.consensus = consensus

        if order_peer_network is not None:
            ctx.block_loop.set_peer_network(order_peer_network)
        orders.set_block_loop(ctx.block_loop)

        # ── solver round coordinator ─────────────────────────────────────
        # Champion consensus init is DECOUPLED from the benchmark worker.
        # Peers need champion consensus (to sign certification proposals)
        # even if they don't run their own benchmark worker. Previously
        # this was gated by `ctx.benchmark_worker is not None`, which
        # caused peers with ENABLE_BENCHMARK_WORKER=0 to respond with
        # 503 "Champion consensus not configured" to all proposals.
        _init_champion = ctx.benchmark_worker is not None or bool(
            os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
        )
        if _init_champion:
            validator_key = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
            from minotaur_subnet.epoch.manager import EpochManager
            from minotaur_subnet.harness.benchmark_worker import GENESIS_HOTKEY
            from minotaur_subnet.harness.round_store import RoundStatus

            if validator_key:
                try:
                    from minotaur_subnet.consensus import (
                        ChampionConsensusManager,
                        ValidatorPeerNetwork,
                        parse_peers_env,
                    )
                    from minotaur_subnet.consensus.eip712 import address_from_key
                    from minotaur_subnet.consensus.protocol_config import ProtocolConfig

                    # CHAMPION_CONSENSUS_CHAIN_ID (BT EVM = 964 in production).
                    # The domain separator must use the chain where
                    # ChampionRegistry is deployed, not the main operational
                    # chain (Base = 8453). This is REQUIRED when champion
                    # consensus is enabled — no silent fallback to CHAIN_ID,
                    # which would point the EIP-712 domain at the wrong chain.
                    # The local testnet sets CHAMPION_CONSENSUS_CHAIN_ID=964
                    # explicitly (see platform/local_testnet/init.py).
                    champion_chain_id_raw = os.environ.get(
                        "CHAMPION_CONSENSUS_CHAIN_ID", "",
                    ).strip()
                    if not champion_chain_id_raw:
                        raise RuntimeError(
                            "Champion consensus enabled but "
                            "CHAMPION_CONSENSUS_CHAIN_ID is not set",
                        )
                    champion_chain_id = int(champion_chain_id_raw)
                    try:
                        champion_consensus_timeout = float(
                            os.environ.get(
                                "CHAMPION_CONSENSUS_TIMEOUT_SECONDS",
                                "30",
                            ).strip()
                            or "30",
                        )
                    except ValueError:
                        champion_consensus_timeout = 30.0
                    internal_round_api_key = os.environ.get(
                        "SOLVER_ROUND_INTERNAL_API_KEY",
                        "",
                    ).strip() or os.environ.get("SUBMISSIONS_API_KEY", "").strip()
                    validator_id = address_from_key(validator_key)

                    # Build the champion-consensus ProtocolConfig.
                    #
                    # Validator set is read from the BT EVM ValidatorRegistry —
                    # the same one ChampionRegistry delegates to on-chain via
                    # constructor wiring (see ChampionRegistry.sol). On the
                    # local testnet (chain_id 31337), the same VALIDATOR_REGISTRY
                    # env is reused.
                    #
                    # Quorum threshold is read from ChampionRegistry itself —
                    # it keeps an independent quorumBps from
                    # ValidatorRegistry's. ChampionRegistry is REQUIRED when
                    # champion consensus is enabled (the local testnet now
                    # deploys one on chain 964 via DeployTestStack); there is
                    # no silent fallback to ValidatorRegistry's quorumBps,
                    # which would read the quorum threshold from the wrong
                    # contract.
                    champion_validator_registry = (
                        os.environ.get(f"VALIDATOR_REGISTRY_{champion_chain_id}", "").strip()
                        or os.environ.get("VALIDATOR_REGISTRY_ADDRESS", "").strip()
                    )
                    champion_registry_address = (
                        os.environ.get(f"CHAMPION_REGISTRY_{champion_chain_id}", "").strip()
                        or os.environ.get("CHAMPION_CONSENSUS_CONTRACT_ADDRESS", "").strip()
                    )
                    if not champion_validator_registry:
                        raise RuntimeError(
                            f"Champion consensus enabled but no "
                            f"VALIDATOR_REGISTRY_{champion_chain_id} (or "
                            f"VALIDATOR_REGISTRY_ADDRESS) configured",
                        )
                    if not champion_registry_address:
                        raise RuntimeError(
                            f"Champion consensus enabled but no "
                            f"CHAMPION_REGISTRY_{champion_chain_id} (or "
                            f"CHAMPION_CONSENSUS_CONTRACT_ADDRESS) configured",
                        )
                    # contract_address for the EIP-712 domain is the deployed
                    # ChampionRegistry — no zero-address fallback. The
                    # ChampionRegistry domain separator binds to this exact
                    # address (ChampionRegistry.sol constructor), so a wrong /
                    # zero address would produce sigs the on-chain certify()
                    # rejects.
                    champion_contract_address = champion_registry_address
                    champion_rpc_url = (
                        os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL", "").strip()
                        or os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
                        or "https://lite.chain.opentensor.ai"
                    )
                    champion_protocol_config = ProtocolConfig.from_validator_registry(
                        rpc_url=champion_rpc_url,
                        registry_address=champion_validator_registry,
                        # Quorum threshold comes from the ChampionRegistry's
                        # own quorumBps() — passed explicitly (required above).
                        quorum_address=champion_registry_address,
                        my_evm_address=validator_id,
                        # metagraph_provider wired below, after
                        # solver_round_metagraph_sync is initialized.
                    )
                    ctx.champion_protocol_config = champion_protocol_config

                    # Named manual override for environments where the
                    # ProtocolConfig discovery loop can't populate peers
                    # automatically — e.g., production where the metagraph
                    # axon URLs are unpublished, or local-testnet pinned
                    # mode. When set, this bypasses discovery and pins the
                    # champion-consensus peer set + validator list.
                    # Parallel to ORDER_CONSENSUS_PEERS for order consensus.
                    champion_peers_env = os.environ.get(
                        "CHAMPION_CONSENSUS_PEERS", "",
                    ).strip()
                    champion_peer_endpoints = parse_peers_env(champion_peers_env)
                    if champion_peer_endpoints:
                        pinned_champion_validators = [validator_id] + [
                            ep.validator_id for ep in champion_peer_endpoints
                        ]
                        logger.info(
                            "Champion consensus pinned via CHAMPION_CONSENSUS_PEERS: "
                            "%d peers (validator-set: %d)",
                            len(champion_peer_endpoints),
                            len(pinned_champion_validators),
                        )
                    else:
                        pinned_champion_validators = None  # discovery mode

                    champion_consensus = ChampionConsensusManager(
                        validator_id=validator_id,
                        private_key=validator_key,
                        protocol_config=champion_protocol_config,
                        # Pinned validators (when override env is set) take
                        # precedence over protocol_config.peers — see
                        # ChampionConsensusManager._validators_override.
                        validators=pinned_champion_validators,
                        timeout=champion_consensus_timeout,
                        chain_id=champion_chain_id,
                        contract_address=champion_contract_address,
                    )
                    champion_peer_network = ValidatorPeerNetwork(
                        validator_id=validator_id,
                        private_key=validator_key,
                        consensus=champion_consensus,
                        # Pinned peers (when override env is set) bypass the
                        # protocol_config discovery loop's peer cache.
                        peers=champion_peer_endpoints if champion_peer_endpoints else None,
                        protocol_config=champion_protocol_config,
                        timeout=champion_consensus_timeout,
                        # Discovered axons point at the :9100 daemon, which
                        # doesn't serve the champion-consensus routes. Retarget
                        # them to each validator's co-located api port (:8080).
                        # Pinned (_peers_override) peers are left verbatim.
                        peer_url_transform=_champion_axon_to_api_url,
                        default_headers=(
                            {
                                "x-solver-round-internal-key": internal_round_api_key,
                            }
                            if internal_round_api_key
                            else None
                        ),
                    )
                    await champion_peer_network.start()
                    submissions.set_champion_consensus_manager(champion_consensus)
                    submissions.set_champion_peer_network(champion_peer_network)
                    locals_bag["champion_peer_network"] = champion_peer_network

                    # Wire /identity endpoint (api side, port 8080). It needs
                    # the signing key now; metagraph_sync gets wired below
                    # after solver_round_metagraph_sync is initialized.
                    from minotaur_subnet.api.routes import identity as identity_route
                    identity_route.set_signing_key(validator_key)

                    logger.info(
                        "Champion consensus enabled (validator=%s, quorum=%d bps "
                        "from %s, validator-set from VR %s on chain %d)",
                        validator_id[:10],
                        champion_protocol_config.quorum_bps,
                        "ChampionRegistry " + champion_registry_address[:20],
                        champion_validator_registry[:20],
                        champion_chain_id,
                    )
                except Exception:
                    logger.warning(
                        "Champion consensus init failed; automatic round certification disabled",
                        exc_info=True,
                    )

            # Fleet-uniform DEFAULT ON: after a certified adoption, a follower with
            # hot-swap OFF keeps running the STALE champion solver to serve orders
            # while the leader runs the NEW one → post-adoption order-execution
            # divergence (weights route to the new champion but execution doesn't).
            # Prod + both compose templates already set "1", so default-True matches
            # deployed intent and removes a 3rd-party footgun. FORCE_SOLVER_IMAGE
            # still takes precedence in _build_live_solver (operator pin preserved).
            # Break-glass: set ALLOW_CHAMPION_HOT_SWAP=0 to disable. Inert under the
            # adoption freeze (no certification fires → no swap).
            allow_champion_hot_swap = _env_true("ALLOW_CHAMPION_HOT_SWAP", default=True)
            coordinator_enabled = _env_true("ENABLE_SOLVER_ROUND_COORDINATOR", default=True)
            try:
                champion_swap_timeout = float(
                    os.environ.get("CHAMPION_SWAP_TIMEOUT_SECONDS", "90").strip() or "90",
                )
            except ValueError:
                champion_swap_timeout = 90.0
            try:
                solver_round_poll_interval = float(
                    os.environ.get("SOLVER_ROUND_COORDINATOR_INTERVAL_SECONDS", "5").strip() or "5",
                )
            except ValueError:
                solver_round_poll_interval = 5.0
            try:
                solver_round_open_seconds = float(
                    os.environ.get("SOLVER_ROUND_OPEN_SECONDS", "300").strip() or "300",
                )
            except ValueError:
                solver_round_open_seconds = 300.0
            try:
                solver_round_decision_epochs = int(
                    os.environ.get("SOLVER_ROUND_DECISION_EPOCHS", "1").strip() or "1",
                )
            except ValueError:
                solver_round_decision_epochs = 1
            try:
                solver_round_activation_delay_epochs = int(
                    os.environ.get("SOLVER_ROUND_ACTIVATION_DELAY_EPOCHS", "1").strip() or "1",
                )
            except ValueError:
                solver_round_activation_delay_epochs = 1
            logger.info(
                "Solver round epoch clock configured: %s",
                _solver_round_epoch_health(ctx),
            )

            def _round_open_elapsed(current_round) -> float:
                return max(0.0, time.time() - float(current_round.created_at or time.time()))

            async def _build_live_solver(submission, epoch):
                """Build the live solver object for an activated champion."""
                # Operator break-glass: while FORCE_SOLVER_IMAGE is set, the live
                # solver is pinned (boot built it) — refuse to hot-swap to a
                # champion, so a broken champion can't reactivate over the forced
                # image. Returning None keeps the current (forced) solver; the
                # champion-of-record / weights still track adoption as normal.
                from minotaur_subnet.harness.runtime_solver import forced_solver_image
                _forced = forced_solver_image()
                if _forced:
                    logger.warning(
                        "FORCE_SOLVER_IMAGE active (%s) — NOT hot-swapping to champion "
                        "%s; keeping the forced live solver",
                        _forced, getattr(submission, "submission_id", "?"),
                    )
                    return None

                if not allow_champion_hot_swap:
                    logger.warning(
                        "Champion hot-swap disabled by policy; keeping current solver",
                    )
                    return None

                if submission.hotkey == GENESIS_HOTKEY:
                    if solver is not None:
                        logger.info("Genesis champion activated -- using baseline solver")
                        return solver
                    logger.warning("Genesis champion activated but baseline solver unavailable")
                    return None

                # Content-addressed: prefer the portable <repo>@sha256:D digest ref
                # (pullable on any host — start_docker pre-pulls it) over the local
                # {{.Id}} image_id (only present where the image was built). This is
                # what lets a follower / fresh node / restart run the certified bytes.
                from minotaur_subnet.harness.image_transport import is_digest_ref
                _digest = (getattr(submission, "image_digest", None) or "").strip()
                if is_digest_ref(_digest):
                    image_ref = _digest
                else:
                    image_ref = (submission.image_id or "").strip()
                    if not image_ref.startswith("sha256:"):
                        logger.warning(
                            "Champion %s missing immutable image_id/digest; refusing hot-swap",
                            submission.submission_id,
                        )
                        return None

                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver

                logger.info(
                    "Starting champion runtime from image_ref %s",
                    image_ref[:24],
                )
                new_solver = await asyncio.wait_for(
                    DockerRuntimeSolver.create(
                        image_ref=image_ref,
                        chain_ids=chain_ids,
                        rpc_urls=rpc_urls,
                        bridge_registry=bridge_registry,
                    ),
                    timeout=champion_swap_timeout,
                )
                meta = new_solver.metadata()
                logger.info(
                    "Champion runtime prepared: %s v%s (%s, epoch=%d)",
                    meta.name,
                    meta.version,
                    submission.submission_id,
                    epoch,
                )
                return new_solver

            _champion_merge_fn = None
            _champion_reject_fn = None
            if os.environ.get("SOLVER_REPO_URL", "").strip() or os.environ.get("SOLVER_REPO_PATH", "").strip():
                from minotaur_subnet.relayer.solver_repo import (
                    assert_solver_repo_token_not_admin,
                    on_champion_adopted_pr,
                    on_champion_rejected_pr,
                )
                # The leader's solver-repo token must NOT be admin-scoped (an admin
                # token bypasses + can edit the protect-main ruleset, defeating the
                # cert gate). HARD-FAIL when adoption is LIVE; while frozen
                # (DISABLE_CHAMPION_ADOPTION=1) the merge path never fires, so only
                # WARN — this keeps the currently-frozen leader bootable before the
                # non-admin PAT is provisioned. See solver_repo.py.
                from minotaur_subnet.epoch.manager import _adoption_disabled
                if _adoption_disabled():
                    try:
                        assert_solver_repo_token_not_admin()
                    except RuntimeError as exc:
                        logger.warning(
                            "[adoption-frozen] solver-repo token not yet hardened (%s) — "
                            "MUST provision a non-admin PAT before flipping DISABLE_CHAMPION_ADOPTION=0",
                            exc,
                        )
                else:
                    assert_solver_repo_token_not_admin()  # adoption LIVE → hard-fail if admin
                _champion_merge_fn = on_champion_adopted_pr
                _champion_reject_fn = on_champion_rejected_pr
                logger.info(
                    "Champion adoption: on-chain attestation + leader-authority merge (registry=%s, repo=%s)",
                    os.environ.get("CHAMPION_REGISTRY_964", "not set"),
                    os.environ.get("SOLVER_REPO_URL", "not set"),
                )

            ctx.epoch_manager = EpochManager(
                block_loop=ctx.block_loop,
                benchmark_worker=ctx.benchmark_worker,
                submission_store=sub_store,
                app_store=ctx.store,  # Stage-3 gate order lookups
                round_store=round_store,
                runtime_builder=_build_live_solver,
                on_champion_adopted=_champion_merge_fn,
                on_champion_rejected=_champion_reject_fn,
                vote_recorder=lambda v: setattr(ctx, "last_independent_vote", v),
            )
            submissions.set_epoch_manager(ctx.epoch_manager)

            solver_round_hotkey = _resolve_solver_round_hotkey()
            solver_round_force_leader = _env_true("FORCE_LEADER", default=False)
            if (
                solver_round_hotkey
                and os.environ.get("SUBTENSOR_URL", "").strip()
                and not solver_round_force_leader
            ):
                try:
                    from minotaur_subnet.validator.metagraph_sync import MetagraphSync

                    ctx.solver_round_metagraph_sync = MetagraphSync(
                        subtensor_url=os.environ.get("SUBTENSOR_URL", "").strip(),
                        netuid=int(os.environ.get("NETUID", "112").strip() or "112"),
                        my_hotkey=solver_round_hotkey,
                        poll_interval=max(solver_round_poll_interval, 10.0),
                    )
                    initial_state = await ctx.solver_round_metagraph_sync.sync_once()
                    ctx.solver_round_role = initial_state.my_role

                    # Wire the metagraph sync as the EpochManager's chain source so
                    # the burn-target subnet owner is resolved CHAIN-PRIMARY (public
                    # on-chain data) instead of env-only. Without this the leader's
                    # ramp silently no-ops on prod (no SUBNET_OWNER_HOTKEY set).
                    if (
                        ctx.epoch_manager is not None
                        and ctx.solver_round_metagraph_sync is not None
                    ):
                        ctx.epoch_manager.set_owner_chain_source(
                            ctx.solver_round_metagraph_sync
                        )

                    # Restore logging — instantiating MetagraphSync above
                    # imports bittensor, which clears all root logging
                    # handlers, sets the root logger to WARNING, and sets
                    # every existing logger to CRITICAL. Without this the
                    # api goes silent after this point: no order/champion
                    # ProtocolConfig refresh-loop logs, no peer-discovery
                    # logs, no submission logs, nothing. Mirrors the same
                    # workaround in ``validator/main.py``.
                    _log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
                    logging.basicConfig(
                        level=getattr(logging, _log_level, logging.INFO),
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        force=True,
                    )
                    for name in list(logging.Logger.manager.loggerDict):
                        if name.startswith("minotaur_subnet"):
                            logging.getLogger(name).setLevel(logging.NOTSET)

                    # Wire /identity now that metagraph_sync exists. The
                    # signing key was already set when ChampionConsensusManager
                    # was constructed above.
                    from minotaur_subnet.api.routes import identity as identity_route
                    identity_route.set_metagraph_sync(ctx.solver_round_metagraph_sync)

                    # Same metagraph_sync also powers the signed-miner gate
                    # on /orders/{id}/dry-run (PR for miner-signed access).
                    # Both setters point at the same instance — the gate
                    # reads .state.peers to verify hotkey membership on SN112.
                    from minotaur_subnet.api.routes import apps as apps_route
                    apps_route.set_metagraph_sync(ctx.solver_round_metagraph_sync)

                    # Wire metagraph_provider into champion ProtocolConfig
                    # and start its refresh loop. The refresh loop walks the
                    # metagraph axon list, probes each /identity, verifies
                    # signatures, and cross-checks against the BT EVM
                    # ValidatorRegistry. ChampionConsensusManager reads
                    # protocol_config.peers through its validators property,
                    # so new peers propagate automatically without restart.
                    if ctx.champion_protocol_config is not None:
                        from minotaur_subnet.consensus.peer_discovery import MetagraphPeer

                        async def _champion_metagraph_peers():
                            sync = ctx.solver_round_metagraph_sync
                            if sync is None or sync.state is None:
                                return []
                            return [
                                MetagraphPeer(hotkey=v.hotkey, axon_url=v.axon_url)
                                for v in sync.state.validators
                                if v.axon_url and v.hotkey
                            ]

                        ctx.champion_protocol_config.metagraph_provider = _champion_metagraph_peers
                        ctx.champion_protocol_config_task = asyncio.create_task(
                            ctx.champion_protocol_config.refresh_loop(),
                        )
                        logger.info(
                            "Champion ProtocolConfig refresh loop started "
                            "(metagraph discovery + on-chain quorum, %ds interval)",
                            ctx.champion_protocol_config.refresh_interval_seconds,
                        )

                    # Same wiring for ORDER consensus's ProtocolConfig — the
                    # leader's ValidatorPeerNetwork reads its discovered set
                    # through this. Without the refresh loop running, the
                    # discovery side of peer_network's union-mode stays empty
                    # and only env-pinned peers get proposals. The same
                    # metagraph provider works for both: discover_peers
                    # cross-attests against the order/champion-specific
                    # ValidatorRegistry, which filters out non-members.
                    if ctx.order_protocol_config is not None and ctx.order_protocol_config_task is None:
                        ctx.order_protocol_config.metagraph_provider = _champion_metagraph_peers
                        ctx.order_protocol_config_task = asyncio.create_task(
                            ctx.order_protocol_config.refresh_loop(),
                        )
                        logger.info(
                            "Order ProtocolConfig refresh loop started "
                            "(metagraph discovery + on-chain quorum, %ds interval)",
                            ctx.order_protocol_config.refresh_interval_seconds,
                        )

                    logger.info(
                        "Solver round metagraph sync enabled (role=%s, validators=%d)",
                        initial_state.my_role,
                        len(initial_state.validators),
                    )
                    ctx.solver_round_metagraph_task = asyncio.create_task(
                        ctx.solver_round_metagraph_sync.sync_loop()
                    )
                except Exception:
                    logger.warning(
                        "Failed to initialize solver round metagraph sync; falling back to standalone role",
                        exc_info=True,
                    )
                    ctx.solver_round_metagraph_sync = None
                    ctx.solver_round_role = "standalone"
            elif solver_round_force_leader:
                ctx.solver_round_role = "leader"

            def _is_solver_round_leader() -> bool:
                if solver_round_force_leader:
                    return True
                if ctx.solver_round_metagraph_sync is None:
                    return True
                return ctx.solver_round_metagraph_sync.is_leader

            # Order-book sync (#228): each FOLLOWER pulls the leader's full order set
            # — including the FAILED orders (rejected/expired) that never reach the
            # chain and are broadcast nowhere — so it can build a representative
            # Stage-2 benchmark corpus for the diverse-subset adoption vote. No-ops
            # on the leader (the source); idempotent upsert is self-healing.
            def _resolve_leader_api_url() -> str | None:
                _sync = ctx.solver_round_metagraph_sync
                if _sync is None or _sync.state is None or _sync.state.leader is None:
                    return None
                _leader = _sync.state.leader
                # Reuse the champion peer network's resolved :8080 endpoints (the
                # CHAMPION_CONSENSUS_PEERS pin on the testnet + the axon->api
                # transform on prod), matching the leader by evm validator_id.
                _net = submissions.get_champion_peer_network()
                if _net is not None:
                    for _peer in _net.peers:
                        if (_peer.validator_id or "").lower() == (_leader.evm_address or "").lower():
                            return _peer.url
                return _champion_axon_to_api_url(_leader.axon_url) or None

            try:
                from minotaur_subnet.blockloop.order_sync import OrderSync
                _order_sync = OrderSync(
                    app_store=ctx.store,
                    leader_api_url=_resolve_leader_api_url,
                    is_follower=(
                        lambda: ctx.solver_round_metagraph_sync is not None
                        and not _is_solver_round_leader()
                    ),
                )
                ctx.order_sync_task = asyncio.create_task(_order_sync.run_loop())
                logger.info("Order-book sync loop started (followers pull the leader's orders)")
            except Exception:
                logger.warning("Order-book sync not started", exc_info=True)

            def _solver_round_validator_set() -> list[str]:
                manager = submissions.get_champion_consensus_manager()
                network = submissions.get_champion_peer_network()
                if manager is not None and network is not None and network.peers:
                    validators = [manager.validator_id] + [
                        peer.validator_id
                        for peer in network.peers
                        if peer.validator_id
                    ]
                    if validators:
                        return validators
                if manager is not None:
                    return list(manager.validators)
                if ctx.solver_round_metagraph_sync is not None and ctx.solver_round_metagraph_sync.state is not None:
                    validators = [
                        peer.evm_address
                        for peer in ctx.solver_round_metagraph_sync.state.validators
                        if peer.evm_address
                    ]
                    if validators:
                        return validators
                return []

            async def _broadcast_round_sync(
                path: str,
                payload: dict[str, object],
                *,
                label: str,
            ) -> None:
                network = submissions.get_champion_peer_network()
                if network is None or not network.peers:
                    return
                try:
                    responses = await network.broadcast_json(path, payload)
                    logger.info(
                        "Solver round %s sync broadcast: responses=%d path=%s",
                        label,
                        len(responses),
                        path,
                    )
                except Exception:
                    logger.warning(
                        "Solver round %s sync broadcast failed",
                        label,
                        exc_info=True,
                    )

            def _close_sync_payload(round_state) -> dict[str, object]:
                return {
                    "round_id": round_state.round_id,
                    "close_epoch": round_state.close_epoch,
                    "benchmark_pack_hash": round_state.benchmark_pack_hash,
                    "committee_block": round_state.committee_block,
                    "committee_hash": round_state.committee_hash,
                    "quorum_required": round_state.quorum_required,
                    "decision_deadline_epoch": round_state.decision_deadline_epoch,
                    "effective_epoch": round_state.effective_epoch,
                }

            def _certify_sync_payload(round_state) -> dict[str, object]:
                certificate = round_state.certificate
                approvals = [
                    approval.to_dict()
                    for approval in (certificate.approvals if certificate is not None else [])
                ]
                return {
                    "round_id": round_state.round_id,
                    "candidate_submission_id": round_state.finalist_submission_id,
                    "candidate_image_id": round_state.finalist_image_id,
                    "committee_hash": round_state.committee_hash,
                    "benchmark_pack_hash": round_state.benchmark_pack_hash,
                    "shadow_case_log_hash": round_state.shadow_case_log_hash,
                    "effective_epoch": round_state.effective_epoch or 0,
                    "quorum_required": round_state.quorum_required or 0,
                    "approvals": approvals,
                }

            def _activate_sync_payload(round_id: str, activation_epoch: int) -> dict[str, object]:
                return {
                    "round_id": round_id,
                    "activation_epoch": activation_epoch,
                }

            def _abort_sync_payload(round_state) -> dict[str, object]:
                return {
                    "round_id": round_state.round_id,
                    "reason": round_state.abort_reason or "round_aborted",
                }

            async def _solver_round_on_leader_change() -> None:
                while ctx.solver_round_metagraph_sync is not None:
                    await ctx.solver_round_metagraph_sync.leader_changed.wait()
                    ctx.solver_round_metagraph_sync.leader_changed.clear()
                    state = ctx.solver_round_metagraph_sync.state
                    if state is None:
                        continue
                    previous_role = ctx.solver_round_role
                    ctx.solver_round_role = state.my_role
                    logger.info(
                        "Solver round leader change observed: %s -> %s",
                        previous_role,
                        ctx.solver_round_role,
                    )
                    manager = submissions.get_champion_consensus_manager()
                    if manager is not None:
                        manager.set_validators(_solver_round_validator_set())

            def _next_solver_round_epoch() -> int:
                current = round_store.get_current_round()
                active = round_store.get_active_champion()
                next_epoch = _current_solver_round_epoch(ctx)
                if active.submission_id:
                    next_epoch = max(next_epoch, active.activated_epoch)
                if ctx.epoch_manager is not None and ctx.epoch_manager.current_epoch > 0:
                    next_epoch = max(next_epoch, ctx.epoch_manager.current_epoch)
                if current is not None:
                    if current.close_epoch is not None:
                        next_epoch = max(next_epoch, int(current.close_epoch))
                    if current.effective_epoch is not None:
                        next_epoch = max(next_epoch, int(current.effective_epoch))
                    if current.opened_epoch is not None:
                        next_epoch = max(next_epoch, int(current.opened_epoch))
                return next_epoch

            def _boundary_epoch_for_round(current) -> int:
                if current.close_epoch is not None:
                    return max(int(current.close_epoch), _current_solver_round_epoch(ctx))
                return max(int(current.opened_epoch), _next_solver_round_epoch())

            def _solver_round_epoch_reached(target_epoch: int | None) -> bool:
                if target_epoch is None:
                    return True
                return _current_solver_round_epoch(ctx) >= int(target_epoch)

            def _solver_round_epoch_expired(target_epoch: int | None) -> bool:
                if target_epoch is None:
                    return False
                return _current_solver_round_epoch(ctx) > int(target_epoch)

            async def _maybe_abort_expired_round(current) -> bool:
                if current.status not in (
                    RoundStatus.CLOSED,
                    RoundStatus.REPLAYING,
                    RoundStatus.CERTIFYING,
                ):
                    return False
                if not _is_solver_round_leader():
                    return False
                if not _solver_round_epoch_expired(current.decision_deadline_epoch):
                    return False

                aborted = submissions._abort_solver_round_state(
                    submissions.AbortRoundRequest(
                        round_id=current.round_id,
                        reason="certification_deadline_elapsed",
                    )
                )
                logger.info(
                    "Solver round aborted after deadline: round=%s deadline=%s epoch=%s",
                    aborted.round_id,
                    aborted.decision_deadline_epoch,
                    _current_solver_round_epoch(ctx),
                )
                await _broadcast_round_sync(
                    "/v1/solver/round/internal/abort",
                    _abort_sync_payload(aborted),
                    label="abort",
                )
                return True

            async def _maybe_close_open_round(current) -> bool:
                if current.status != RoundStatus.OPEN:
                    return False
                if not _is_solver_round_leader():
                    return False
                if _round_open_elapsed(current) < solver_round_open_seconds:
                    return False

                validators = _solver_round_validator_set()
                manager = submissions.get_champion_consensus_manager()
                if manager is not None and validators:
                    manager.set_validators(validators)

                close_epoch = max(
                    int(current.opened_epoch),
                    _current_solver_round_epoch(ctx),
                )
                # Round-anchored fork pins (gated, default-off). Populate BEFORE
                # the pack hash below so the canonical pins are folded into it.
                _maybe_populate_round_fork_pins(current.round_id, close_epoch)
                # Shadow phase (ROUND_ANCHOR_SHADOW): when the real gate is off,
                # still derive + log the pins the leader WOULD pin, so fleet pin
                # parity can be verified before enabling. No consensus effect.
                _maybe_shadow_log_round_fork_pins(
                    ctx, current.round_id, role="leader", anchor_epoch=close_epoch,
                )
                committee_hash = manager.committee_hash if manager is not None else None
                quorum_required = manager.quorum_required if manager is not None else None
                closed = submissions._close_solver_round_state(
                    submissions.CloseRoundRequest(
                        round_id=current.round_id,
                        close_epoch=close_epoch,
                        benchmark_pack_hash=_build_solver_round_benchmark_pack_hash(ctx, current.round_id),
                        committee_block=(
                            ctx.solver_round_metagraph_sync.state.block
                            if ctx.solver_round_metagraph_sync is not None
                            and ctx.solver_round_metagraph_sync.state is not None
                            else close_epoch
                        ),
                        committee_hash=committee_hash,
                        quorum_required=quorum_required,
                        decision_deadline_epoch=close_epoch + max(1, solver_round_decision_epochs),
                        effective_epoch=close_epoch + max(1, solver_round_activation_delay_epochs),
                    )
                )
                logger.info(
                    "Solver round closed by leader: round=%s close_epoch=%s quorum=%s",
                    closed.round_id,
                    closed.close_epoch,
                    closed.quorum_required,
                )
                await _broadcast_round_sync(
                    "/v1/solver/round/internal/close",
                    _close_sync_payload(closed),
                    label="close",
                )
                return True

            async def _maybe_certify_round(current) -> bool:
                if current.status != RoundStatus.CERTIFYING:
                    return False
                if not _is_solver_round_leader():
                    return False
                if submissions.get_champion_consensus_manager() is None:
                    logger.warning("Cannot certify: champion consensus manager is None")
                    return False
                if current.certificate is not None:
                    return False
                if not current.finalist_submission_id:
                    logger.warning("Cannot certify: no finalist")
                    return False
                logger.info(
                    "Attempting certification: round=%s finalist=%s",
                    current.round_id, current.finalist_submission_id,
                )
                certified = await submissions._certify_solver_round_state(
                    submissions.CertifyRoundRequest(
                        round_id=current.round_id,
                        candidate_submission_id=current.finalist_submission_id,
                        candidate_image_id=current.finalist_image_id,
                        committee_hash=current.committee_hash,
                        benchmark_pack_hash=current.benchmark_pack_hash,
                        shadow_case_log_hash=current.shadow_case_log_hash,
                        effective_epoch=current.effective_epoch or _boundary_epoch_for_round(current),
                        quorum_required=current.quorum_required or 0,
                        approvals=[],
                    )
                )
                logger.info(
                    "Solver round certified by leader: round=%s effective_epoch=%s approvals=%s",
                    certified.round_id,
                    certified.effective_epoch,
                    len(certified.certificate.approvals) if certified.certificate else 0,
                )
                await _broadcast_round_sync(
                    "/v1/solver/round/internal/certify",
                    _certify_sync_payload(certified),
                    label="certify",
                )
                return True

            async def _maybe_activate_certified_round(current) -> bool:
                if current.status != RoundStatus.CERTIFIED:
                    return False
                if not _is_solver_round_leader():
                    return False
                activation_epoch = current.effective_epoch or _boundary_epoch_for_round(current)
                current_epoch = _current_solver_round_epoch(ctx)
                if current_epoch < activation_epoch:
                    return False
                result = await ctx.epoch_manager.activate_certified_round(
                    current.round_id,
                    epoch=current_epoch,
                )
                logger.info(
                    "Solver round activated by leader: round=%s changed=%s next=%s",
                    result.get("round_id"),
                    result.get("champion_changed"),
                    result.get("next_round_id"),
                )
                await _broadcast_round_sync(
                    "/v1/solver/round/internal/activate",
                    _activate_sync_payload(current.round_id, activation_epoch),
                    label="activate",
                )
                return True

            async def _solver_round_loop() -> None:
                logger.info(
                    "Solver round coordinator started (poll_interval=%.1f)",
                    solver_round_poll_interval,
                )
                while True:
                    try:
                        current = round_store.get_current_round()
                        if current is None:
                            incumbent = round_store.get_active_champion()
                            round_store.ensure_open_round(
                                opened_epoch=_next_solver_round_epoch(),
                                incumbent=incumbent if incumbent.submission_id else None,
                            )
                            current = round_store.get_current_round()

                        if current is not None and await _maybe_close_open_round(current):
                            continue

                        if current is not None and await _maybe_abort_expired_round(current):
                            continue

                        if current is not None and current.status in (
                            RoundStatus.CLOSED,
                            RoundStatus.REPLAYING,
                        ):
                            if not _solver_round_epoch_reached(current.close_epoch):
                                await asyncio.sleep(solver_round_poll_interval)
                                continue
                            if current.status == RoundStatus.REPLAYING:
                                logger.info(
                                    "Shadow evaluation deferred (Phase 2) -- "
                                    "proceeding directly to certification for round %s",
                                    current.round_id,
                                )
                            summary = await ctx.epoch_manager.evaluate_round(
                                current.round_id,
                                epoch=_boundary_epoch_for_round(current),
                            )
                            logger.info(
                                "Solver round evaluated: round=%s status=%s finalist=%s next=%s benchmarked=%s abort=%s",
                                summary.get("round_id"),
                                summary.get("status_after"),
                                summary.get("finalist_submission_id"),
                                summary.get("next_round_id"),
                                summary.get("benchmarked"),
                                summary.get("abort_reason"),
                            )
                            if _is_solver_round_leader() and summary.get("status_after") == RoundStatus.ABORTED.value:
                                processed_round = round_store.get_round(current.round_id)
                                if processed_round is not None and processed_round.status == RoundStatus.ABORTED:
                                    await _broadcast_round_sync(
                                        "/v1/solver/round/internal/abort",
                                        _abort_sync_payload(processed_round),
                                        label="abort",
                                    )
                            continue

                        if current is not None and await _maybe_certify_round(current):
                            continue

                        if current is not None and await _maybe_activate_certified_round(current):
                            continue

                        if current is not None and current.status in (
                            RoundStatus.ACTIVATED,
                            RoundStatus.ABORTED,
                        ):
                            incumbent = round_store.get_active_champion()
                            round_store.open_next_round(
                                opened_epoch=_next_solver_round_epoch(),
                                incumbent=incumbent if incumbent.submission_id else None,
                            )
                            continue

                        await asyncio.sleep(solver_round_poll_interval)
                    except asyncio.CancelledError:
                        raise
                    except Exception as loop_exc:
                        logger.warning("Solver round coordinator loop failed: %s", loop_exc, exc_info=True)
                        await asyncio.sleep(min(solver_round_poll_interval, 5.0))

            if coordinator_enabled:
                ctx.solver_round_task = asyncio.create_task(_solver_round_loop())
                if ctx.solver_round_metagraph_sync is not None:
                    ctx.solver_round_role_task = asyncio.create_task(
                        _solver_round_on_leader_change()
                    )
            else:
                logger.info("Solver round coordinator disabled by policy")

        ctx.block_loop_task = asyncio.create_task(ctx.block_loop.run_loop())
        logger.info(
            "BlockLoop started (tick=%.1fs, threshold=%.2f)",
            tick_interval, score_threshold,
        )

    # Round-anchor parity probe — runs on every validator (leader + follower),
    # independent of the solver-round coordinator, so /health always carries
    # this node's derived pin for fleet parity diffing. Default-on; opt out with
    # ROUND_ANCHOR_PARITY=0.
    if _round_anchor_parity_enabled():
        ctx.round_anchor_task = asyncio.create_task(_round_anchor_parity_loop(ctx))
        logger.info("[round-anchor-parity] /health probe started")

    # CloudWatch metrics publisher (Phase 5.4). No-op unless
    # CLOUDWATCH_METRICS_ENABLED=1 and boto3 is installed.
    from minotaur_subnet.api.metrics import publish_loop as _metrics_publish_loop
    _metrics_task = asyncio.create_task(
        _metrics_publish_loop(
            peer_network=locals_bag.get("order_peer_network"),
            blockloop=ctx.block_loop,
        ),
    )
    locals_bag["metrics_task"] = _metrics_task

    return locals_bag


async def shutdown(ctx: ServerContext, locals_bag: dict) -> None:
    """Gracefully stop all background tasks and services."""
    from minotaur_subnet.api.routes import submissions

    bridge_tracker = locals_bag.get("bridge_tracker")
    bridge_tracker_task = locals_bag.get("bridge_tracker_task")
    order_peer_network = locals_bag.get("order_peer_network")
    champion_peer_network = locals_bag.get("champion_peer_network")

    if ctx.block_loop is not None:
        ctx.block_loop.stop()
        if bridge_tracker is not None:
            bridge_tracker.stop()
        if bridge_tracker_task is not None:
            bridge_tracker_task.cancel()
            try:
                await bridge_tracker_task
            except asyncio.CancelledError:
                pass
            logger.info("BridgeTracker stopped")
    if ctx.block_loop_task is not None:
        ctx.block_loop_task.cancel()
        try:
            await ctx.block_loop_task
        except asyncio.CancelledError:
            pass
        logger.info("BlockLoop stopped")

    if ctx.block_loop is not None and ctx.block_loop.solver is not None:
        shutdown_fn = getattr(ctx.block_loop.solver, "shutdown", None)
        if callable(shutdown_fn):
            try:
                maybe_awaitable = shutdown_fn()
                if asyncio.iscoroutine(maybe_awaitable):
                    await maybe_awaitable
            except Exception:
                logger.warning("Solver shutdown during API teardown failed", exc_info=True)

    if ctx.benchmark_worker is not None:
        ctx.benchmark_worker.stop()
    if ctx.benchmark_task is not None:
        ctx.benchmark_task.cancel()
        try:
            await ctx.benchmark_task
        except asyncio.CancelledError:
            pass
        logger.info("Benchmark worker stopped")
    if ctx.round_anchor_task is not None:
        ctx.round_anchor_task.cancel()
        try:
            await ctx.round_anchor_task
        except asyncio.CancelledError:
            pass
        logger.info("Round-anchor parity probe stopped")
    if ctx.solver_round_task is not None:
        ctx.solver_round_task.cancel()
        try:
            await ctx.solver_round_task
        except asyncio.CancelledError:
            pass
        logger.info("Solver round coordinator stopped")
    if ctx.solver_round_role_task is not None:
        ctx.solver_round_role_task.cancel()
        try:
            await ctx.solver_round_role_task
        except asyncio.CancelledError:
            pass
        logger.info("Solver round leader monitor stopped")
    if ctx.solver_round_metagraph_task is not None:
        ctx.solver_round_metagraph_task.cancel()
        try:
            await ctx.solver_round_metagraph_task
        except asyncio.CancelledError:
            pass
        logger.info("Solver round metagraph sync stopped")
    if order_peer_network is not None:
        await order_peer_network.stop()
        logger.info("Order consensus peer network stopped")
    if champion_peer_network is not None:
        await champion_peer_network.stop()
        logger.info("Champion peer network stopped")
    submissions.set_champion_consensus_manager(None)
    submissions.set_champion_peer_network(None)
    submissions.set_epoch_manager(None)
    submissions.set_solver_round_epoch_provider(None)
    ctx.solver_round_metagraph_sync = None
    ctx.solver_round_role = "standalone"
    ctx.solver_round_epoch_clock = None
