"""Tests for the wrapper-sig auth on ``POST /deploy`` (C1 from 2026-05-25 audit).

The audit found the endpoint fully unauthenticated. These tests lock in the
fix: every deploy must carry an EIP-191 wrapper whose recovered signer is in
the on-chain ``ValidatorRegistry`` and whose ``plan_hash`` field binds the
bytecode + constructor args (replay protection).
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
    compute_deploy_hash,
    sign_wrapper,
)


# ── compute_deploy_hash unit tests ─────────────────────────────────────────


def test_compute_deploy_hash_is_deterministic():
    h1 = compute_deploy_hash("0x6080", [100, "0xabc"])
    h2 = compute_deploy_hash("0x6080", [100, "0xabc"])
    assert h1 == h2


def test_compute_deploy_hash_changes_on_bytecode_change():
    h1 = compute_deploy_hash("0x6080", [100])
    h2 = compute_deploy_hash("0x6081", [100])
    assert h1 != h2


def test_compute_deploy_hash_changes_on_args_change():
    h1 = compute_deploy_hash("0x6080", [100])
    h2 = compute_deploy_hash("0x6080", [101])
    assert h1 != h2


def test_compute_deploy_hash_accepts_unprefixed_bytecode():
    """The api may send ``0x``-prefixed or not — both must hash the same."""
    assert compute_deploy_hash("0x6080", []) == compute_deploy_hash("6080", [])


def test_compute_deploy_hash_returns_32_byte_hex():
    h = compute_deploy_hash("0xdeadbeef", [1, 2, 3])
    assert h.startswith("0x")
    assert len(h) == 2 + 64  # "0x" + 32 bytes hex


def test_compute_deploy_hash_args_order_matters():
    """Arg order matters even though JSON is sort-key canonical — that's
    for dict keys. List ordering is preserved by spec, so a reordering
    must produce a different hash."""
    h1 = compute_deploy_hash("0x6080", [1, 2])
    h2 = compute_deploy_hash("0x6080", [2, 1])
    assert h1 != h2


def test_compute_deploy_hash_empty_args_stable():
    assert compute_deploy_hash("0x6080", []) == compute_deploy_hash("0x6080", None)


# ── handle_deploy integration tests ────────────────────────────────────────


@pytest.fixture
def fresh_signer():
    """A fresh signing key for each test — never reused."""
    acct = Account.create()
    return acct.key.hex(), acct.address


def _build_service_under_test(
    authorized_addrs: list[str],
    *,
    deploy_result: tuple[str, str] = ("0xDeployedAddr", "0xTxHashFromRelayer"),
):
    """Build a ``RelayerService`` with mocked downstream relayer + registry read."""
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
    service.relayer.deploy_contract = AsyncMock(return_value=deploy_result)

    # Bind the real handle_deploy bound method to our mock instance — we
    # want the real verification logic, just with mocked dependencies.
    service.handle_deploy = relayer_main.RelayerService.handle_deploy.__get__(service)
    return service


async def _post_deploy(service, body: dict):
    """Synthesize an aiohttp Request from a dict body and call handle_deploy."""
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return await service.handle_deploy(request)


def _read_json(resp: web.Response) -> dict:
    """Extract the JSON body from an aiohttp Response."""
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_handle_deploy_rejects_missing_wrapper(fresh_signer):
    _, _ = fresh_signer
    service = _build_service_under_test([])
    resp = await _post_deploy(service, {
        "bytecode": "0x6080", "constructor_args": [], "chain_id": 8453,
    })
    assert resp.status == 400
    assert "wrapper" in _read_json(resp)["error"].lower()


@pytest.mark.asyncio
async def test_handle_deploy_rejects_missing_bytecode(fresh_signer):
    service = _build_service_under_test([])
    resp = await _post_deploy(service, {"chain_id": 8453})
    assert resp.status == 400
    assert "bytecode" in _read_json(resp)["error"].lower()


@pytest.mark.asyncio
async def test_handle_deploy_rejects_unsupported_chain(fresh_signer):
    service = _build_service_under_test([])
    resp = await _post_deploy(service, {
        "bytecode": "0x6080", "chain_id": 999999,
    })
    assert resp.status == 400
    assert "unsupported" in _read_json(resp)["error"].lower()


@pytest.mark.asyncio
async def test_handle_deploy_rejects_wrapper_with_wrong_plan_hash(fresh_signer):
    """A wrapper whose ``plan_hash`` doesn't match the bytecode+args is
    rejected — this is the key replay-protection invariant."""
    priv, _ = fresh_signer
    wrapper, sig = sign_wrapper(
        priv,
        plan_hash="0x" + "00" * 32,  # wrong — doesn't match the body
        submission_nonce=1,
        chain_id=8453,
    )
    service = _build_service_under_test([])
    resp = await _post_deploy(service, {
        "bytecode": "0x6080", "constructor_args": [42], "chain_id": 8453,
        "wrapper": {
            "plan_hash": wrapper.plan_hash, "submission_nonce": wrapper.submission_nonce,
            "timestamp": wrapper.timestamp, "chain_id": wrapper.chain_id,
        },
        "wrapper_signature": sig,
    })
    assert resp.status == 400
    assert "plan_hash doesn't match" in _read_json(resp)["error"]


@pytest.mark.asyncio
async def test_handle_deploy_rejects_unregistered_signer(fresh_signer):
    """Even with a valid wrapper sig + matching deploy hash, if the signer
    isn't in the ValidatorRegistry, reject with 403."""
    priv, addr = fresh_signer
    bytecode = "0x6080"
    args = [42]
    deploy_hash = compute_deploy_hash(bytecode, args)
    wrapper, sig = sign_wrapper(priv, plan_hash=deploy_hash, submission_nonce=1, chain_id=8453)
    service = _build_service_under_test(authorized_addrs=["0x" + "ab" * 20])  # NOT our signer

    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=["0x" + "ab" * 20],
    ):
        resp = await _post_deploy(service, {
            "bytecode": bytecode, "constructor_args": args, "chain_id": 8453,
            "wrapper": {
                "plan_hash": wrapper.plan_hash, "submission_nonce": wrapper.submission_nonce,
                "timestamp": wrapper.timestamp, "chain_id": wrapper.chain_id,
            },
            "wrapper_signature": sig,
        })
    assert resp.status == 403
    err = _read_json(resp)["error"]
    assert "not in ValidatorRegistry" in err
    # Make sure the signer's address shows up in the error (helps debug)
    assert addr.lower()[:8] in err.lower()


