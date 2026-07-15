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


def test_set_app_config_flips_fee_mode_to_app(tmp_path):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = set_app_config(s, "app_x", 8453, {"fee_mode": 1})

    assert out["txs"] == {"fee_mode": "0xtx"}
    call = relayer.call_contract_function.await_args
    assert call.args[2] == "setFeeMode(uint8)"
    assert call.args[4] == [1]


def test_set_app_config_rejects_invalid_fee_mode(tmp_path):
    assert "fee_mode" in set_app_config(
        _store(tmp_path), "app_x", 8453, {"fee_mode": 2})["error"]


def test_set_app_config_sets_app_owner(tmp_path):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    owner = "0x" + "99" * 20
    with patch("minotaur_subnet.api.services._state._deploy_service", svc):
        out = set_app_config(s, "app_x", 8453, {"app_owner": owner})

    assert out["txs"] == {"app_owner": "0xtx"}
    call = relayer.call_contract_function.await_args
    assert call.args[2] == "setAppOwner(address)"
    assert call.args[4] == [owner]


def test_set_app_config_rejects_zero_app_owner(tmp_path):
    assert "app_owner" in set_app_config(
        _store(tmp_path), "app_x", 8453, {"app_owner": "0x" + "00" * 20})["error"]


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


# ── registry automation ──────────────────────────────────────────────────


def _registry_env(tmp_path, views):
    """store + deploy_service + fake w3 answering by selector prefix."""
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    relayer._resolve_wallet = MagicMock(return_value="0x" + "d4" * 20)
    cfg = MagicMock(); cfg.app_registry_address = "0x" + "55" * 20
    relayer.chains = {8453: cfg}
    w3 = MagicMock()
    def call(tx):
        key = bytes.fromhex(tx["data"][2:])[:4]
        if key in views:
            return views[key]
        raise Exception("execution reverted")
    w3.eth.call.side_effect = call
    return s, svc, relayer, w3


def _k(sig):
    from eth_hash.auto import keccak
    return keccak(sig.encode())[:4]


def test_auto_register_fresh_contract_allowlists_and_registers(tmp_path):
    views = {
        _k("appByContract(address)"): bytes(32),          # not mapped
        _k("apps(bytes32)"): bytes(128),                  # appId free
        _k("mode()"): (0).to_bytes(32, "big"),            # GATED
        _k("allowedDevelopers(address)"): bytes(32),      # relayer NOT allowed yet
    }
    from minotaur_subnet.api.services.app_lifecycle import auto_register_deployment
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = auto_register_deployment(s, "app_x", 8453, APP_ADDR)

    assert out["registered"] is True
    sigs = [c.args[2] for c in relayer.call_contract_function.await_args_list]
    assert sigs == ["setDeveloperAllowed(address,bool)",
                    "registerApp(bytes32,bytes32,address)"]


def test_auto_register_redeploy_revokes_old_mapping(tmp_path):
    from eth_hash.auto import keccak
    old_rec = bytes(32) + b"\xaa" * 32 + bytes(12) + b"\x99" * 20 + (7).to_bytes(32, "big")
    views = {
        _k("appByContract(address)"): bytes(32),
        _k("apps(bytes32)"): old_rec,                     # appId → OLD contract
        _k("mode()"): (1).to_bytes(32, "big"),            # OPEN → no allowlist step
    }
    from minotaur_subnet.api.services.app_lifecycle import auto_register_deployment
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = auto_register_deployment(s, "app_x", 8453, APP_ADDR)

    assert out["registered"] is True
    sigs = [c.args[2] for c in relayer.call_contract_function.await_args_list]
    assert sigs == ["revokeApp(bytes32)", "registerApp(bytes32,bytes32,address)"]
    assert relayer.call_contract_function.await_args_list[0].args[4] == [keccak(b"app_x")]


def test_auto_register_skips_already_mapped(tmp_path):
    views = {_k("appByContract(address)"): b"\x07" * 32}
    from minotaur_subnet.api.services.app_lifecycle import auto_register_deployment
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = auto_register_deployment(s, "app_x", 8453, APP_ADDR)
    assert out == {"registered": True, "already": True,
                   "registry_app_id": "0x" + ("07" * 32)}
    relayer.call_contract_function.assert_not_awaited()


def test_auto_register_env_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_REGISTER_APPS", "0")
    from minotaur_subnet.api.services.app_lifecycle import auto_register_deployment
    out = auto_register_deployment(_store(tmp_path), "app_x", 8453, APP_ADDR)
    assert out["registered"] is False and "disabled" in out["skipped"]


def test_auto_register_never_raises(tmp_path):
    from minotaur_subnet.api.services.app_lifecycle import auto_register_deployment
    with patch("minotaur_subnet.api.services._state._deploy_service", None):
        out = auto_register_deployment(_store(tmp_path), "app_x", 8453, APP_ADDR)
    assert out["registered"] is False


def test_set_developer_allowed_noop_when_already_set(tmp_path):
    views = {_k("allowedDevelopers(address)"): (1).to_bytes(32, "big")}
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert out == {"developer": "0x" + "63" * 20, "allowed": True, "changed": False}
    relayer.call_contract_function.assert_not_awaited()


def test_set_developer_allowed_sends_owner_tx(tmp_path):
    views = {_k("allowedDevelopers(address)"): bytes(32)}
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert out["changed"] is True and out["tx"] == "0xtx"
    call = relayer.call_contract_function.await_args
    assert call.args[2] == "setDeveloperAllowed(address,bool)"
    assert call.args[4] == ["0x" + "63" * 20, True]


