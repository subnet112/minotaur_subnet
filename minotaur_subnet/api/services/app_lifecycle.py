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
    # Who pays the protocol fee: 0=USER (pulled from the user's WETH),
    # 1=APP (paid from the app-held WETH float). setFeeMode is onlyRelayer,
    # so a post-deploy USER->APP flip has to go through this endpoint.
    "fee_mode": ("setFeeMode(uint8)", "uint8"),
    # V2 float-recovery co-signer (withdrawFloat is relayer OR appOwner).
    # setAppOwner is relayer-bootstrappable once, then appOwner-gated.
    "app_owner": ("setAppOwner(address)", "address"),
}


def set_app_config(
    store: Any, app_id: str, chain_id: int, updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply relayer-gated on-chain config setters (V2 dex app)."""
    updates = {k: v for k, v in (updates or {}).items() if v is not None}
    unknown = set(updates) - set(_CONFIG_SETTERS)
    if unknown:
        return {"error": f"Unknown config fields: {sorted(unknown)}"}
    if "fee_mode" in updates and int(updates["fee_mode"]) not in (0, 1):
        return {"error": "fee_mode must be 0 (USER) or 1 (APP)"}
    if "app_owner" in updates and not int(str(updates["app_owner"]), 16):
        return {"error": "app_owner must be a non-zero address"}
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


# ── AppRegistry automation (relayer key IS the registry owner today) ─────


def _registry_ctx(store: Any, app_id: str, chain_id: int):
    """(definition, app_addr, relayer, registry, w3) or an error dict."""
    from minotaur_subnet.api.services.app_admin import _registry_address
    from minotaur_subnet.blockchain.chains import get_web3

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}
    relayer = _relayer()
    if relayer is None:
        return {"error": "No EVM relayer configured"}
    registry = _registry_address(chain_id, relayer)
    if not registry:
        return {"error": f"No AppRegistry configured for chain {chain_id}"}
    return definition, relayer, registry, get_web3(chain_id)


def set_developer_allowed(
    store: Any, app_id: str, chain_id: int, developer: str, allowed: bool = True,
) -> dict[str, Any]:
    """Owner-only AppRegistry.setDeveloperAllowed via the relayer key.

    Only works when the relayer key IS the registry owner — on production
    registries the owner is the operator wallet, so this pre-checks
    ``owner()`` and returns a clean service-level error instead of sending a
    doomed tx (mainapp #20 moves the flow to a direct owner-wallet tx).
    Allowlisting the app's REAL developer is what lets them
    registerApp/updateManifest themselves — the registry-side counterpart
    of the appOwner float rights.
    """
    ctx = _registry_ctx(store, app_id, chain_id)
    if isinstance(ctx, dict):
        return ctx
    _definition, relayer, registry, w3 = ctx
    if not developer or not int(developer, 16):
        return {"error": "developer address is required"}

    from minotaur_subnet.api.services.app_admin import _call, _selector, _view_address

    # Probe FIRST: when the developer is already in the desired state there is
    # no tx to send, so registry ownership is irrelevant. Checking owner before
    # the probe made auto-register abort on production ("allowlist failed:
    # registry owner is …") even though the relayer was ALREADY allowlisted and
    # registerApp needed nothing from the owner.
    probe = _call(w3, registry, _selector("allowedDevelopers(address)")
                  + bytes.fromhex(developer[2:].lower().zfill(64)))
    already = bool(probe) and bool(int.from_bytes(probe[:32], "big"))
    if already == allowed:
        return {"developer": developer, "allowed": allowed, "changed": False}

    owner = _view_address(w3, registry, "owner")
    wallet = relayer._resolve_wallet(chain_id)
    if owner and wallet and owner.lower() != wallet.lower():
        return {
            "error": (
                f"registry owner is {owner}, not the relayer ({wallet}) — "
                "send setDeveloperAllowed from the owner wallet instead"
            ),
            "owner": owner,
        }
    try:
        tx = _run_async(relayer.call_contract_function(
            registry, chain_id, "setDeveloperAllowed(address,bool)",
            ["address", "bool"], [developer, allowed], gas=100_000,
        ))
    except Exception as exc:  # revert / RPC rejection → service-level error, not a 500
        return {"error": f"setDeveloperAllowed failed: {exc}"[:300]}
    return {"developer": developer, "allowed": allowed, "changed": True, "tx": tx}


def bootstrap_app_owner(
    store: Any, app_id: str, chain_id: int, contract_address: str, owner: str,
) -> dict[str, Any]:
    """Best-effort post-deploy ``setAppOwner`` bootstrap (never raises).

    V2 keeps ``appOwner`` out of the constructor, so a fresh deployment has
    owner 0x0 and the developer is custodially dependent on the relayer for
    float recovery (``withdrawFloat`` is relayer OR appOwner). Called from
    the deploy pipeline right after a successful deploy so every app is born
    with its developer-of-record as a self-sovereign owner. Skips when the
    contract has no ``appOwner()`` view (V1) or the owner is already set —
    ``setAppOwner`` is relayer-bootstrappable exactly once, then owner-gated.
    """
    try:
        if not owner or not int(owner, 16):
            return {"owner_set": False, "skipped": "no deployer recorded"}
        relayer = _relayer()
        if relayer is None:
            return {"owner_set": False, "error": "No EVM relayer configured"}

        from minotaur_subnet.api.services.app_admin import _view_address
        from minotaur_subnet.blockchain.chains import get_web3

        w3 = get_web3(chain_id)
        current = _view_address(w3, contract_address, "appOwner")
        if current is None:
            # V1 base — no appOwner() view; nothing to bootstrap.
            return {"owner_set": False, "skipped": "contract has no appOwner()"}
        if int(current, 16) != 0:
            return {"owner_set": True, "already": True, "owner": current}

        tx = _run_async(relayer.call_contract_function(
            contract_address, chain_id, "setAppOwner(address)",
            ["address"], [owner], gas=100_000,
        ))
        return {"owner_set": True, "owner": owner, "tx": tx}
    except Exception as exc:  # never fail the deploy over the owner bootstrap
        logger.warning(
            "appOwner bootstrap failed for %s chain %d: %s", app_id, chain_id, exc,
        )
        return {"owner_set": False, "error": str(exc)[:300]}


def auto_register_deployment(
    store: Any, app_id: str, chain_id: int, contract_address: str,
) -> dict[str, Any]:
    """Best-effort post-deploy AppRegistry registration (never raises).

    Sequence: skip if the contract is already mapped; revokeApp if OUR appId
    (keccak(app_id)) points at an older contract (owner-only — same key);
    self-allowlist the relayer wallet if the registry is GATED; registerApp.
    The registrant (our relayer) becomes developer-of-record — for external
    developers, allowlist them via set_developer_allowed and let them sign
    registry-calldata instead.
    """
    import os as _os

    if _os.environ.get("AUTO_REGISTER_APPS", "1").strip() in ("0", "false", "no"):
        return {"registered": False, "skipped": "AUTO_REGISTER_APPS disabled"}
    try:
        ctx = _registry_ctx(store, app_id, chain_id)
        if isinstance(ctx, dict):
            return {"registered": False, **ctx}
        definition, relayer, registry, w3 = ctx

        from eth_hash.auto import keccak

        from minotaur_subnet.api.services.app_admin import _call, _selector

        addr_word = bytes(12) + bytes.fromhex(contract_address[2:].lower().zfill(40)[-40:])
        mapped = _call(w3, registry, _selector("appByContract(address)") + addr_word)
        if mapped and any(mapped[:32]):
            return {"registered": True, "already": True,
                    "registry_app_id": "0x" + mapped[:32].hex()}

        app_id_b32 = keccak(app_id.encode())
        txs: dict[str, str] = {}
        rec = _call(w3, registry, _selector("apps(bytes32)") + app_id_b32)
        if rec and len(rec) >= 128 and int.from_bytes(rec[96:128], "big") != 0:
            # Our appId points at an OLD contract (redeploy) — owner revoke.
            txs["revoke"] = _run_async(relayer.call_contract_function(
                registry, chain_id, "revokeApp(bytes32)", ["bytes32"],
                [app_id_b32], gas=120_000,
            ))

        mode = _call(w3, registry, _selector("mode()"))
        if mode and int.from_bytes(mode[:32], "big") == 0:  # GATED
            wallet = relayer._resolve_wallet(chain_id)
            allow = set_developer_allowed(store, app_id, chain_id, wallet, True)
            if allow.get("error"):
                return {"registered": False, "error": f"allowlist failed: {allow['error']}", "txs": txs}
            if allow.get("changed"):
                txs["allowlist"] = allow["tx"]

        manifest_hash = hashlib.sha256((definition.js_code or "").encode()).digest()
        txs["register"] = _run_async(relayer.call_contract_function(
            registry, chain_id, "registerApp(bytes32,bytes32,address)",
            ["bytes32", "bytes32", "address"],
            [app_id_b32, manifest_hash, contract_address], gas=200_000,
        ))
        return {"registered": True, "registry_app_id": "0x" + app_id_b32.hex(), "txs": txs}
    except Exception as exc:  # never fail the deploy over registration
        logger.warning("auto-register failed for %s chain %d: %s", app_id, chain_id, exc)
        return {"registered": False, "error": str(exc)[:300]}
