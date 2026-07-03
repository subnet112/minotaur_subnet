"""App lifecycle management: solidity update, deployment retire, V2 float
ops, relayer-gated config, AppRegistry calldata.

These are the write-side operations behind the app-management frontend that
were previously impossible via the API (issue context: migrating the dex
aggregator to the V2 contracts):

- ``update_app_solidity`` — replace the stored contract source so a
  redeploy compiles the NEW generation (previously only JS was updatable).
- ``retire_deployment`` — mark a chain's deployment RETIRED, which releases
  ``deploy_app_intent``'s already-active guard; the existing deploy
  endpoint then works as an in-place redeploy (``save_deployment`` upserts
  on (app_id, chain_id), so the same app_id keeps flowing through
  app-sync, manifests, and the benchmark).
- ``float_deposit`` / ``float_withdraw`` — fund or recover the V2 app-held
  WETH fee float from the relayer wallet (``withdrawFloat`` is
  relayer-gated on-chain).
- ``set_app_config`` — the V2 relayer-gated setters (feeBps, volumeCapBps,
  feeCollector).
- ``registry_calldata`` — prepared calldata for AppRegistry
  ``registerApp``/``revokeApp``. Registration is a developer call (the
  relayer can send it if allowlisted) but re-pointing an existing appId
  needs the registry OWNER's ``revokeApp`` first — that key stays cold, so
  we hand the frontend calldata to sign externally instead of holding it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from minotaur_subnet.shared.types import AppStatus

logger = logging.getLogger(__name__)


def _relayer() -> Any:
    from ._state import _deploy_service

    return getattr(_deploy_service, "relayer", None) if _deploy_service else None


def _run_async(coro) -> Any:
    """Run a relayer coroutine from sync service code (route runs us in an
    executor thread, so a fresh event loop per call is safe)."""
    return asyncio.run(coro)


def update_app_solidity(
    store: Any,
    app_id: str,
    solidity_code: str,
    constructor_args: list[list[str]] | None = None,
    contract_version: str = "",
) -> dict[str, Any]:
    """Replace an app's stored contract source (and optionally ctor args /
    generation marker) so the next deploy compiles the new code.

    Refuses while any chain is mid-deploy — swapping the source under a
    running compile would make the stored code and the deployed artifact
    disagree.
    """
    if not solidity_code or not solidity_code.strip():
        return {"error": "solidity_code is required"}
    contract_version = (contract_version or "").strip().lower()
    if contract_version and contract_version not in ("v1", "v2"):
        return {"error": f"contract_version must be 'v1' or 'v2', got {contract_version!r}"}

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    for chain_id, dep in (store.get_deployments(app_id) or {}).items():
        if dep.status == AppStatus.DEPLOYING:
            return {"error": f"Deploy in progress on chain {chain_id}; retry after it settles"}

    definition.solidity_code = solidity_code
    if constructor_args is not None:
        definition.constructor_args = [tuple(a) for a in constructor_args]
    if contract_version:
        definition.contract_version = contract_version
    store.save_app(definition)

    return {
        "app_id": app_id,
        "solidity_code_sha256": hashlib.sha256(solidity_code.encode()).hexdigest(),
        "contract_version": definition.contract_version or "v1",
        "updated": True,
    }


def retire_deployment(store: Any, app_id: str, chain_id: int) -> dict[str, Any]:
    """Mark a deployment RETIRED — frees the deploy guard for a redeploy.

    The on-chain contract is untouched (recover any V2 float FIRST via
    ``float_withdraw``); this only changes the store record that order
    routing, app-sync, and the deploy guard read.
    """
    dep = store.get_deployment(app_id, chain_id=chain_id)
    if dep is None:
        return {"error": f"No deployment for {app_id} on chain {chain_id}"}
    if dep.status == AppStatus.DEPLOYING:
        return {"error": "Deploy in progress; cannot retire mid-deploy"}
    store.update_deployment_status(app_id, chain_id, AppStatus.RETIRED)
    return {
        "app_id": app_id,
        "chain_id": chain_id,
        "status": "retired",
        "contract_address": dep.contract_address,
    }


def _deployed_address(store: Any, app_id: str, chain_id: int) -> str | None:
    dep = store.get_deployment(app_id, chain_id=chain_id)
    return getattr(dep, "contract_address", None) if dep else None


def float_deposit(
    store: Any, app_id: str, chain_id: int, amount_wei: int, wrap: bool = True,
) -> dict[str, Any]:
    """Fund the V2 app-held WETH fee float from the relayer wallet.

    ``wrap=True`` (default) wraps relayer ETH via ``WETH.deposit`` first,
    then transfers — the relayer holds gas ETH, not WETH, in steady state.
    """
    if amount_wei <= 0:
        return {"error": "amount_wei must be positive"}
    relayer = _relayer()
    if relayer is None:
        return {"error": "No EVM relayer configured"}
    app_addr = _deployed_address(store, app_id, chain_id)
    if not app_addr:
        return {"error": f"No deployed contract for {app_id} on chain {chain_id}"}

    from minotaur_subnet.api.services.app_admin import _view_address

    from minotaur_subnet.blockchain.chains import get_web3

    w3 = get_web3(chain_id)
    weth = _view_address(w3, app_addr, "wrappedNativeToken")
    if not weth or not int(weth, 16):
        return {"error": "App has no wrappedNativeToken() — not a fee app"}

    txs: dict[str, str] = {}
    if wrap:
        txs["wrap"] = _run_async(relayer.call_contract_function(
            weth, chain_id, "deposit()", [], [], tx_value=amount_wei, gas=100_000,
        ))
    txs["transfer"] = _run_async(relayer.call_contract_function(
        weth, chain_id, "transfer(address,uint256)",
        ["address", "uint256"], [app_addr, amount_wei], gas=100_000,
    ))
    return {"app_id": app_id, "chain_id": chain_id, "amount_wei": amount_wei, "txs": txs}


def float_withdraw(
    store: Any, app_id: str, chain_id: int, to: str, amount_wei: int,
) -> dict[str, Any]:
    """Recover the V2 WETH float via the app's relayer-gated withdrawFloat."""
    if amount_wei <= 0:
        return {"error": "amount_wei must be positive"}
    if not to or not int(to, 16):
        return {"error": "recipient 'to' is required"}
    relayer = _relayer()
    if relayer is None:
        return {"error": "No EVM relayer configured"}
    app_addr = _deployed_address(store, app_id, chain_id)
    if not app_addr:
        return {"error": f"No deployed contract for {app_id} on chain {chain_id}"}

    tx = _run_async(relayer.call_contract_function(
        app_addr, chain_id, "withdrawFloat(address,uint256)",
        ["address", "uint256"], [to, amount_wei], gas=120_000,
    ))
    return {"app_id": app_id, "chain_id": chain_id, "to": to, "amount_wei": amount_wei, "tx": tx}


