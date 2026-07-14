"""App-management admin state aggregation.

Backs the app-management frontend: one call returns everything an operator
needs to see about an app — stored code (JS + Solidity, hashed), per-chain
deployments, live on-chain app configuration, fee-settlement balances (V2
app-held float AND V1 paymaster), and AppRegistry registration status.

Every chain read is best-effort: a dead RPC or missing view degrades to
``None`` plus an entry in that chain's ``errors`` list — the endpoint never
5xxes because one chain is unreachable, so the frontend can always render
the store-side state.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict
from typing import Any

logger = logging.getLogger(__name__)

# No-arg views on AppIntentBase / AppIntentBaseV2 (both generations expose all
# of these — V2 keeps appPaymaster() as an informational pointer).
_APP_ADDRESS_VIEWS = (
    "relayer",
    "platformFeeCollector",
    "appPaymaster",
    "wrappedNativeToken",
    # V2-only (revert -> None on V1): float-recovery co-signer, 0x0 = unset.
    "appOwner",
)
_APP_UINT_VIEWS = (
    "feeMode",            # enum FeeMode (0=USER, 1=APP)
    "minPlatformFeeWei",
    "maxPlatformFeeWei",
    "scoreThreshold",
    # Dex-app specific (revert -> None on other apps):
    "feeBps",
    "volumeCapBps",
)

_FEE_MODE_NAMES = {0: "USER", 1: "APP"}
_REGISTRY_MODE_NAMES = {0: "GATED", 1: "OPEN"}


def _selector(sig: str) -> bytes:
    from eth_hash.auto import keccak

    return keccak(sig.encode())[:4]


def _call(w3: Any, to: str, data: bytes) -> bytes | None:
    """eth_call returning raw bytes, None on revert/absence."""
    try:
        out = w3.eth.call({"to": to, "data": "0x" + data.hex()})
        return bytes(out) if out else None
    except Exception:
        return None


def _view_address(w3: Any, target: str, name: str) -> str | None:
    out = _call(w3, target, _selector(f"{name}()"))
    if out is None or len(out) < 32:
        return None
    from web3 import Web3

    return Web3.to_checksum_address("0x" + out[-20:].hex())


def _view_uint(w3: Any, target: str, name: str) -> int | None:
    out = _call(w3, target, _selector(f"{name}()"))
    if out is None or len(out) < 32:
        return None
    return int.from_bytes(out[:32], "big")


def _erc20_balance(w3: Any, token: str, holder: str) -> int | None:
    data = _selector("balanceOf(address)") + bytes.fromhex(holder[2:].lower().zfill(64))
    out = _call(w3, token, data)
    return int.from_bytes(out[:32], "big") if out else None


def _erc20_allowance(w3: Any, token: str, owner: str, spender: str) -> int | None:
    data = (
        _selector("allowance(address,address)")
        + bytes.fromhex(owner[2:].lower().zfill(64))
        + bytes.fromhex(spender[2:].lower().zfill(64))
    )
    out = _call(w3, token, data)
    return int.from_bytes(out[:32], "big") if out else None


def _sha256(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode()).hexdigest()


def _registry_address(chain_id: int, relayer: Any) -> str | None:
    """Resolve the AppRegistry address the same way the deployer does
    (chain config first, APP_REGISTRY_{chain} env fallback)."""
    cfg = None
    if relayer is not None:
        cfg = getattr(relayer, "chains", {}).get(chain_id)
    addr = (
        (getattr(cfg, "app_registry_address", "") or "")
        or os.environ.get(f"APP_REGISTRY_{chain_id}", "")
    ).strip()
    return addr or None


def _registry_state(
    w3: Any, registry: str, contract_addr: str, deployer: str,
) -> dict[str, Any]:
    """AppRegistry status for one deployed contract — all public views."""
    from web3 import Web3

    state: dict[str, Any] = {"registry_address": registry}

    mode = _view_uint(w3, registry, "mode")
    state["mode"] = _REGISTRY_MODE_NAMES.get(mode, mode)

    # appByContract(address) -> bytes32 appId (zero = not registered)
    data = _selector("appByContract(address)") + bytes.fromhex(
        contract_addr[2:].lower().zfill(64)
    )
    out = _call(w3, registry, data)
    app_id_b32 = out[:32] if out else None
    registered = bool(app_id_b32) and any(app_id_b32)
    state["registered"] = registered
    state["registry_app_id"] = "0x" + app_id_b32.hex() if registered else None

    if registered:
        # apps(bytes32) -> (developer, manifestHash, contractAddr, registeredAt)
        rec = _call(w3, registry, _selector("apps(bytes32)") + app_id_b32)
        if rec and len(rec) >= 128:
            state["record"] = {
                "developer": Web3.to_checksum_address("0x" + rec[12:32].hex()),
                "manifest_hash": "0x" + rec[32:64].hex(),
                "contract_addr": Web3.to_checksum_address("0x" + rec[76:96].hex()),
                "registered_at": int.from_bytes(rec[96:128], "big"),
            }

    if deployer:
        data = _selector("allowedDevelopers(address)") + bytes.fromhex(
            deployer[2:].lower().zfill(64)
        )
        out = _call(w3, registry, data)
        state["deployer_allowlisted"] = bool(out) and bool(int.from_bytes(out[:32], "big"))

    return state


def _chain_state(
    w3: Any, contract_addr: str, deployer: str, chain_id: int, relayer: Any,
) -> dict[str, Any]:
    """Live on-chain state for one deployment: app config, balances, registry."""
    state: dict[str, Any] = {"errors": []}

    views: dict[str, Any] = {}
    for name in _APP_ADDRESS_VIEWS:
        views[name] = _view_address(w3, contract_addr, name)
    for name in _APP_UINT_VIEWS:
        views[name] = _view_uint(w3, contract_addr, name)
    if views.get("feeMode") is not None:
        views["feeModeName"] = _FEE_MODE_NAMES.get(views["feeMode"])
    state["app_config"] = views

    weth = views.get("wrappedNativeToken")
    paymaster = views.get("appPaymaster")
    balances: dict[str, Any] = {}
    if weth and int(weth, 16):
        # V2 fee model: the float held by the app contract itself.
        balances["app_float_wei"] = _erc20_balance(w3, weth, contract_addr)
        # V1 fee model: paymaster balance + allowance to the app.
        if paymaster and int(paymaster, 16):
            balances["paymaster_balance_wei"] = _erc20_balance(w3, weth, paymaster)
            balances["paymaster_allowance_wei"] = _erc20_allowance(
                w3, weth, paymaster, contract_addr,
            )
    relayer_addr = views.get("relayer")
    if relayer_addr:
        try:
            balances["relayer_gas_wei"] = int(w3.eth.get_balance(relayer_addr))
        except Exception as exc:
            state["errors"].append(f"relayer gas balance: {exc}")
    state["balances"] = balances

    registry = _registry_address(chain_id, relayer)
    if registry:
        try:
            state["app_registry"] = _registry_state(
                w3, registry, contract_addr, deployer,
            )
        except Exception as exc:
            state["errors"].append(f"app_registry: {exc}")
            state["app_registry"] = {"registry_address": registry}
    else:
        state["app_registry"] = None  # no registry configured for this chain

    return state


def get_app_admin_state(store: Any, app_id: str) -> dict[str, Any]:
    """Aggregate store + per-chain on-chain state for the management frontend."""
    from ._state import _deploy_service

    definition = store.get_app(app_id)
    if definition is None:
        return {"error": f"App not found: {app_id}"}

    result: dict[str, Any] = {
        "app_id": app_id,
        "name": definition.name,
        "version": definition.version,
        "description": definition.description,
        "deployer": definition.deployer,
        "fee_mode": definition.config.fee_mode,
        "supported_chains": list(definition.config.supported_chains or []),
        # Empty contract_version = record predates the field = v1 base.
        "contract_version": definition.contract_version or "v1",
        # Moderation gate: "" (legacy) presents as "approved". See
        # app_registration.py. registration_meta carries note/reviewer if set.
        "registration_status": definition.registration_status or "approved",
        "registration_meta": (definition.policy_metadata or {}).get("registration"),
        "constructor_args": definition.constructor_args,
        "js_code": definition.js_code,
        "js_code_sha256": _sha256(definition.js_code),
        "solidity_code": definition.solidity_code,
        "solidity_code_sha256": _sha256(definition.solidity_code),
    }

    deployments: dict[int, dict[str, Any]] = {}
    relayer = getattr(_deploy_service, "relayer", None) if _deploy_service else None
    for chain_id, dep in (store.get_deployments(app_id) or {}).items():
        entry: dict[str, Any] = asdict(dep)
        entry["status"] = getattr(dep.status, "value", dep.status)
        addr = getattr(dep, "contract_address", None)
        if addr:
            try:
                from minotaur_subnet.blockchain.chains import get_web3

                w3 = get_web3(chain_id)
                entry["chain_state"] = _chain_state(
                    w3, addr, definition.deployer, chain_id, relayer,
                )
            except Exception as exc:
                logger.warning(
                    "admin-state chain read failed (app=%s chain=%d): %s",
                    app_id, chain_id, exc,
                )
                entry["chain_state"] = {"errors": [f"chain unreachable: {exc}"]}
        deployments[chain_id] = entry
    result["deployments"] = deployments

    return result
