"""Tests for ``POST /v1/contract-call`` — the allowlisted admin-call endpoint
that backs ``HttpRelayer.call_contract_function`` on the split api/relayer
topology.

Before this endpoint existed, every app-lifecycle admin action (float
deposit/withdraw, config setters, AppRegistry automation) raised
``AttributeError: 'HttpRelayer' object has no attribute
'call_contract_function'`` in production, because the generic call only
existed on the in-process ``EvmRelayer``.

Auth mirrors ``POST /deploy`` (wrapper-sig + on-chain ValidatorRegistry
membership), PLUS a hard function allowlist and gas/value caps — this is an
admin surface, not an arbitrary-tx relay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from eth_account import Account

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.leader_wrapper import (
    compute_contract_call_hash,
    sign_wrapper,
)

TARGET = "0x" + "22" * 20


# ── compute_contract_call_hash unit tests ──────────────────────────────────


def test_call_hash_is_deterministic():
    a = compute_contract_call_hash(1, TARGET, "setFeeMode(uint8)", ["uint8"], ["1"], 0, 100_000)
    b = compute_contract_call_hash(1, TARGET, "setFeeMode(uint8)", ["uint8"], ["1"], 0, 100_000)
    assert a == b and a.startswith("0x") and len(a) == 66


@pytest.mark.parametrize("mutation", [
    {"chain_id": 8453},
    {"target": "0x" + "33" * 20},
    {"fn_signature": "setFeeBps(uint256)"},
    {"abi_types": ["uint256"]},
    {"values": ["2"]},
    {"tx_value": 1},
    {"gas": 100_001},
])
def test_call_hash_binds_every_param(mutation):
    base = dict(chain_id=1, target=TARGET, fn_signature="setFeeMode(uint8)",
                abi_types=["uint8"], values=["1"], tx_value=0, gas=100_000)
    changed = {**base, **mutation}
    assert (compute_contract_call_hash(**base)
            != compute_contract_call_hash(**changed))


def test_call_hash_target_case_insensitive():
    a = compute_contract_call_hash(1, TARGET, "deposit()", [], [], 5, 100_000)
    b = compute_contract_call_hash(1, TARGET.upper().replace("0X", "0x"), "deposit()", [], [], 5, 100_000)
    assert a == b


# ── handle_contract_call integration tests ─────────────────────────────────


@pytest.fixture
def fresh_signer():
    acct = Account.create()
    return acct.key.hex(), acct.address


def _service(call_result: str = "0xTxHash"):
    from minotaur_subnet.relayer import main as relayer_main
    from minotaur_subnet.relayer.safeguards import Safeguards

    service = MagicMock(spec=relayer_main.RelayerService)
    service.chains = {
        8453: MagicMock(
            chain_id=8453,
            rpc_url="http://localhost:1",
            validator_registry_address="0xBaseRegistry",
        ),
    }
    service.safeguards = Safeguards()
    service.relayer = MagicMock()
    service.relayer.call_contract_function = AsyncMock(return_value=call_result)
    service.CONTRACT_CALL_ALLOWED_SIGS = relayer_main.RelayerService.CONTRACT_CALL_ALLOWED_SIGS
    service.CONTRACT_CALL_MAX_GAS = relayer_main.RelayerService.CONTRACT_CALL_MAX_GAS
    service.CONTRACT_CALL_MAX_VALUE_WEI = relayer_main.RelayerService.CONTRACT_CALL_MAX_VALUE_WEI
    service.handle_contract_call = (
        relayer_main.RelayerService.handle_contract_call.__get__(service)
    )
    return service


def _signed_body(priv: str, *, chain_id=8453, target=TARGET,
                 fn_signature="setFeeMode(uint8)", abi_types=("uint8",),
                 values=("1",), tx_value=0, gas=100_000, plan_hash=None):
    abi_types = list(abi_types)
    values = [str(v) for v in values]
    if plan_hash is None:
        plan_hash = compute_contract_call_hash(
            chain_id, target, fn_signature, abi_types, values, tx_value, gas,
        )
    wrapper, sig = sign_wrapper(
        priv, plan_hash=plan_hash, submission_nonce=1, chain_id=chain_id,
    )
    return {
        "target": target, "chain_id": chain_id, "fn_signature": fn_signature,
        "abi_types": abi_types, "values": values,
        "tx_value": tx_value, "gas": gas,
        "wrapper": {
            "plan_hash": wrapper.plan_hash,
            "submission_nonce": wrapper.submission_nonce,
            "timestamp": wrapper.timestamp,
            "chain_id": wrapper.chain_id,
        },
        "wrapper_signature": sig,
    }


async def _post(service, body: dict):
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_contract_call(request)


def _json(resp: web.Response) -> dict:
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_function(fresh_signer):
    priv, _ = fresh_signer
    service = _service()
    body = _signed_body(priv, fn_signature="transferOwnership(address)",
                        abi_types=("address",), values=(TARGET,))
    resp = await _post(service, body)
    assert resp.status == 403
    assert "allowlisted" in _json(resp)["error"]
    service.relayer.call_contract_function.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_missing_wrapper(fresh_signer):
    service = _service()
    resp = await _post(service, {
        "target": TARGET, "chain_id": 8453, "fn_signature": "deposit()",
        "abi_types": [], "values": [], "tx_value": 0, "gas": 100_000,
    })
    assert resp.status == 400
    assert "wrapper" in _json(resp)["error"].lower()


@pytest.mark.asyncio
async def test_rejects_wrong_plan_hash(fresh_signer):
    priv, _ = fresh_signer
    service = _service()
    body = _signed_body(priv, plan_hash="0x" + "00" * 32)
    resp = await _post(service, body)
    assert resp.status == 400
    assert "plan_hash" in _json(resp)["error"]


@pytest.mark.asyncio
async def test_rejects_gas_above_cap(fresh_signer):
    priv, _ = fresh_signer
    service = _service()
    body = _signed_body(priv, gas=300_001)
    resp = await _post(service, body)
    assert resp.status == 400
    assert "gas" in _json(resp)["error"]


@pytest.mark.asyncio
async def test_rejects_value_above_cap(fresh_signer):
    priv, _ = fresh_signer
    service = _service()
    body = _signed_body(priv, fn_signature="deposit()", abi_types=(),
                        values=(), tx_value=10**18 + 1)
    resp = await _post(service, body)
    assert resp.status == 400
    assert "tx_value" in _json(resp)["error"]


@pytest.mark.asyncio
async def test_rejects_signer_not_in_registry(fresh_signer):
    priv, _ = fresh_signer
    service = _service()
    body = _signed_body(priv)
    with patch("minotaur_subnet.relayer.main._read_authorized_validators",
               return_value=["0x" + "aa" * 20]):
        resp = await _post(service, body)
    assert resp.status == 403
    assert "ValidatorRegistry" in _json(resp)["error"]
    service.relayer.call_contract_function.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_call_executes_with_coerced_values(fresh_signer):
    priv, addr = fresh_signer
    service = _service()
    body = _signed_body(priv, fn_signature="transfer(address,uint256)",
                        abi_types=("address", "uint256"),
                        values=(TARGET, "5000000000000000"))
    with patch("minotaur_subnet.relayer.main._read_authorized_validators",
               return_value=[addr]):
        resp = await _post(service, body)
    assert resp.status == 200, resp.body
    assert _json(resp) == {"status": "sent", "tx_hash": "0xTxHash"}
    call = service.relayer.call_contract_function.await_args
    # target, chain, signature, types, values — uint256 coerced back to int
    assert call.args[0] == TARGET and call.args[1] == 8453
    assert call.args[2] == "transfer(address,uint256)"
    assert call.args[4] == [TARGET, 5000000000000000]


# ── HttpRelayer.call_contract_function / _resolve_wallet ───────────────────


def _http_relayer(signing_key: str = ""):
    from minotaur_subnet.relayer.http_relayer import HttpRelayer
    return HttpRelayer("http://relayer.test:8091", signing_key=signing_key)


@pytest.mark.asyncio
async def test_http_relayer_requires_signing_key():
    r = _http_relayer("")
    with pytest.raises(RuntimeError, match="signing_key"):
        await r.call_contract_function(TARGET, 8453, "deposit()", [], [])


@pytest.mark.asyncio
async def test_http_relayer_posts_bound_wrapper(fresh_signer):
    priv, addr = fresh_signer
    r = _http_relayer(priv)

    captured: dict = {}

    class _Resp:
        status = 200
        async def json(self):
            return {"status": "sent", "tx_hash": "0xabc"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass
        def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return _Resp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    with patch("minotaur_subnet.relayer.http_relayer.aiohttp.ClientSession", _Session):
        tx = await r.call_contract_function(
            TARGET, 8453, "setAppOwner(address)", ["address"], [TARGET],
        )
    assert tx == "0xabc"
    assert captured["url"].endswith("/v1/contract-call")
    p = captured["payload"]
    # The wrapper's plan_hash must be the canonical call hash of the payload.
    assert p["wrapper"]["plan_hash"] == compute_contract_call_hash(
        8453, TARGET, "setAppOwner(address)", ["address"], [TARGET], 0, 200_000,
    )
    from minotaur_subnet.consensus.leader_wrapper import (
        WrapperPayload, recover_wrapper_signer,
    )
    signer = recover_wrapper_signer(
        WrapperPayload(**p["wrapper"]), p["wrapper_signature"],
    )
    assert signer.lower() == addr.lower()


def test_http_relayer_resolve_wallet_caches(fresh_signer):
    r = _http_relayer()
    fake = MagicMock()
    fake.json.return_value = {"wallets": {"8453": "0x" + "63" * 20}}
    fake.raise_for_status.return_value = None
    with patch("requests.get", return_value=fake) as g:
        assert r._resolve_wallet(8453) == "0x" + "63" * 20
        assert r._resolve_wallet(8453) == "0x" + "63" * 20
        assert g.call_count == 1
    with pytest.raises(RuntimeError, match="no wallet"):
        r._resolve_wallet(964)
