"""Tests for the app-management admin-state aggregation (app_admin.py).

The endpoint backs the operator frontend: store code state + per-chain
on-chain config, fee-settlement balances (V2 app-held float, V1 paymaster
balance/allowance), and AppRegistry status. Chain reads must degrade to
nulls + per-chain errors, never an exception.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from minotaur_subnet.api.services.app_admin import get_app_admin_state
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

APP = "0x" + "22" * 20
DEPLOYER = "0x" + "44" * 20


def _store_with_app(tmp_path, contract_version="v2") -> AppIntentStore:
    store = AppIntentStore(store_path=tmp_path / "store.db")
    store.save_app(AppIntentDefinition(
        app_id="app_x",
        name="dex",
        version="1.0.0",
        intent_type="swap",
        js_code="function score() { return 1; }",
        solidity_code="contract DexAggregatorAppV2 {}",
        config=AppIntentConfig(supported_chains=[8453], fee_mode="APP"),
        deployer=DEPLOYER,
        contract_version=contract_version,
    ))
    store.save_deployment(DeploymentResult(
        app_id="app_x",
        status=AppStatus.ACTIVE,
        js_code_hash="deadbeef",
        chain_id=8453,
        contract_address=APP,
    ))
    return store


def test_not_found():
    store = MagicMock()
    store.get_app.return_value = None
    assert "error" in get_app_admin_state(store, "nope")


def test_store_side_state_without_chain(tmp_path):
    """RPC unreachable → full store state, chain_state carries the error."""
    store = _store_with_app(tmp_path)
    with patch(
        "minotaur_subnet.blockchain.chains.get_web3",
        side_effect=RuntimeError("no rpc"),
    ):
        out = get_app_admin_state(store, "app_x")

    assert out["app_id"] == "app_x"
    assert out["contract_version"] == "v2"
    assert out["js_code_sha256"] == hashlib.sha256(
        b"function score() { return 1; }").hexdigest()
    assert out["solidity_code"].startswith("contract DexAggregatorAppV2")
    dep = out["deployments"][8453]
    assert dep["contract_address"] == APP
    assert dep["status"] == "active"
    assert dep["chain_state"]["errors"]  # degraded, not raised


def test_contract_version_defaults_to_v1_for_legacy_records(tmp_path):
    """Records that predate the field (empty string) present as v1."""
    store = _store_with_app(tmp_path, contract_version="")
    with patch(
        "minotaur_subnet.blockchain.chains.get_web3",
        side_effect=RuntimeError("no rpc"),
    ):
        out = get_app_admin_state(store, "app_x")
    assert out["contract_version"] == "v1"


def _w3_with_views(views: dict[bytes, bytes], balances: dict[str, int]):
    """Fake w3 whose eth.call answers by 4-byte selector prefix."""
    w3 = MagicMock()

    def call(tx):
        data = bytes.fromhex(tx["data"][2:])
        key = data[:4]
        if key in views:
            return views[key]
        raise Exception("execution reverted")

    w3.eth.call.side_effect = call
    w3.eth.get_balance.side_effect = lambda addr: balances.get(addr, 0)
    return w3


def _sel(sig: str) -> bytes:
    from eth_hash.auto import keccak
    return keccak(sig.encode())[:4]


def _addr_word(addr: str) -> bytes:
    return bytes(12) + bytes.fromhex(addr[2:])


def test_chain_state_reads_config_balances_and_registry(tmp_path):
    store = _store_with_app(tmp_path)
    weth = "0x4200000000000000000000000000000000000006"
    relayer_addr = "0x" + "33" * 20
    registry = "0x" + "55" * 20
    app_id_b32 = b"\x07" * 32

    views = {
        _sel("relayer()"): _addr_word(relayer_addr),
        _sel("platformFeeCollector()"): _addr_word("0x" + "66" * 20),
        _sel("appPaymaster()"): bytes(32),  # zero — V2 deployed via our deployer
        _sel("wrappedNativeToken()"): _addr_word(weth),
        _sel("feeMode()"): (1).to_bytes(32, "big"),
        _sel("minPlatformFeeWei()"): (0).to_bytes(32, "big"),
        _sel("maxPlatformFeeWei()"): (10**18).to_bytes(32, "big"),
        _sel("scoreThreshold()"): (5000).to_bytes(32, "big"),
        _sel("feeBps()"): (30).to_bytes(32, "big"),
        _sel("volumeCapBps()"): (98).to_bytes(32, "big"),
        _sel("balanceOf(address)"): (7 * 10**18).to_bytes(32, "big"),
        # Registry views:
        _sel("mode()"): (0).to_bytes(32, "big"),
        _sel("appByContract(address)"): app_id_b32,
        _sel("apps(bytes32)"): (
            _addr_word(DEPLOYER) + b"\xaa" * 32 + _addr_word(APP)
            + (1234).to_bytes(32, "big")
        ),
        _sel("allowedDevelopers(address)"): (1).to_bytes(32, "big"),
    }
    w3 = _w3_with_views(views, {relayer_addr: 5 * 10**17})

    relayer_cfg = MagicMock()
    relayer_cfg.app_registry_address = registry
    deploy_svc = MagicMock()
    deploy_svc.relayer.chains = {8453: relayer_cfg}

    with patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3), \
         patch("minotaur_subnet.api.services._state._deploy_service", deploy_svc):
        out = get_app_admin_state(store, "app_x")

    cs = out["deployments"][8453]["chain_state"]
    cfg = cs["app_config"]
    assert cfg["feeModeName"] == "APP"
    assert cfg["wrappedNativeToken"].lower() == weth.lower()
    assert cfg["appPaymaster"] is None or int(cfg["appPaymaster"], 16) == 0
    assert cfg["feeBps"] == 30

    # V2 float visible; paymaster balances absent (paymaster is zero).
    assert cs["balances"]["app_float_wei"] == 7 * 10**18
    assert "paymaster_balance_wei" not in cs["balances"]
    assert cs["balances"]["relayer_gas_wei"] == 5 * 10**17

    reg = cs["app_registry"]
    assert reg["mode"] == "GATED"
    assert reg["registered"] is True
    assert reg["registry_app_id"] == "0x" + app_id_b32.hex()
    assert reg["record"]["developer"].lower() == DEPLOYER.lower()
    assert reg["record"]["registered_at"] == 1234
    assert reg["deployer_allowlisted"] is True


def test_unregistered_contract_reports_registered_false(tmp_path):
    store = _store_with_app(tmp_path)
    views = {
        _sel("wrappedNativeToken()"): bytes(32),
        _sel("mode()"): (1).to_bytes(32, "big"),
        _sel("appByContract(address)"): bytes(32),  # zero appId
    }
    w3 = _w3_with_views(views, {})
    relayer_cfg = MagicMock()
    relayer_cfg.app_registry_address = "0x" + "55" * 20
    deploy_svc = MagicMock()
    deploy_svc.relayer.chains = {8453: relayer_cfg}

    with patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3), \
         patch("minotaur_subnet.api.services._state._deploy_service", deploy_svc):
        out = get_app_admin_state(store, "app_x")

    reg = out["deployments"][8453]["chain_state"]["app_registry"]
    assert reg["registered"] is False
    assert reg["mode"] == "OPEN"


def test_create_app_persists_contract_version(tmp_path):
    from minotaur_subnet.api.services.app_service import create_app_intent

    store = AppIntentStore(store_path=tmp_path / "s.db")
    out = create_app_intent(
        store, name="dex", description="", supported_chains=[8453],
        js_code=(
            "const manifest = { intent_functions: [{ name: 'swap', params: [] }] };\n"
            "function score() { return 1; }\n"
            "module.exports = { score, manifest };\n"
        ),
        solidity_code="contract X {}",
        contract_version="V2",
    )
    assert "error" not in out, out
    assert store.get_app(out["app_id"]).contract_version == "v2"


def test_create_app_rejects_bad_contract_version(tmp_path):
    from minotaur_subnet.api.services.app_service import create_app_intent

    store = AppIntentStore(store_path=tmp_path / "s.db")
    out = create_app_intent(
        store, name="dex", description="", supported_chains=[8453],
        js_code=(
            "const manifest = { intent_functions: [{ name: 'swap', params: [] }] };\n"
            "function score() { return 1; }\n"
            "module.exports = { score, manifest };\n"
        ),
        solidity_code="contract X {}",
        contract_version="v3",
    )
    assert "contract_version" in out.get("error", "")
