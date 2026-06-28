"""App Intent lifecycle service functions.

Functions: create_app_intent, deploy_app_intent, validate_app_intent_code,
list_minotaur_subnet, get_app_status, update_scoring, activate_app,
get_app_manifest, list_app_manifests.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.shared.interop_address import parse_address
from minotaur_subnet.store import AppIntentStore

from ._helpers import (
    _config_from_manifest,
    _generate_app_id,
    _sha256,
    _validate_manifest_semantics_for_response,
)
import logging

logger = logging.getLogger(__name__)


def create_app_intent(
    store: AppIntentStore,
    name: str,
    description: str,
    supported_chains: list[int],
    js_code: str | None = None,
    solidity_code: str | None = None,
    constructor_args: list[list[str]] | None = None,
    deployer: str = "",
    fee_mode: str = "",
) -> dict[str, Any]:
    """Define a new App Intent with developer-provided JS and Solidity code.

    Both ``js_code`` and ``solidity_code`` are required. Apps provide their
    own scoring JS and on-chain contract -- there is no auto-generation.

    Args:
        name:             Human-readable name (e.g. "ETH-USDC Swap").
        description:      What this app does, in plain English.
        supported_chains: Chain IDs to deploy on (e.g. [1, 8453]).
        js_code:          JS scoring code (required).
        solidity_code:    Solidity contract code (required).
        deployer:         Address of the deployer. Only this address can
                          update the app's JS scoring code later.

    Returns:
        Full AppIntentDefinition as a dict, including JS and Solidity
        source code (and their SHA-256 hashes).
    """
    # ── validation ───────────────────────────────────────────────────────
    if not name or not name.strip():
        return {"error": "name is required"}
    if not supported_chains:
        return {"error": "supported_chains must be a non-empty list"}
    for cid in supported_chains:
        if not isinstance(cid, int) or cid <= 0:
            return {"error": f"Invalid chain_id in supported_chains: {cid}"}

    # Per-App fee mode (#239): "USER"/"APP", or empty to fall back to the
    # operator's FEE_MODE_DEFAULT at deploy time. Normalize + validate here so a
    # bad value is rejected at create with a clear error, not at deploy.
    fee_mode = (fee_mode or "").strip().upper()
    if fee_mode and fee_mode not in ("USER", "APP"):
        return {"error": f"fee_mode must be 'USER', 'APP', or empty, got {fee_mode!r}"}

    # ── both JS and Solidity are required ────────────────────────────────
    has_js = bool(js_code and js_code.strip())
    has_sol = bool(solidity_code and solidity_code.strip())

    if not has_js or not has_sol:
        return {
            "error": (
                "Both js_code and solidity_code are required. "
                "Apps must provide their own scoring JS and Solidity contract."
            ),
        }

    js_code = js_code.strip()  # type: ignore[union-attr]
    solidity_code = solidity_code.strip()  # type: ignore[union-attr]

    validation_warnings: list[str] = []
    extracted_manifest: dict[str, Any] | None = None

    # ── pre-flight code validation ────────────────────────────────────
    try:
        import asyncio
        from minotaur_subnet.engine.validation import validate_app_intent as _validate

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    validation = pool.submit(
                        lambda: asyncio.run(_validate(js_code, solidity_code))
                    ).result()
            else:
                validation = loop.run_until_complete(
                    _validate(js_code, solidity_code)
                )
        except RuntimeError:
            validation = asyncio.run(_validate(js_code, solidity_code))

        if not validation.valid:
            return {
                "error": "Validation failed",
                "validation_errors": validation.errors,
                "validation_warnings": validation.warnings,
            }
        validation_warnings.extend(validation.warnings)
        if isinstance(validation.js_manifest, dict):
            extracted_manifest = validation.js_manifest
    except Exception as exc:
        logger.warning("Pre-flight validation skipped: %s", exc)

    # ── validate deployer address (if provided) ────────────────────────
    deployer_addr = ""
    if deployer and deployer.strip():
        try:
            ia = parse_address(deployer.strip())
            deployer_addr = ia.address  # EIP-55 checksummed
        except ValueError as exc:
            return {"error": f"Invalid deployer address: {exc}"}

    # ── build definition ─────────────────────────────────────────────────
    app_id = _generate_app_id()
    config = AppIntentConfig(
        supported_chains=supported_chains,
        score_threshold=0.5,
        on_chain_threshold=5000,
        max_gas=500_000,
        fee_mode=fee_mode,
    )

    manifest_errors, manifest_warnings, typed_manifest = (
        _validate_manifest_semantics_for_response(
            extracted_manifest,
            app_name=name.strip(),
        )
    )
    validation_warnings.extend(manifest_warnings)
    if manifest_errors:
        return {
            "error": "Validation failed",
            "validation_errors": manifest_errors,
            "validation_warnings": validation_warnings,
        }
    if typed_manifest is not None:
        config = _config_from_manifest(supported_chains, typed_manifest)
        manifest_errors, manifest_warnings, _ = _validate_manifest_semantics_for_response(
            extracted_manifest,
            app_name=name.strip(),
            config=config,
        )
        validation_warnings.extend(manifest_warnings)
        if manifest_errors:
            return {
                "error": "Validation failed",
                "validation_errors": manifest_errors,
                "validation_warnings": validation_warnings,
            }

    # Convert [[type, val], ...] from JSON to [(type, val), ...] tuples
    ctor_args = None
    if constructor_args:
        ctor_args = [(pair[0], pair[1]) for pair in constructor_args]

    definition = AppIntentDefinition(
        app_id=app_id,
        name=name.strip(),
        version="1.0.0",
        intent_type="",
        js_code=js_code,
        solidity_code=solidity_code,
        config=config,
        deployer=deployer_addr,
        description=description.strip() if description else "",
        manifest=extracted_manifest,
        constructor_args=ctor_args,
    )

    store.save_app(definition)

    result = asdict(definition)
    result["js_code_hash"] = _sha256(js_code)
    if solidity_code:
        result["solidity_code_hash"] = _sha256(solidity_code)
    if validation_warnings:
        result["validation_warnings"] = validation_warnings
    return result


def deploy_app_intent(
    store: AppIntentStore,
    app_id: str,
    chain_id: int | None = None,
    *,
    is_admin: bool = True,
    fee_paid: bool = False,
    payment: Any = None,
) -> dict[str, Any]:
    """Deploy an App Intent to a specific chain.

    When a DeployService is configured (via ``set_deploy_service``), this
    compiles the Solidity contract and deploys it on-chain.  Otherwise it
    falls back to a stub that generates a fake contract address (for tests
    and local development without a chain).

    Args:
        app_id:   The app_id returned by create_app_intent.
        chain_id: Target chain ID.  If None, defaults to the first chain
                  in the app's ``supported_chains``.
        is_admin: True for an operator/admin deploy (free, as today). A PUBLIC /
                  3rd-party deploy passes False and is gated by the #238 deploy-fee
                  / public-deployment check below.
        fee_paid: Whether the #238 deploy fee was collected (only meaningful for a
                  public deploy). Computed from ``payment`` when one is supplied.
        payment:  Optional ``deploy_payment.DeployFeePayment``. When supplied, the
                  deploy is treated as payment-backed (``is_admin=False``) and
                  authorized via the deployer's EIP-712 ``pay_deploy_fee``
                  signature + on-chain payment proof. ``None`` → admin deploy.

    Returns:
        DeploymentResult dict with status, contract address, and js_code_hash.
    """
    from ._state import _deploy_service
    from minotaur_subnet.deployment.deploy_fee import (
        DeploymentFeeRequired,
        require_deployment_authorized,
    )

    if not app_id:
        return {"error": "app_id is required"}

    # Hard gate (#238) for the no-payment case: admin deploys pass; a public
    # deploy with no payment claim is refused here, BEFORE any store lookup, so
    # an unauthorized public caller does zero work (and a bare store is fine).
    if payment is None:
        try:
            require_deployment_authorized(is_admin=is_admin, fee_paid=fee_paid)
        except DeploymentFeeRequired as exc:
            return {"error": str(exc), "deploy_fee_required": True}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    # Resolve target chain (a payment authorization binds the chain it paid for).
    supported = definition.config.supported_chains
    if not supported:
        return {"error": "App has no supported_chains configured"}
    if chain_id is None:
        chain_id = supported[0]
    elif chain_id not in supported:
        return {
            "error": (
                f"Chain {chain_id} is not in app's supported_chains "
                f"{supported}"
            ),
        }

    # A payment-backed deploy (a payment claim is supplied) is a public/3rd-party
    # deploy: authorize it via the deployer's EIP-712 pay_deploy_fee signature +
    # on-chain payment proof. verify_deploy_fee_payment IS the #238 gate for this
    # path — it enforces public_deployment_enabled() and only succeeds once the
    # fee is confirmed, consuming the nonce. No claim → admin deploy (free).
    if payment is not None:
        from minotaur_subnet.api.services.deploy_payment import verify_deploy_fee_payment

        ok, fee_err = verify_deploy_fee_payment(
            store, definition, chain_id=chain_id, payment=payment,
        )
        if not ok:
            return {"error": f"Deploy fee not authorized: {fee_err}", "deploy_fee_required": True}

    # Check not already deployed on this chain
    existing = store.get_deployment(app_id, chain_id=chain_id)
    if existing and existing.status in (
        AppStatus.ACTIVE, AppStatus.SOLVED, AppStatus.SOLVING, AppStatus.DEPLOYING,
    ):
        return {
            "error": (
                f"App {app_id} is already {existing.status.value} "
                f"on chain {chain_id}"
            ),
        }

    js_hash = _sha256(definition.js_code)

    # ── Real deployment path ────────────────────────────────────────────
    if _deploy_service is not None:
        import asyncio

        # Mark as deploying
        deploying = DeploymentResult(
            app_id=app_id,
            status=AppStatus.DEPLOYING,
            js_code_hash=js_hash,
            chain_id=chain_id,
        )
        store.save_deployment(deploying)

        import concurrent.futures

        def _run_in_new_loop():
            return asyncio.run(_deploy_service.deploy(definition, chain_id))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(_run_in_new_loop).result(timeout=300)
        except concurrent.futures.TimeoutError:
            return {
                "app_id": app_id,
                "status": "deploying",
                "error": "Deploy timed out (compilation may be slow)",
                "chain_id": chain_id,
            }
        except Exception as exc:
            return {
                "app_id": app_id,
                "status": "draft",
                "error": f"Deploy failed: {exc}",
                "chain_id": chain_id,
            }

        store.save_deployment(result)
        return asdict(result)

    # ── No relayer configured ────────────────────────────────────────────
    return {
        "error": (
            "No EVM relayer configured — cannot deploy on-chain. "
            "Set USE_EVM_RELAYER=1 with RELAYER_PRIVATE_KEY to enable."
        ),
    }


def list_minotaur_subnet(
    store: AppIntentStore,
    deployer: str | None = None,
) -> dict[str, Any]:
    """List all App Intents, optionally filtered by deployer address.

    Args:
        deployer: If provided, only return apps deployed by this address.

    Returns:
        Dict with "apps" key containing a list of AppIntentDefinition dicts,
        each enriched with a "status" field derived from deployment records.
    """
    apps = store.list_apps(deployer=deployer if deployer else None)
    result = []
    for a in apps:
        d = asdict(a)
        # Derive status from deployment records
        deployments = store.get_deployments(a.app_id)
        if deployments:
            statuses = [dep.status for dep in deployments.values()]
            if any(s == AppStatus.ACTIVE for s in statuses):
                d["status"] = "active"
            elif any(s == AppStatus.SOLVED for s in statuses):
                d["status"] = "solved"
            elif any(s.is_operational() for s in statuses):
                d["status"] = statuses[0].value
            else:
                d["status"] = "draft"
        else:
            d["status"] = "draft"
        result.append(d)
    return {
        "apps": result,
        "total": len(result),
    }


def get_app_status(
    store: AppIntentStore,
    app_id: str,
) -> dict[str, Any]:
    """Check an App Intent's health and execution statistics.

    Args:
        app_id: The app to check.

    Returns:
        Dict with status, execution count, average score, last triggered
        timestamp, deployment info, champion score, and per-scenario breakdown.
    """
    if not app_id:
        return {"error": "app_id is required"}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    deployment = store.get_deployment(app_id)  # primary (first active)
    deployments = store.get_deployments(app_id)
    stats = store.get_stats(app_id)
    quote_stats = store.get_quote_stats(app_id)

    # Champion score and per-scenario breakdown from submission store
    champion_score = 0.0
    scenario_scores: dict[str, float] = {}
    try:
        from minotaur_subnet.api.routes.submissions import get_store as _get_sub_store
        sub_store = _get_sub_store()
        champion = sub_store.get_champion()
        if champion is not None:
            champion_score = champion.benchmark_score or 0.0
            details = champion.benchmark_details or {}
            for entry in details.get("per_intent", []):
                intent_id = entry.get("intent_id", "")
                # Filter to this app's scenarios (intent_id starts with app_id)
                if intent_id.startswith(app_id):
                    scenario_scores[intent_id] = entry.get("score", 0.0)
    except Exception:
        pass  # Submission store not available -- degrade gracefully

    # Determine overall status from all chain deployments
    if deployments:
        statuses = [d.status for d in deployments.values()]
        if all(s == AppStatus.ACTIVE for s in statuses):
            status = AppStatus.ACTIVE.value
        elif all(s == AppStatus.SOLVED for s in statuses):
            status = AppStatus.SOLVED.value
        elif all(s.is_order_ready() for s in statuses):
            # Mix of ACTIVE + SOLVED
            status = AppStatus.SOLVED.value
        elif all(s == AppStatus.SOLVING for s in statuses):
            status = AppStatus.SOLVING.value
        elif any(s.is_operational() for s in statuses):
            status = AppStatus.PARTIAL.value
        elif any(s == AppStatus.DEPLOYING for s in statuses):
            status = AppStatus.DEPLOYING.value
        else:
            status = AppStatus.DRAFT.value
    else:
        status = AppStatus.DRAFT.value

    return {
        "app_id": app_id,
        "name": definition.name,
        "status": status,
        # Full app definition (includes solidity_code, js_code, config, etc.)
        "app": asdict(definition),
        "execution_count": stats["total_executions"],
        "successful_executions": stats["successful_executions"],
        "avg_score": round(stats["avg_score"], 4),
        "best_score": round(stats["best_score"], 4),
        "last_triggered": stats["last_triggered"],
        "recent_scores": stats.get("recent_scores", []),
        "quote_stats": quote_stats,
        "champion_score": round(champion_score, 4),
        "scenario_scores": scenario_scores,
        # Primary deployment
        "deployment": {
            "contract_address": deployment.contract_address,
            "chain_id": deployment.chain_id,
            "status": deployment.status.value,
            "abi": deployment.abi,
        } if deployment else None,
        # Per-chain deployments
        "deployments": {
            cid: {
                "contract_address": d.contract_address,
                "chain_id": d.chain_id,
                "status": d.status.value,
            }
            for cid, d in deployments.items()
        },
    }


def update_scoring(
    store: AppIntentStore,
    app_id: str,
    new_js_code: str,
    caller: str = "",
    signature: str = "",
    nonce: int = 0,
    deadline: int = 0,
) -> dict[str, Any]:
    """Update the JS scoring code for an existing App Intent.

    The new code will be distributed to validators on the next sync cycle.
    The old code remains active until validators confirm the update.

    Args:
        app_id:      The app whose scoring to update.
        new_js_code: New JavaScript scoring source code.
        caller:      Deprecated, ignored. Authorization is by ``signature`` only.
        signature:   EIP-712 developer-auth signature from the app's deployer
                     (see ``developer_auth``). Required when a deployer is set.
                     Binds (action="update_scoring", app_id, keccak(new_js_code),
                     nonce, deadline); the nonce is consumed once on success.
        nonce:       The deployer's next developer-auth nonce (read from
                     ``GET /apps/{id}/auth-nonce``). Must equal last_consumed + 1.
        deadline:    Unix-seconds expiry the signature was signed with.

    Returns:
        Dict with the new js_code_hash and update status.
    """
    if not app_id:
        return {"error": "app_id is required"}
    if not new_js_code or not new_js_code.strip():
        return {"error": "new_js_code must be non-empty"}

    validation_warnings: list[str] = []
    extracted_manifest: dict[str, Any] | None = None

    # ── pre-flight JS validation ──────────────────────────────────────
    try:
        import asyncio
        from minotaur_subnet.engine.validation import validate_js_code as _validate_js

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    js_validation = pool.submit(
                        lambda: asyncio.run(_validate_js(new_js_code))
                    ).result()
            else:
                js_validation = loop.run_until_complete(
                    _validate_js(new_js_code)
                )
        except RuntimeError:
            js_validation = asyncio.run(_validate_js(new_js_code))

        if not js_validation.valid:
            return {
                "error": "JS validation failed",
                "validation_errors": js_validation.errors,
                "validation_warnings": js_validation.warnings,
            }
        validation_warnings.extend(js_validation.warnings)
        if isinstance(js_validation.js_manifest, dict):
            extracted_manifest = js_validation.js_manifest
    except Exception as exc:
        logger.warning("Pre-flight JS validation skipped: %s", exc)

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    # Authorization: if the app has a deployer, only that address can update,
    # proven by an EIP-712 developer-auth signature with a single-use nonce.
    # The nonce + deadline make a captured signature unusable twice — closing
    # the version-rollback replay the old nonce-less scheme allowed.
    if definition.deployer:
        from minotaur_subnet.api.services import developer_auth

        deployer_addr = definition.deployer.strip().lower()
        ok, err = developer_auth.verify_developer_auth(
            expected_deployer=deployer_addr,
            action=developer_auth.ACTION_UPDATE_SCORING,
            app_id=app_id,
            params_hash=developer_auth.params_hash(new_js_code.encode()),
            nonce=nonce,
            deadline=deadline,
            signature=signature,
        )
        if not ok:
            return {"error": f"Unauthorized: {err}"}
        # Consume the nonce only after the signature checks out, so a bad
        # signature never burns a nonce. Atomic in the store (replay-safe).
        consumed, cerr = store.consume_developer_nonce(app_id, deployer_addr, nonce)
        if not consumed:
            return {"error": f"Unauthorized: {cerr}"}

    manifest_errors, manifest_warnings, typed_manifest = (
        _validate_manifest_semantics_for_response(
            extracted_manifest,
            app_name=definition.name,
        )
    )
    validation_warnings.extend(manifest_warnings)
    if manifest_errors:
        return {
            "error": "JS validation failed",
            "validation_errors": manifest_errors,
            "validation_warnings": validation_warnings,
        }

    updated_config = definition.config
    if typed_manifest is not None:
        updated_config = _config_from_manifest(
            definition.config.supported_chains,
            typed_manifest,
            base_config=definition.config,
        )
        manifest_errors, manifest_warnings, _ = _validate_manifest_semantics_for_response(
            extracted_manifest,
            app_name=definition.name,
            config=updated_config,
        )
        validation_warnings.extend(manifest_warnings)
        if manifest_errors:
            return {
                "error": "JS validation failed",
                "validation_errors": manifest_errors,
                "validation_warnings": validation_warnings,
            }

    old_hash = _sha256(definition.js_code)
    new_hash = _sha256(new_js_code)

    if old_hash == new_hash:
        return {
            "app_id": app_id,
            "js_code_hash": old_hash,
            "status": "unchanged",
            "message": "New code is identical to the current version.",
        }

    # Bump version
    parts = definition.version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts[-1] = "1"
    new_version = ".".join(parts)

    definition.js_code = new_js_code
    definition.version = new_version
    definition.manifest = extracted_manifest
    definition.config = updated_config
    store.save_app(definition)

    result = {
        "app_id": app_id,
        "js_code_hash": new_hash,
        "previous_hash": old_hash,
        "version": new_version,
        "status": "updated",
        "message": "JS scoring code updated. Validators will receive the new code on next sync.",
    }
    if validation_warnings:
        result["validation_warnings"] = validation_warnings
    return result


def update_shadow_scoring(
    store: AppIntentStore,
    app_id: str,
    new_js_code: str,
    caller: str = "",
    signature: str = "",
    nonce: int = 0,
    deadline: int = 0,
) -> dict[str, Any]:
    """Set the SHADOW (observe-only) raw-output scoring JS for an App Intent.

    Mirrors :func:`update_scoring`'s auth/validation shape, but writes
    ``shadow_js_code`` instead of ``js_code`` and does NOT bump the version or
    touch the manifest/config — the shadow scorer runs ALONGSIDE the live one and
    must never alter live behaviour. Validators load it when
    ``relative_scoring_shadow_enabled()`` and score it in parallel; until
    ``relative_scoring_active()`` it changes nothing about adoption.
    """
    if not app_id:
        return {"error": "app_id is required"}
    if not new_js_code or not new_js_code.strip():
        return {"error": "new_js_code must be non-empty"}

    new_js_code = new_js_code.strip()
    validation_warnings: list[str] = []

    # ── pre-flight JS validation (same as update_scoring) ──
    try:
        import asyncio
        from minotaur_subnet.engine.validation import validate_js_code as _validate_js

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    js_validation = pool.submit(
                        lambda: asyncio.run(_validate_js(new_js_code))
                    ).result()
            else:
                js_validation = loop.run_until_complete(_validate_js(new_js_code))
        except RuntimeError:
            js_validation = asyncio.run(_validate_js(new_js_code))

        if not js_validation.valid:
            return {
                "error": "JS validation failed",
                "validation_errors": js_validation.errors,
                "validation_warnings": js_validation.warnings,
            }
        validation_warnings.extend(js_validation.warnings)
    except Exception as exc:
        logger.warning("Pre-flight shadow JS validation skipped: %s", exc)

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    # Authorization: identical to update_scoring — a deployer-set app requires the
    # deployer's EIP-712 developer-auth signature (single-use nonce) binding the
    # shadow code hash. Reuses the update_scoring action so no new signed action
    # type is introduced for this minimal observe-only endpoint.
    if definition.deployer:
        from minotaur_subnet.api.services import developer_auth

        deployer_addr = definition.deployer.strip().lower()
        ok, err = developer_auth.verify_developer_auth(
            expected_deployer=deployer_addr,
            action=developer_auth.ACTION_UPDATE_SCORING,
            app_id=app_id,
            params_hash=developer_auth.params_hash(new_js_code.encode()),
            nonce=nonce,
            deadline=deadline,
            signature=signature,
        )
        if not ok:
            return {"error": f"Unauthorized: {err}"}
        consumed, cerr = store.consume_developer_nonce(app_id, deployer_addr, nonce)
        if not consumed:
            return {"error": f"Unauthorized: {cerr}"}

    new_hash = _sha256(new_js_code)
    if definition.shadow_js_code and _sha256(definition.shadow_js_code) == new_hash:
        return {
            "app_id": app_id,
            "shadow_js_code_hash": new_hash,
            "status": "unchanged",
            "message": "New shadow code is identical to the current shadow version.",
        }

    definition.shadow_js_code = new_js_code
    store.save_app(definition)

    result = {
        "app_id": app_id,
        "shadow_js_code_hash": new_hash,
        "status": "updated",
        "message": "Shadow scoring JS updated (observe-only; runs alongside live JS).",
    }
    if validation_warnings:
        result["validation_warnings"] = validation_warnings
    return result


async def validate_app_intent_code(
    js_code: str,
    solidity_code: str = "",
    skip_solidity: bool = False,
) -> dict[str, Any]:
    """Pre-flight validation for App Intent JS and/or Solidity code.

    Validates JS by loading it in a sandbox (checks syntax, required exports).
    Optionally validates Solidity by compiling with Forge.

    Args:
        js_code:        JavaScript scoring module source.
        solidity_code:  Solidity contract source (optional).
        skip_solidity:  If True, skip Solidity compilation check.

    Returns:
        Dict with valid, errors, warnings, and extracted metadata.
    """
    from minotaur_subnet.engine.validation import validate_app_intent

    result = await validate_app_intent(js_code, solidity_code, skip_solidity=skip_solidity)
    errors = list(result.errors)
    warnings = list(result.warnings)
    if isinstance(result.js_manifest, dict):
        manifest_errors, manifest_warnings, _ = _validate_manifest_semantics_for_response(
            result.js_manifest,
            app_name=(result.js_config or {}).get("name", "") if isinstance(result.js_config, dict) else "",
        )
        errors.extend(manifest_errors)
        warnings.extend(manifest_warnings)

    response: dict[str, Any] = {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }

    if result.js_config is not None:
        response["js_config"] = result.js_config
    if result.js_manifest is not None:
        response["js_manifest"] = result.js_manifest
    if result.js_exports:
        response["js_exports"] = result.js_exports
    if result.solidity_abi is not None:
        response["solidity_abi_functions"] = [
            entry.get("name", "")
            for entry in result.solidity_abi
            if entry.get("type") == "function"
        ]
    if result.solidity_contract_name:
        response["solidity_contract_name"] = result.solidity_contract_name

    return response


async def get_app_manifest(
    store: AppIntentStore,
    app_id: str,
) -> dict[str, Any]:
    """Extract and return the JS manifest for an app.

    The manifest contains intent function definitions, parameter schemas,
    example params, and scoring hints -- everything a miner needs to build
    plans intelligently.

    Args:
        store:  The app intent store.
        app_id: The app whose manifest to extract.

    Returns:
        Dict with manifest data, or error dict.
    """
    if not app_id:
        return {"error": "app_id is required"}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    from minotaur_subnet.engine import JsExecutionEngine

    engine = JsExecutionEngine(timeout_ms=3000, max_memory_mb=64)
    try:
        await engine.load_intent(app_id, definition.js_code)
        manifest = engine.get_manifest(app_id)
    except Exception as exc:
        return {"error": f"Failed to extract manifest: {exc}"}

    if not manifest:
        return {
            "app_id": app_id,
            "manifest": None,
            "message": "This app does not export a manifest.",
        }

    return {
        "app_id": app_id,
        "manifest": manifest,
    }


def build_intent_params_hex_from_manifest(
    store: Any,
    js_engine: Any,
    app_id: str,
    intent_function: str,
    params: dict[str, Any],
    submitted_by: str,
) -> str | None:
    """ABI-encode intentParams using the app's manifest param schema.

    Generic, manifest-driven encoder that works for ANY app: it ABI-encodes
    EVERY param the manifest declares, in order, using a type-default for any
    value the caller didn't supply. Emitting the full fixed-width tuple (rather
    than skipping missing fields) is what makes the on-chain abi.decode line up
    — a skipped field would shift every later field and corrupt the decode.
    Computational/quoted params (min_output, platform_fee_wei, quoted_output,
    or whatever a given app declares) are populated upstream by the quote's
    source:"quote" loop; here we just lay them out per the manifest. Returns
    None only if no manifest/intent spec is available.
    """
    try:
        from eth_abi import encode as abi_encode
        from web3 import Web3

        # Get manifest from JS engine or store
        manifest = None
        app = store.get_app(app_id) if store else None
        if app and app.manifest:
            from minotaur_subnet.v3.manifest import IntentManifest
            manifest = app.manifest if isinstance(app.manifest, IntentManifest) else None
            if manifest is None:
                from minotaur_subnet.v3.manifest import manifest_from_legacy_dict
                if isinstance(app.manifest, dict):
                    manifest = manifest_from_legacy_dict(app.manifest)

        if js_engine and manifest is None:
            try:
                raw = js_engine.get_manifest(app_id)
                if raw:
                    from minotaur_subnet.v3.manifest import manifest_from_legacy_dict
                    manifest = manifest_from_legacy_dict(raw)
            except Exception:
                pass

        if manifest is None:
            return None

        # Find the intent function spec
        intent_spec = manifest.get_intent(intent_function)
        if intent_spec is None:
            return None

        # Build ABI types and values from the manifest params. Emit EVERY
        # declared field in order, defaulting any value the caller omitted —
        # fixed-layout abi.decode on-chain requires the full ordered tuple, so
        # we must NOT skip missing fields (that would shift every later field).
        ZERO_ADDR = "0x0000000000000000000000000000000000000000"
        abi_types = []
        abi_values = []
        for field in intent_spec.params:
            vtype = field.value_type
            val = params.get(field.name)
            # 'receiver' conventionally defaults to the order submitter.
            if val is None and field.name == "receiver":
                val = submitted_by
            abi_types.append(vtype)
            if vtype == "address":
                abi_values.append(Web3.to_checksum_address(val) if val else ZERO_ADDR)
            elif vtype == "address[]":
                items = val if isinstance(val, list) else ([val] if val else [])
                abi_values.append([Web3.to_checksum_address(a) for a in items])
            elif vtype.startswith("uint") or vtype.startswith("int"):
                if "[]" in vtype:
                    items = val if isinstance(val, list) else ([val] if val is not None else [])
                    abi_values.append([int(v) for v in items])
                else:
                    abi_values.append(int(val) if val is not None else 0)
            elif vtype == "bool":
                abi_values.append(bool(val) if val is not None else False)
            elif vtype == "bytes":  # dynamic bytes
                if isinstance(val, (bytes, bytearray)):
                    abi_values.append(bytes(val))
                elif isinstance(val, str) and val:
                    abi_values.append(bytes.fromhex(val.replace("0x", "")))
                else:
                    abi_values.append(b"")
            elif vtype.startswith("bytes"):  # fixed bytesN (e.g. bytes32)
                n = int(vtype[5:]) if vtype[5:].isdigit() else 32
                if isinstance(val, (bytes, bytearray)):
                    abi_values.append(bytes(val)[:n].ljust(n, b"\x00"))
                elif isinstance(val, str) and val:
                    abi_values.append(bytes.fromhex(val.replace("0x", "").ljust(n * 2, "0"))[:n])
                else:
                    abi_values.append(b"\x00" * n)
            else:
                abi_values.append(val if val is not None else 0)

        if not abi_types:
            return None

        encoded = abi_encode(abi_types, abi_values)
        return encoded.hex()
    except Exception as exc:
        logger.warning("Failed to encode manifest intent params: %s", exc)
        return None


def map_quote_result_to_params(
    quote_result: Any,
    manifest_dict: dict[str, Any] | None,
    intent_function: str,
    slippage_bps: int = 50,
) -> dict[str, str]:
    """Map a solver QuoteResult onto an app's source:"quote" intent params.

    This is the ONE place the quote → param mapping lives. The live
    ``get_quote`` endpoint (api/routes/orders.py) and the benchmark
    quote-at-benchmark path both depend on the same contract: for the
    manifest's ``intent_function``, every param whose definition has
    ``source == "quote"`` is set to ``quote_values[param_def["quote_field"]]``.

    Args:
        quote_result: A ``QuoteResult`` (or any object exposing
            ``estimated_output``, ``platform_fee_wei``, ``gas_estimate``).
        manifest_dict: The app's manifest in legacy-dict form (as returned by
            ``JsExecutionEngine.get_manifest``). May be ``None``.
        intent_function: The intent function name to resolve params for.
        slippage_bps: Slippage applied to derive ``suggested_min_output`` from
            ``estimated_output`` (default 50 = 0.5%).

    Returns:
        A dict of ``{param_name: value}`` for ONLY the source:"quote" params
        the manifest declares for ``intent_function``. Empty dict if there's no
        manifest, no matching function, or no quote params.
    """
    if quote_result is None or not manifest_dict:
        return {}

    estimated_output = str(getattr(quote_result, "estimated_output", "0") or "0")
    suggested_min_output = "0"
    try:
        est_int = int(estimated_output)
        if est_int > 0:
            bps = max(0, min(int(slippage_bps), 10000))
            suggested_min_output = str(est_int * (10000 - bps) // 10000)
    except (ValueError, TypeError):
        pass

    # Mirror the quote_values dict the live get_quote endpoint binds against.
    quote_values = {
        "estimated_output": estimated_output,
        "estimated_output_gross": estimated_output,
        "suggested_min_output": suggested_min_output,
        "platform_fee_wei": str(getattr(quote_result, "platform_fee_wei", "0") or "0"),
        "gas_estimate": str(getattr(quote_result, "gas_estimate", 0) or 0),
    }

    mapped: dict[str, str] = {}
    for fn_def in manifest_dict.get("intent_functions", []) or []:
        if fn_def.get("name") != intent_function:
            continue
        for param_name, param_def in (fn_def.get("params", {}) or {}).items():
            if not isinstance(param_def, dict):
                continue
            if param_def.get("source") == "quote":
                qf = param_def.get("quote_field", "")
                if qf and qf in quote_values:
                    mapped[param_name] = quote_values[qf]
        break
    return mapped


def source_quote_param_names(
    manifest_dict: dict[str, Any] | None,
    intent_function: str,
) -> set[str]:
    """Names of the params an app sources from a quote for ``intent_function``.

    The one gate used to decide whether a benchmark scenario needs
    quote-at-benchmark enrichment. Keyed off the manifest (NOT
    ``AppIntentDefinition.intent_type``, which is empty for the live
    DexAggregator app — keying on it makes enrichment a silent no-op).
    """
    if not manifest_dict:
        return set()
    for fn_def in manifest_dict.get("intent_functions", []) or []:
        if fn_def.get("name") != intent_function:
            continue
        return {
            n for n, pdef in (fn_def.get("params", {}) or {}).items()
            if isinstance(pdef, dict) and pdef.get("source") == "quote"
        }
    return set()