# Relayer-gated V2 config setters: request field -> (signature, abi type).
_CONFIG_SETTERS = {
    "fee_bps": ("setFeeBps(uint256)", "uint256"),
    "volume_cap_bps": ("setVolumeCapBps(uint256)", "uint256"),
    "fee_collector": ("setFeeCollector(address)", "address"),
}


def set_app_config(
    store: Any, app_id: str, chain_id: int, updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply relayer-gated on-chain config setters (V2 dex app)."""
    updates = {k: v for k, v in (updates or {}).items() if v is not None}
    unknown = set(updates) - set(_CONFIG_SETTERS)
    if unknown:
        return {"error": f"Unknown config fields: {sorted(unknown)}"}
    if not updates:
        return {"error": f"Nothing to set; supported: {sorted(_CONFIG_SETTERS)}"}
    relayer = _relayer()
    if relayer is None:
        return {"error": "No EVM relayer configured"}
    app_addr = _deployed_address(store, app_id, chain_id)
    if not app_addr:
        return {"error": f"No deployed contract for {app_id} on chain {chain_id}"}

    txs: dict[str, str] = {}
    for field, value in updates.items():
        sig, abi_type = _CONFIG_SETTERS[field]
        txs[field] = _run_async(relayer.call_contract_function(
            app_addr, chain_id, sig, [abi_type],
            [value if abi_type == "address" else int(value)], gas=100_000,
        ))
    return {"app_id": app_id, "chain_id": chain_id, "txs": txs}


def registry_calldata(store: Any, app_id: str, chain_id: int) -> dict[str, Any]:
    """Prepared AppRegistry calldata for registering the CURRENT deployment.

    - ``register``: ``registerApp(appId, manifestHash, contractAddr)`` —
      callable by any allowlisted developer (incl. the relayer). appId
      defaults to keccak(app_id) unless the contract is already mapped.
    - ``revoke``: ``revokeApp(appId)`` — registry-OWNER only; needed before
      re-pointing an appId that is already registered to an old contract.
      Returned as calldata for external signing (the owner key stays cold).
    """
    from eth_abi import encode as abi_encode
    from eth_hash.auto import keccak

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}
    app_addr = _deployed_address(store, app_id, chain_id)
    if not app_addr:
        return {"error": f"No deployed contract for {app_id} on chain {chain_id}"}

    from minotaur_subnet.api.services.app_admin import _registry_address

    registry = _registry_address(chain_id, _relayer())
    app_id_b32 = keccak(app_id.encode())
    manifest_hash = hashlib.sha256((definition.js_code or "").encode()).digest()

    register_data = (
        keccak(b"registerApp(bytes32,bytes32,address)")[:4]
        + abi_encode(["bytes32", "bytes32", "address"], [app_id_b32, manifest_hash, app_addr])
    )
    revoke_data = keccak(b"revokeApp(bytes32)")[:4] + abi_encode(["bytes32"], [app_id_b32])

    return {
        "app_id": app_id,
        "chain_id": chain_id,
        "registry_address": registry,
        "contract_address": app_addr,
        "registry_app_id": "0x" + app_id_b32.hex(),
        "manifest_hash": "0x" + manifest_hash.hex(),
        "register_calldata": "0x" + register_data.hex(),
        "revoke_calldata": "0x" + revoke_data.hex(),
        "notes": (
            "register: sender must be an allowlisted developer while the "
            "registry is GATED. revoke: registry owner only — required before "
            "re-registering an appId that already points at an old contract."
        ),
    }
