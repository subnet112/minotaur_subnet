"""Production Stage-2 corpus from chain (plan Phase 5b).

A freshly-promoted leader has no local order history, so Stage 2 (~60% of the
champion score) collapses. This rebuilds the filled-order corpus from the generic
``IntentExecuted`` event + ``executeIntent`` calldata (any app) — no contract
changes — so Stage 2 is reproducible without a synced local store. Gated by
``BENCHMARK_CHAIN_CORPUS`` (default off = today's app_store.list_orders path).

Reuses the chain-reading primitives proven live in
``scoring_lab/order_recovery.py`` and the AppRegistry/RPC resolution in
``consensus/app_registry_cache.py`` — nothing hardcoded.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from minotaur_subnet.harness.scoring_lab.order_recovery import recover_corpus

logger = logging.getLogger(__name__)


def decode_intent_params_hex(manifest: Any, intent_function: str,
                             intent_params_hex: str) -> dict[str, Any]:
    """Inverse of ``api/services/app_service.build_intent_params_hex_from_manifest``:
    decode the ABI bytes back to a ``{param_name: value}`` dict using the manifest's
    ordered param types.

    MUST stay the exact inverse of the encoder (locked by the round-trip test) — a
    wrong inverse silently corrupts every Stage-2 scenario. Conventions match the
    encoder + JSON order submission: uints -> decimal strings, addresses ->
    checksummed, bytes -> 0x-hex, bool -> bool.
    """
    from eth_abi import decode as abi_decode
    from web3 import Web3

    spec = manifest.get_intent(intent_function) if manifest else None
    if spec is None:
        return {}
    types = [f.value_type for f in spec.params]
    if not types:
        return {}
    raw = bytes.fromhex(intent_params_hex.replace("0x", ""))
    vals = abi_decode(types, raw)
    out: dict[str, Any] = {}
    for field, v in zip(spec.params, vals):
        vt = field.value_type
        if vt == "address":
            out[field.name] = Web3.to_checksum_address(v)
        elif vt == "address[]":
            out[field.name] = [Web3.to_checksum_address(a) for a in v]
        elif vt.startswith("uint") or vt.startswith("int"):
            out[field.name] = [str(int(x)) for x in v] if "[]" in vt else str(int(v))
        elif vt == "bool":
            out[field.name] = bool(v)
        elif vt == "bytes" or vt.startswith("bytes"):
            out[field.name] = "0x" + (bytes(v).hex() if isinstance(v, (bytes, bytearray)) else "")
        else:
            out[field.name] = v
    return out


def _function_for_selector(manifest: Any, selector_hex: str) -> str | None:
    """Map a recovered 4-byte intent selector back to the manifest function name."""
    from minotaur_subnet.v3.manifest import compute_selector_from_manifest
    target = (selector_hex or "").lower().replace("0x", "")
    for fn in getattr(manifest, "intent_functions", []):
        sel = (fn.selector or "").lower().replace("0x", "")
        if not sel:
            try:
                sel = compute_selector_from_manifest(manifest, fn.name).lower().replace("0x", "")
            except Exception:
                continue
        if sel == target:
            return fn.name
    return None


def _resolve_app_id_from_contract(app_store: Any, contract_address: str) -> str | None:
    """Map a deployed contract address back to the store's string app_id by scanning
    deployments. Returns None (order dropped + logged) if not found — canonical_order
    emits a contract ADDRESS as app_id, but Stage-2 keys apps by the store app_id."""
    if app_store is None or not contract_address:
        return None
    target = contract_address.lower()
    for app in app_store.list_apps():
        try:
            dep = app_store.get_deployment(app.app_id)
        except Exception:
            dep = None
        if dep and (getattr(dep, "contract_address", "") or "").lower() == target:
            return app.app_id
    return None


def _get_manifest(app_store: Any, js_engine: Any, app_id: str) -> Any:
    """Resolve an app's IntentManifest (mirrors the encoder's resolution)."""
    from minotaur_subnet.v3.manifest import IntentManifest, manifest_from_legacy_dict
    app = app_store.get_app(app_id) if app_store else None
    if app and getattr(app, "manifest", None):
        if isinstance(app.manifest, IntentManifest):
            return app.manifest
        if isinstance(app.manifest, dict):
            return manifest_from_legacy_dict(app.manifest)
    if js_engine is not None:
        try:
            raw = js_engine.get_manifest(app_id)
            if raw:
                return manifest_from_legacy_dict(raw)
        except Exception:
            pass
    return None


def build_chain_corpus(app_store: Any, js_engine: Any, chain_id: int, *,
                       confirmations: int = 1, from_block: int = 0) -> list[dict]:
    """Build chain-derived filled-order records (sample_historical_orders shape) for a
    chain: scan IntentExecuted per registered app contract, decode params, remap the
    contract address to the store app_id. Fail-closed (empty + WARN) if no live RPC —
    never scans a sim fork. Reorg-safe via a confirmed-block cutoff.
    """
    from minotaur_subnet.consensus.app_registry_cache import _chain_rpc_env
    rpc = _chain_rpc_env(chain_id)
    if not rpc:
        logger.warning("chain corpus: no live RPC for chain %s (set BASE_UPSTREAM_RPC_URL etc.) "
                       "— empty corpus (fail-closed)", chain_id)
        return []
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(rpc))
    to_block = max(0, w3.eth.block_number - int(confirmations))

    records: list[dict] = []
    for app in app_store.list_apps():
        dep = app_store.get_deployment(app.app_id) if app_store else None
        contract = getattr(dep, "contract_address", None) if dep else None
        if not contract or getattr(dep, "chain_id", chain_id) != chain_id:
            continue
        manifest = _get_manifest(app_store, js_engine, app.app_id)
        if manifest is None:
            logger.warning("chain corpus: no manifest for %s; skipping", app.app_id)
            continue
        try:
            recovered = recover_corpus(w3, contract, from_block=from_block, to_block=to_block)
        except Exception as exc:
            logger.warning("chain corpus: recover failed for %s: %s", contract, exc)
            continue
        for rec in recovered:
            fn = _function_for_selector(manifest, rec.get("intent_selector", ""))
            if fn is None:
                continue
            params = decode_intent_params_hex(manifest, fn, rec.get("intent_params_hex", ""))
            if not params:
                logger.warning("chain corpus: decode failed for order %s; dropped",
                               rec.get("order_id"))
                continue
            rec["app_id"] = app.app_id              # remap contract address -> store id
            rec["params"] = params
            rec["intent_function"] = fn
            records.append(rec)
    logger.info("chain corpus: %d filled orders recovered for chain %s (to_block %d)",
                len(records), chain_id, to_block)
    return records


def chain_corpus_enabled() -> bool:
    return os.environ.get("BENCHMARK_CHAIN_CORPUS", "").strip().lower() in ("1", "true", "yes", "on")