def test_set_developer_allowed_owner_mismatch_is_clean_error(tmp_path):
    # owner() = operator wallet, relayer wallet = 0xd4… → no doomed tx sent.
    operator = bytes(12) + b"\xab" * 20
    views = {
        _k("owner()"): operator,
        _k("allowedDevelopers(address)"): bytes(32),
    }
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert "registry owner is" in out["error"]
    assert out["owner"].lower() == "0x" + "ab" * 20
    relayer.call_contract_function.assert_not_awaited()


def test_set_developer_allowed_owner_match_sends_tx(tmp_path):
    # owner() == relayer wallet (0xd4…) → the pre-check lets the tx through.
    views = {
        _k("owner()"): bytes(12) + b"\xd4" * 20,
        _k("allowedDevelopers(address)"): bytes(32),
    }
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert out["changed"] is True and out["tx"] == "0xtx"


def test_set_developer_allowed_revert_is_clean_error(tmp_path):
    # Tx raising (revert / RPC rejection) → {"error": …}, never a 500.
    views = {_k("allowedDevelopers(address)"): bytes(32)}
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    relayer.call_contract_function = AsyncMock(
        side_effect=RuntimeError("setDeveloperAllowed(address,bool) reverted: tx=0xdead"))
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert out["error"].startswith("setDeveloperAllowed failed:")
    assert "reverted" in out["error"]


# ── appOwner bootstrap at deploy ─────────────────────────────────────────

DEV = "0x" + "44" * 20


def _owner_env(tmp_path, views):
    s = _store(tmp_path)
    svc, relayer = _deploy_service_with_relayer()
    w3 = MagicMock()
    def call(tx):
        key = bytes.fromhex(tx["data"][2:])[:4]
        if key in views:
            return views[key]
        raise Exception("execution reverted")
    w3.eth.call.side_effect = call
    return s, svc, relayer, w3


def test_bootstrap_app_owner_sets_owner_on_fresh_v2(tmp_path):
    views = {_k("appOwner()"): bytes(32)}  # deployed, owner unset (0x0)
    from minotaur_subnet.api.services.app_lifecycle import bootstrap_app_owner
    s, svc, relayer, w3 = _owner_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = bootstrap_app_owner(s, "app_x", 8453, APP_ADDR, DEV)
    assert out["owner_set"] is True and out["tx"] == "0xtx"
    call = relayer.call_contract_function.await_args
    assert call.args[2] == "setAppOwner(address)"
    assert call.args[4] == [DEV]


def test_bootstrap_app_owner_skips_when_already_owned(tmp_path):
    views = {_k("appOwner()"): bytes(12) + b"\x99" * 20}
    from minotaur_subnet.api.services.app_lifecycle import bootstrap_app_owner
    s, svc, relayer, w3 = _owner_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = bootstrap_app_owner(s, "app_x", 8453, APP_ADDR, DEV)
    assert out.get("already") is True and out["owner_set"] is True
    relayer.call_contract_function.assert_not_awaited()


def test_bootstrap_app_owner_skips_v1_contract_without_view(tmp_path):
    # V1 base has no appOwner() — the probe reverts → skip, no tx.
    from minotaur_subnet.api.services.app_lifecycle import bootstrap_app_owner
    s, svc, relayer, w3 = _owner_env(tmp_path, views={})
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = bootstrap_app_owner(s, "app_x", 8453, APP_ADDR, DEV)
    assert out["owner_set"] is False and "appOwner" in out["skipped"]
    relayer.call_contract_function.assert_not_awaited()


def test_bootstrap_app_owner_skips_without_deployer(tmp_path):
    from minotaur_subnet.api.services.app_lifecycle import bootstrap_app_owner
    out = bootstrap_app_owner(_store(tmp_path), "app_x", 8453, APP_ADDR, "")
    assert out["owner_set"] is False and "deployer" in out["skipped"]


def test_bootstrap_app_owner_never_raises(tmp_path):
    from minotaur_subnet.api.services.app_lifecycle import bootstrap_app_owner
    with patch("minotaur_subnet.api.services._state._deploy_service", None):
        out = bootstrap_app_owner(_store(tmp_path), "app_x", 8453, APP_ADDR, DEV)
    assert out["owner_set"] is False


def test_set_developer_allowed_already_set_skips_owner_check(tmp_path):
    # Already in the desired state → changed:False, and the owner mismatch is
    # IRRELEVANT (no tx needed). The pre-#782 ordering returned "registry
    # owner is …" here, which aborted auto-register on production even though
    # the relayer was already allowlisted and registerApp needed no owner.
    operator = bytes(12) + b"\xab" * 20  # owner != relayer wallet
    views = {
        _k("owner()"): operator,
        _k("allowedDevelopers(address)"): (1).to_bytes(32, "big"),
    }
    from minotaur_subnet.api.services.app_lifecycle import set_developer_allowed
    s, svc, relayer, w3 = _registry_env(tmp_path, views)
    with patch("minotaur_subnet.api.services._state._deploy_service", svc), \
         patch("minotaur_subnet.blockchain.chains.get_web3", return_value=w3):
        out = set_developer_allowed(s, "app_x", 8453, "0x" + "63" * 20, True)
    assert out == {"developer": "0x" + "63" * 20, "allowed": True, "changed": False}
    relayer.call_contract_function.assert_not_awaited()
