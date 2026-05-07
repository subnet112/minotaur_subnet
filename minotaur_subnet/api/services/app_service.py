"""App Intent lifecycle service functions.

Functions: create_app_intent, deploy_app_intent, validate_app_intent_code,
list_minotaur_subnet, get_app_status, update_scoring, activate_app,
get_app_manifest, list_app_manifests.
"""

from __future__ import annotations

import hashlib
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

    Returns:
        DeploymentResult dict with status, contract address, and js_code_hash.
    """
    from ._state import _deploy_service

    if not app_id:
        return {"error": "app_id is required"}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    # Resolve target chain
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


def _verify_scoring_update_signature(
    app_id: str,
    new_js_code: str,
    signature: str,
    expected_deployer: str,
) -> tuple[bool, str]:
    """Verify EIP-191 signature for a scoring update request.

    The signed message is keccak256(abi.encode(app_id, sha256(new_js_code))).
    Returns (ok, error_message).
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        from eth_abi import encode as abi_encode
        from web3 import Web3
    except ImportError as exc:
        logger.warning("Signature verification deps missing: %s", exc)
        return False, f"Server missing dependency for signature verification: {exc}"

    js_code_hash = hashlib.sha256(new_js_code.encode()).hexdigest()
    # abi.encode(string, string) -- keccak256 of that gives the message
    encoded = abi_encode(["string", "string"], [app_id, js_code_hash])
    message_hash = Web3.keccak(encoded)

    # Sign over the raw 32-byte hash using EIP-191 personal sign
    signable = encode_defunct(primitive=message_hash)
    try:
        recovered = Account.recover_message(signable, signature=signature)
    except Exception as exc:
        return False, f"Signature recovery failed: {exc}"

    if recovered.lower() != expected_deployer.strip().lower():
        return False, (
            f"Signature mismatch: recovered {recovered.lower()}, "
            f"expected deployer {expected_deployer.strip().lower()}"
        )
    return True, ""


def update_scoring(
    store: AppIntentStore,
    app_id: str,
    new_js_code: str,
    caller: str = "",
    signature: str = "",
) -> dict[str, Any]:
    """Update the JS scoring code for an existing App Intent.

    The new code will be distributed to validators on the next sync cycle.
    The old code remains active until validators confirm the update.

    Args:
        app_id:      The app whose scoring to update.
        new_js_code: New JavaScript scoring source code.
        caller:      Address of the caller. Must match the app's deployer
                     (if one was set at creation time).
        signature:   EIP-191 signature proving the caller owns the deployer
                     address. Message = keccak256(abi.encode(app_id, sha256(new_js_code))).
                     Optional for backward compatibility but strongly recommended.

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

    # Authorization: if the app has a deployer, only that address can update.
    # Prefer cryptographic signature verification; fall back to caller field
    # with a deprecation warning for backward compatibility.
    if definition.deployer:
        deployer_addr = definition.deployer.strip().lower()
        if not signature:
            return {
                "error": "Unauthorized: signature is required to update scoring. "
                "Sign keccak256(app_id + js_code_hash) with the deployer key."
            }
        ok, err = _verify_scoring_update_signature(
            app_id, new_js_code, signature, deployer_addr,
        )
        if not ok:
            return {"error": f"Unauthorized: {err}"}

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

    Generic version of build_swap_intent_params_hex that works for any app.
    Falls back to the swap-specific encoder if no manifest is available.
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

        # Build ABI types and values from the manifest params
        abi_types = []
        abi_values = []
        for field in intent_spec.params:
            val = params.get(field.name)
            if val is None:
                continue  # Skip missing optional params
            vtype = field.value_type
            abi_types.append(vtype)
            if vtype == "address":
                abi_values.append(Web3.to_checksum_address(val))
            elif vtype == "address[]":
                # Array of addresses -- checksum each one
                if isinstance(val, list):
                    abi_values.append([Web3.to_checksum_address(a) for a in val])
                else:
                    abi_values.append([Web3.to_checksum_address(val)])
            elif vtype.startswith("uint") or vtype.startswith("int"):
                if "[]" in vtype:
                    abi_values.append([int(v) for v in val] if isinstance(val, list) else [int(val)])
                else:
                    abi_values.append(int(val))
            elif vtype == "bool":
                abi_values.append(bool(val))
            else:
                abi_values.append(val)

        if not abi_types:
            return None

        encoded = abi_encode(abi_types, abi_values)
        return encoded.hex()
    except Exception as exc:
        logger.warning("Failed to encode manifest intent params: %s", exc)
        return None
