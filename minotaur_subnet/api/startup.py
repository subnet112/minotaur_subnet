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


def _build_solver_round_benchmark_pack_hash(
    ctx: ServerContext,
    round_id: str,
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

    scenario_hash = compute_pack_hash(round_id, synthetic_scenarios, historical_order_ids)

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
    # Step 0: hydrate signing keys from AWS Secrets Manager before any
    # other module reads os.environ. Env values that are already set are
    # preserved — SM is fallback, not override. No-op if boto3 is missing
    # or the instance lacks the IAM role.
    from minotaur_subnet.api.secrets_loader import hydrate_env_from_secrets_manager
    _outcome = hydrate_env_from_secrets_manager()
    if _outcome.env_vars_set:
        logger.info(
            "[secrets] hydrated %d signing key(s) from Secrets Manager",
            _outcome.env_vars_set,
        )

    from minotaur_subnet.api.routes import (
        apps,
        chains,
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
        ctx.benchmark_worker = BenchmarkWorker(
            submission_store=sub_store,
            app_store=ctx.store,
            round_store=round_store,
            genesis_solver_image=_genesis_solver_image,
        )
        ctx.benchmark_task = asyncio.create_task(
            ctx.benchmark_worker.run_loop(interval=poll_interval),
        )
        logger.info("Benchmark worker started (poll every %ds)", poll_interval)

    # ── relayer ──────────────────────────────────────────────────────────
    relayer_instance = None
    if os.environ.get("USE_EVM_RELAYER", "").lower() in ("1", "true", "yes"):
        from minotaur_subnet.relayer import EvmRelayer
        from minotaur_subnet.relayer.chain_config import get_supported_chains
        relayer_key = os.environ.get("RELAYER_PRIVATE_KEY", "")
        relayer_instance = EvmRelayer(
            chains=get_supported_chains(),
            private_key=relayer_key,
        )

        from minotaur_subnet.deployment.compiler import ForgeCompiler
        from minotaur_subnet.deployment.deployer import DeployService
        from minotaur_subnet.api.services import set_deploy_service

        registry_address = os.environ.get("VALIDATOR_REGISTRY_ADDRESS", "")
        if not registry_address:
            registry_address = os.environ.get("VALIDATOR_REGISTRY_31337", "")
        # Quorum is no longer a deploy-time arg — AppIntentBase reads it from
        # the ValidatorRegistry at execution time.
        deploy_service = DeployService(
            ForgeCompiler(),
            relayer_instance,
            registry_address,
        )
        set_deploy_service(deploy_service)
        logger.info(
            "DeployService configured (registry=%s)",
            registry_address[:20] if registry_address else "none",
        )

    # ── chain info ───────────────────────────────────────────────────────
    from minotaur_subnet.relayer.chain_config import get_supported_chains as _get_chains
    from minotaur_subnet.api.services import set_chain_info
    _chains = _get_chains()
    set_chain_info([
        {
            "chain_id": c.chain_id,
            "name": c.name,
            "rpc_available": bool(c.rpc_url),
            "registry_address": c.validator_registry_address,
        }
        for c in _chains.values()
    ])
    logger.info("Chain info: %s", [c.chain_id for c in _chains.values()])

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

        # Solver — boot from Docker genesis image
        solver = None
        _genesis_image = os.environ.get("GENESIS_SOLVER_IMAGE", "").strip()
        if _genesis_image:
            try:
                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
                solver = await DockerRuntimeSolver.create(
                    image_ref=_genesis_image,
                    chain_ids=chain_ids,
                    rpc_urls=rpc_urls,
                    bridge_registry=bridge_registry,
                )
                logger.info(
                    "Genesis solver initialized via Docker (%s, chains=%s)",
                    _genesis_image, list(rpc_urls.keys()),
                )

                # Background pre-warm for token discovery. A cold call can
                # take 60-120 s on Base (17 seed tokens × 4 fee tiers ≈ 600
                # factory.getPool RPC roundtrips). Doing it here populates
                # the DockerRuntimeSolver's TTL cache so the frontend's
                # first /v1/chains/{id}/tokens call returns instantly.
                async def _prewarm_tokens(_solver, _chain_ids):
                    for cid in _chain_ids:
                        try:
                            t0 = time.monotonic()
                            tokens = await _solver.supported_tokens(cid)
                            logger.info(
                                "Token discovery pre-warm chain %d: %d tokens in %.1fs",
                                cid, len(tokens), time.monotonic() - t0,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Token discovery pre-warm failed for chain %d: %s",
                                cid, exc,
                            )
                asyncio.create_task(_prewarm_tokens(solver, list(rpc_urls.keys())))
            except Exception as exc:
                logger.warning("Genesis Docker solver unavailable: %s", exc)
        else:
            logger.info("No GENESIS_SOLVER_IMAGE set — solver unavailable until champion is adopted")

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

        if simulator is not None and ctx.benchmark_worker is not None:
            ctx.benchmark_worker._simulator = simulator
            logger.info("BenchmarkWorker using real Anvil simulation")

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
        if validator_keys_env:
            try:
                from eth_account import Account

                raw_keys = [k.strip() for k in validator_keys_env.split(",") if k.strip()]
                validator_pairs: list[tuple[str, str]] = []
                for key in raw_keys:
                    addr = Account.from_key(key).address
                    validator_pairs.append((addr, key))

                if not validator_pairs:
                    logger.warning(
                        "VALIDATOR_PRIVATE_KEYS set but no validator keys parsed"
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
                    else:
                        leader_addr, leader_key = validator_pairs[0]
                    all_validator_addrs = [addr for addr, _ in validator_pairs]
                    if leader_addr not in all_validator_addrs:
                        all_validator_addrs.insert(0, leader_addr)

                    order_peers_env = os.environ.get("ORDER_CONSENSUS_PEERS", "")
                    order_peer_endpoints = parse_peers_env(order_peers_env)

                    if not order_peer_endpoints:
                        vp_env = os.environ.get("VALIDATOR_PEERS", "")
                        order_peer_endpoints = parse_peers_env(vp_env)

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
                    order_rpc_url = (
                        os.environ.get("ANVIL_RPC_URL")
                        or os.environ.get("BASE_RPC_URL")
                        or "http://anvil:8545"
                    )
                    order_protocol_config = ProtocolConfig.from_validator_registry(
                        rpc_url=order_rpc_url,
                        registry_address=order_registry_address,
                    )

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
        chains.set_block_loop(ctx.block_loop)

        # Token cache warm-up. DockerRuntimeSolver.supported_tokens is an
        # async coroutine; sync solvers expose it as a regular function.
        # Route each appropriately — previously we unconditionally wrapped
        # in asyncio.to_thread which produced an unawaited coroutine and
        # an ``object of type 'coroutine' has no len()'' warning.
        #
        # Note: this is redundant with the genesis-solver _prewarm_tokens
        # task earlier, which already warms the DockerRuntimeSolver cache.
        # Kept for sync-solver support and as a defensive re-warm.
        if ctx.block_loop.solver and hasattr(ctx.block_loop.solver, "supported_tokens"):
            _supported_tokens = ctx.block_loop.solver.supported_tokens
            _is_async = asyncio.iscoroutinefunction(_supported_tokens)

            async def _warm_token_cache() -> None:
                for cid in chain_ids:
                    try:
                        if _is_async:
                            tokens = await _supported_tokens(cid)
                        else:
                            tokens = await asyncio.to_thread(_supported_tokens, cid)
                        logger.info(
                            "Token cache warmed for chain %d: %d tokens",
                            cid, len(tokens),
                        )
                    except Exception as exc:
                        logger.warning("Token cache warm failed for chain %d: %s", cid, exc)
            asyncio.create_task(_warm_token_cache())

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
                    )
                    from minotaur_subnet.consensus.eip712 import address_from_key
                    from minotaur_subnet.consensus.protocol_config import ProtocolConfig

                    try:
                        # CHAMPION_CONSENSUS_CHAIN_ID (BT EVM = 964 in production).
                        # The domain separator must use the chain where
                        # ChampionRegistry is deployed, not the main operational
                        # chain (Base = 8453).
                        champion_chain_id = int(
                            os.environ.get(
                                "CHAMPION_CONSENSUS_CHAIN_ID",
                                "964",
                            ).strip() or "964",
                        )
                    except ValueError:
                        champion_chain_id = 964
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
                    # Validator set is read from the BT EVM ValidatorRegistry
                    # (the same one ChampionRegistry delegates to on-chain via
                    # constructor wiring — see ChampionRegistry.sol).
                    # Quorum threshold is read from ChampionRegistry itself,
                    # which keeps an independent quorumBps from
                    # ValidatorRegistry's.
                    champion_validator_registry = os.environ.get(
                        f"VALIDATOR_REGISTRY_{champion_chain_id}", "",
                    ).strip()
                    champion_registry_address = (
                        os.environ.get(f"CHAMPION_REGISTRY_{champion_chain_id}", "").strip()
                        or os.environ.get("CHAMPION_CONSENSUS_CONTRACT_ADDRESS", "").strip()
                    )
                    if not champion_validator_registry:
                        raise RuntimeError(
                            f"Champion consensus enabled but no "
                            f"VALIDATOR_REGISTRY_{champion_chain_id} configured",
                        )
                    if not champion_registry_address:
                        raise RuntimeError(
                            f"Champion consensus enabled but no "
                            f"CHAMPION_REGISTRY_{champion_chain_id} configured",
                        )
                    champion_rpc_url = (
                        os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL", "").strip()
                        or os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
                        or "https://lite.chain.opentensor.ai"
                    )
                    champion_protocol_config = ProtocolConfig.from_validator_registry(
                        rpc_url=champion_rpc_url,
                        registry_address=champion_validator_registry,
                        quorum_address=champion_registry_address,
                        my_evm_address=validator_id,
                        # metagraph_provider wired below, after
                        # solver_round_metagraph_sync is initialized.
                    )
                    ctx.champion_protocol_config = champion_protocol_config

                    champion_consensus = ChampionConsensusManager(
                        validator_id=validator_id,
                        private_key=validator_key,
                        protocol_config=champion_protocol_config,
                        timeout=champion_consensus_timeout,
                        chain_id=champion_chain_id,
                        contract_address=champion_registry_address,
                    )
                    champion_peer_network = ValidatorPeerNetwork(
                        validator_id=validator_id,
                        private_key=validator_key,
                        consensus=champion_consensus,
                        protocol_config=champion_protocol_config,
                        timeout=champion_consensus_timeout,
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
                        "from ChampionRegistry %s, validator-set from "
                        "VR %s on chain %d)",
                        validator_id[:10],
                        champion_protocol_config.quorum_bps,
                        champion_registry_address[:20],
                        champion_validator_registry[:20],
                        champion_chain_id,
                    )
                except Exception:
                    logger.warning(
                        "Champion consensus init failed; automatic round certification disabled",
                        exc_info=True,
                    )

            allow_champion_hot_swap = _env_true("ALLOW_CHAMPION_HOT_SWAP", default=False)
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

                image_ref = (submission.image_id or "").strip()
                if not image_ref.startswith("sha256:"):
                    logger.warning(
                        "Champion %s missing immutable image_id; refusing hot-swap",
                        submission.submission_id,
                    )
                    return None

                from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver

                logger.info(
                    "Starting champion runtime from image_id %s",
                    image_ref[:20],
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
            if os.environ.get("SOLVER_REPO_URL", "").strip() or os.environ.get("SOLVER_REPO_PATH", "").strip():
                from minotaur_subnet.relayer.solver_repo import on_champion_adopted_pr
                _champion_merge_fn = on_champion_adopted_pr
                logger.info(
                    "Champion adoption: on-chain attestation + GitHub PR (registry=%s, repo=%s)",
                    os.environ.get("CHAMPION_REGISTRY_964", "not set"),
                    os.environ.get("SOLVER_REPO_URL", "not set"),
                )

            ctx.epoch_manager = EpochManager(
                block_loop=ctx.block_loop,
                benchmark_worker=ctx.benchmark_worker,
                submission_store=sub_store,
                round_store=round_store,
                runtime_builder=_build_live_solver,
                on_champion_adopted=_champion_merge_fn,
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

                    # Wire /identity now that metagraph_sync exists. The
                    # signing key was already set when ChampionConsensusManager
                    # was constructed above.
                    from minotaur_subnet.api.routes import identity as identity_route
                    identity_route.set_metagraph_sync(ctx.solver_round_metagraph_sync)

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
