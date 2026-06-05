"""Resolve an App's contract address from the on-chain AppRegistry.

The platform never hardcodes app addresses: apps deployed through the normal path
(validator API) register on-chain in the AppRegistry, and every consumer resolves
``app_id -> contractAddr`` from it (production: ``benchmark_worker`` does
``app_store.get_deployment(app_id).contract_address``; the registry is the
on-chain source of truth — ``consensus/app_registry_cache.py``). The lab mirrors
that: give it the registry address (env ``APP_REGISTRY_{chain_id}`` /
``--app-registry``, same as production) and resolve the contract from chain
instead of baking in a deployment address.

Registry interface (``minotaur_contracts/src/AppRegistry.sol``):
  getApp(bytes32 appId) -> (developer, manifestHash, contractAddr, registeredAt)
  appByContract(address) -> bytes32 appId          (reverse / registered check)
  event AppRegistered(bytes32 appId, address developer, bytes32 manifestHash, address contractAddr)
There is no view-enumeration, so listing apps scans AppRegistered (same approach
as the IntentExecuted order indexer).
"""
from __future__ import annotations

from typing import Any

APP_REGISTRY_ABI = [
    {"type": "function", "name": "getApp", "stateMutability": "view",
     "inputs": [{"name": "appId", "type": "bytes32"}],
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "developer", "type": "address"},
         {"name": "manifestHash", "type": "bytes32"},
         {"name": "contractAddr", "type": "address"},
         {"name": "registeredAt", "type": "uint64"},
     ]}]},
    {"type": "function", "name": "appByContract", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "bytes32"}]},
    {"type": "event", "name": "AppRegistered", "anonymous": False, "inputs": [
        {"name": "appId", "type": "bytes32", "indexed": True},
        {"name": "developer", "type": "address", "indexed": True},
        {"name": "manifestHash", "type": "bytes32", "indexed": False},
        {"name": "contractAddr", "type": "address", "indexed": False},
    ]},
]

_ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def to_bytes32(app_id: str | bytes) -> bytes:
    """Accept a 0x-hex appId (the on-chain bytes32) or raw bytes; right-pad to 32."""
    if isinstance(app_id, (bytes, bytearray)):
        b = bytes(app_id)
    elif isinstance(app_id, str) and app_id.startswith("0x"):
        b = bytes.fromhex(app_id[2:])
    else:
        raise ValueError(f"app_id must be a 0x-hex bytes32 (the on-chain appId), got {app_id!r}")
    if len(b) > 32:
        raise ValueError("app_id longer than 32 bytes")
    return b.ljust(32, b"\x00")   # bytes32 is right-padded (solidity convention)


def resolve_contract(w3, registry_address: str, app_id: str | bytes) -> str:
    """app_id -> deployed contract address, read from the registry. Raises if the
    app isn't registered (contractAddr == 0)."""
    reg = w3.eth.contract(address=w3.to_checksum_address(registry_address), abi=APP_REGISTRY_ABI)
    rec = reg.functions.getApp(to_bytes32(app_id)).call()
    contract = rec[2] if not isinstance(rec, dict) else rec["contractAddr"]
    if int(contract, 16) == 0:
        raise ValueError(f"app {app_id} not registered in AppRegistry {registry_address}")
    return w3.to_checksum_address(contract)


def is_registered(w3, registry_address: str, contract_address: str) -> bool:
    """Reverse check: is this contract a currently-registered App?"""
    reg = w3.eth.contract(address=w3.to_checksum_address(registry_address), abi=APP_REGISTRY_ABI)
    app_id = reg.functions.appByContract(w3.to_checksum_address(contract_address)).call()
    return any(b != 0 for b in app_id)


def list_registered_apps(w3, registry_address: str, from_block: int = 0,
                         to_block: int | str = "latest", chunk: int = 2000) -> list[dict]:
    """Enumerate apps by scanning AppRegistered (no view-enumeration on the contract).
    Returns [{app_id, contract, developer, manifest_hash}], paged to respect RPC caps."""
    reg = w3.eth.contract(address=w3.to_checksum_address(registry_address), abi=APP_REGISTRY_ABI)
    head = w3.eth.block_number if to_block == "latest" else int(to_block)
    out: dict[str, dict[str, Any]] = {}
    start = int(from_block)
    while start <= head:
        end = min(start + chunk - 1, head)
        try:
            evts = reg.events.AppRegistered().get_logs(from_block=start, to_block=end)
        except Exception:
            if chunk > 1:
                chunk //= 2
                continue
            raise
        for e in evts:
            a = e["args"]
            out["0x" + a["appId"].hex()] = {
                "app_id": "0x" + a["appId"].hex(),
                "contract": w3.to_checksum_address(a["contractAddr"]),
                "developer": w3.to_checksum_address(a["developer"]),
                "manifest_hash": "0x" + a["manifestHash"].hex(),
            }
        start = end + 1
    return list(out.values())
