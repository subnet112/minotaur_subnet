"""Tests for app lifecycle services: solidity update, retire, float ops,
config setters, registry calldata — the write side of the app-management
frontend that unblocks in-place V2 redeploys."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from minotaur_subnet.api.services.app_lifecycle import (
    float_deposit,
    float_withdraw,
    registry_calldata,
    retire_deployment,
    set_app_config,
    update_app_solidity,
)
from minotaur_subnet.api.services.app_service import deploy_app_intent
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

APP_ADDR = "0x" + "22" * 20
WETH = "0x4200000000000000000000000000000000000006"


def _store(tmp_path, status=AppStatus.ACTIVE) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}",
        solidity_code="contract DexAggregatorApp {}",
        config=AppIntentConfig(supported_chains=[8453], fee_mode="APP"),
        deployer="0x" + "44" * 20,
    ))
    s.save_deployment(DeploymentResult(
        app_id="app_x", status=status, js_code_hash="x",
        chain_id=8453, contract_address=APP_ADDR,
    ))
    return s


# ── solidity update ──────────────────────────────────────────────────────


def test_update_solidity_replaces_source_and_version(tmp_path):
    s = _store(tmp_path)
    out = update_app_solidity(
        s, "app_x", "contract DexAggregatorAppV2 {}",
        constructor_args=[["uint256", "30"]], contract_version="v2",
    )
    assert out.get("updated") is True
    app = s.get_app("app_x")
    assert "V2" in app.solidity_code
    assert app.contract_version == "v2"
    # JSON round-trip normalizes tuples to lists — compare shape-insensitively.
    assert [list(a) for a in app.constructor_args] == [["uint256", "30"]]


def test_update_solidity_refuses_mid_deploy(tmp_path):
    s = _store(tmp_path, status=AppStatus.DEPLOYING)
    out = update_app_solidity(s, "app_x", "contract X {}")
    assert "in progress" in out["error"]
    assert "V2" not in (s.get_app("app_x").solidity_code or "")


def test_update_solidity_rejects_bad_version(tmp_path):
    out = update_app_solidity(_store(tmp_path), "app_x", "contract X {}",
                              contract_version="v9")
    assert "contract_version" in out["error"]


# ── retire → redeploy guard release ──────────────────────────────────────


def test_retire_then_deploy_guard_released(tmp_path):
    """The whole point: an ACTIVE deployment blocks deploy; RETIRED does not.
    (deploy still errors later on 'no relayer configured' — that error, not
    the already-active guard, proves the guard released.)"""
    s = _store(tmp_path)

    blocked = deploy_app_intent(s, "app_x", chain_id=8453)
    assert "already" in blocked.get("error", "")

    out = retire_deployment(s, "app_x", 8453)
    assert out["status"] == "retired"
    assert s.get_deployment("app_x", chain_id=8453).status == AppStatus.RETIRED

    with patch("minotaur_subnet.api.services._state._deploy_service", None):
        after = deploy_app_intent(s, "app_x", chain_id=8453)
    assert "already" not in after.get("error", "")
    assert "relayer" in after.get("error", "").lower()


def test_retire_refuses_mid_deploy(tmp_path):
    s = _store(tmp_path, status=AppStatus.DEPLOYING)
    assert "error" in retire_deployment(s, "app_x", 8453)


def test_retire_unknown_chain(tmp_path):
    assert "error" in retire_deployment(_store(tmp_path), "app_x", 1)


# ── float ops ────────────────────────────────────────────────────────────


def _deploy_service_with_relayer():
    relayer = MagicMock()
    relayer.call_contract_function = AsyncMock(return_value="0xtx")
    svc = MagicMock()
    svc.relayer = relayer
    return svc, relayer


def test_float_deposit_wraps_then_transfers(tmp_path):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.api.services.app_admin._view_address",
               return_value=WETH), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=MagicMock()):
        out = float_deposit(s, "app_x", 8453, 10**18)

    assert out["txs"] == {"wrap": "0xtx", "transfer": "0xtx"}
    calls = relayer.call_contract_function.await_args_list
    # wrap: WETH.deposit() carrying value
    assert calls[0].args[0] == WETH and calls[0].args[2] == "deposit()"
    assert calls[0].kwargs["tx_value"] == 10**18
    # transfer: WETH.transfer(app, amount)
    assert calls[1].args[2] == "transfer(address,uint256)"
    assert calls[1].args[4] == [APP_ADDR, 10**18]


def test_float_withdraw_calls_withdraw_float_on_app(tmp_path):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    to = "0x" + "77" * 20
    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = float_withdraw(s, "app_x", 8453, to, 5)

    assert out["tx"] == "0xtx"
    call = relayer.call_contract_function.await_args
    assert call.args[0] == APP_ADDR
    assert call.args[2] == "withdrawFloat(address,uint256)"
    assert call.args[4] == [to, 5]


def test_float_ops_require_relayer(tmp_path):
    s = _store(tmp_path)
    with patch("minotaur_subnet.api.services._state._deploy_service", None):
        assert "relayer" in float_deposit(s, "app_x", 8453, 1)["error"].lower()
        assert "relayer" in float_withdraw(
            s, "app_x", 8453, "0x" + "77" * 20, 1)["error"].lower()


# ── config setters ───────────────────────────────────────────────────────


def test_set_app_config_dispatches_setters(tmp_path):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = set_app_config(s, "app_x", 8453, {"fee_bps": 25, "fee_collector": None})

    assert out["txs"] == {"fee_bps": "0xtx"}
    call = relayer.call_contract_function.await_args
    assert call.args[2] == "setFeeBps(uint256)"
    assert call.args[4] == [25]


def test_set_app_config_rejects_unknown_field(tmp_path):
    assert "Unknown" in set_app_config(
        _store(tmp_path), "app_x", 8453, {"nope": 1})["error"]


# ── registry calldata ────────────────────────────────────────────────────


def test_registry_calldata_encodes_register_and_revoke(tmp_path):
    from eth_hash.auto import keccak

    s = _store(tmp_path)
    with patch("minotaur_subnet.api.services._state._deploy_service", None):
        out = registry_calldata(s, "app_x", 8453)

    assert out["registry_app_id"] == "0x" + keccak(b"app_x").hex()
    reg = bytes.fromhex(out["register_calldata"][2:])
    assert reg[:4] == keccak(b"registerApp(bytes32,bytes32,address)")[:4]
    assert reg[4:36] == keccak(b"app_x")  # appId arg
    assert APP_ADDR[2:].lower() in out["register_calldata"]
    rev = bytes.fromhex(out["revoke_calldata"][2:])
    assert rev[:4] == keccak(b"revokeApp(bytes32)")[:4]