@pytest.mark.asyncio
async def test_handle_deploy_accepts_registered_signer_with_matching_wrapper(fresh_signer):
    """Happy path: registered validator signs a wrapper that matches the
    deploy params → relayer broadcasts via deploy_contract → 200."""
    priv, addr = fresh_signer
    bytecode = "0x6080"
    args = [42, "0xabc"]
    deploy_hash = compute_deploy_hash(bytecode, args)
    wrapper, sig = sign_wrapper(priv, plan_hash=deploy_hash, submission_nonce=1, chain_id=8453)

    service = _build_service_under_test(
        authorized_addrs=[addr],
        deploy_result=("0xDeployedContract", "0xTxHash"),
    )
    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[addr],
    ):
        resp = await _post_deploy(service, {
            "bytecode": bytecode, "constructor_args": args, "chain_id": 8453,
            "wrapper": {
                "plan_hash": wrapper.plan_hash, "submission_nonce": wrapper.submission_nonce,
                "timestamp": wrapper.timestamp, "chain_id": wrapper.chain_id,
            },
            "wrapper_signature": sig,
        })
    assert resp.status == 200
    body = _read_json(resp)
    assert body["status"] == "deployed"
    assert body["address"] == "0xDeployedContract"
    assert body["tx_hash"] == "0xTxHash"
    # The relayer was invoked with exactly the verified params
    service.relayer.deploy_contract.assert_awaited_once_with(
        bytecode="0x6080", constructor_args=[42, "0xabc"], chain_id=8453,
    )


@pytest.mark.asyncio
async def test_handle_deploy_rejects_replayed_nonce(fresh_signer):
    """A successful deploy advances the nonce high-water mark. A second
    submit reusing the same nonce is rejected with 409."""
    priv, addr = fresh_signer
    bytecode = "0x6080"
    args = []
    deploy_hash = compute_deploy_hash(bytecode, args)
    wrapper, sig = sign_wrapper(priv, plan_hash=deploy_hash, submission_nonce=5, chain_id=8453)

    service = _build_service_under_test(authorized_addrs=[addr])
    with patch(
        "minotaur_subnet.relayer.main._read_authorized_validators",
        return_value=[addr],
    ):
        body_template = {
            "bytecode": bytecode, "constructor_args": args, "chain_id": 8453,
            "wrapper": {
                "plan_hash": wrapper.plan_hash, "submission_nonce": wrapper.submission_nonce,
                "timestamp": wrapper.timestamp, "chain_id": wrapper.chain_id,
            },
            "wrapper_signature": sig,
        }
        resp1 = await _post_deploy(service, body_template)
        assert resp1.status == 200

        # Replay — same nonce, same wrapper. Rejected even though the sig
        # is still cryptographically valid.
        resp2 = await _post_deploy(service, body_template)
        assert resp2.status == 409
        assert "non-monotonic nonce" in _read_json(resp2)["error"]
