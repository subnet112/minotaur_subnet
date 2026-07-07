"""App-level status derivation and per-chain deployment attribution.

Live confusion 2026-07-07: an app solved on Base and draft on Ethereum was
shown as "deployed on Ethereum" — the list endpoint said "solved" with
any-chain semantics and carried no per-chain map, and the status endpoint's
singular "deployment" field is the preferred record from ANOTHER chain.
Both endpoints now share one derivation and both carry a "deployments" map.
"""
from __future__ import annotations

from minotaur_subnet.api.services.app_service import (
    _derive_overall_status,
    get_app_status,
    list_minotaur_subnet,
)
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store.app_intent_store import AppIntentStore

BASE_ADDR = "0x" + "22" * 20


def _store(tmp_path) -> AppIntentStore:
    s = AppIntentStore(store_path=tmp_path / "s.db")
    s.save_app(AppIntentDefinition(
        app_id="app_x", name="dex", version="1.0.0", intent_type="swap",
        js_code="function score(){return 1;}", solidity_code="contract X {}",
        config=AppIntentConfig(supported_chains=[8453, 1]),
        deployer="0x" + "44" * 20,
    ))
    return s


def _dep(chain_id: int, status: AppStatus, addr: str | None = None) -> DeploymentResult:
    return DeploymentResult(
        app_id="app_x", status=status, js_code_hash="x",
        chain_id=chain_id, contract_address=addr,
    )


# ── the shared derivation ────────────────────────────────────────────────


def test_mixed_solved_plus_draft_is_partial_not_solved():
    deployments = {8453: _dep(8453, AppStatus.SOLVED, BASE_ADDR), 1: _dep(1, AppStatus.DRAFT)}
    assert _derive_overall_status(deployments) == "partial"


def test_uniform_and_empty_cases():
    assert _derive_overall_status({}) == "draft"
    assert _derive_overall_status({8453: _dep(8453, AppStatus.SOLVED, BASE_ADDR)}) == "solved"
    assert _derive_overall_status({8453: _dep(8453, AppStatus.SOLVING)}) == "solving"
    assert _derive_overall_status({
        8453: _dep(8453, AppStatus.ACTIVE, BASE_ADDR),
        1: _dep(1, AppStatus.SOLVED, BASE_ADDR),
    }) == "solved"  # mix of order-ready
    assert _derive_overall_status({8453: _dep(8453, AppStatus.DEPLOYING)}) == "deploying"


# ── endpoints agree and carry per-chain truth ────────────────────────────


def test_list_and_status_agree_on_mixed_state(tmp_path):
    s = _store(tmp_path)
    s.save_deployment(_dep(8453, AppStatus.SOLVED, BASE_ADDR))
    s.save_deployment(_dep(1, AppStatus.DRAFT))

    listed = list_minotaur_subnet(s)["apps"][0]
    status = get_app_status(s, "app_x")

    assert listed["status"] == status["status"] == "partial"


def test_list_carries_per_chain_deployments(tmp_path):
    s = _store(tmp_path)
    s.save_deployment(_dep(8453, AppStatus.SOLVED, BASE_ADDR))
    s.save_deployment(_dep(1, AppStatus.DRAFT))

    listed = list_minotaur_subnet(s)["apps"][0]

    assert listed["deployments"]["8453"] == {
        "status": "solved", "contract_address": BASE_ADDR,
    }
    # The chain that never deployed is attributed honestly
    assert listed["deployments"]["1"] == {
        "status": "draft", "contract_address": None,
    }


def test_list_app_without_deployments(tmp_path):
    s = _store(tmp_path)
    listed = list_minotaur_subnet(s)["apps"][0]
    assert listed["status"] == "draft"
    assert listed["deployments"] == {}


def test_status_deployments_map_has_no_cross_chain_bleed(tmp_path):
    s = _store(tmp_path)
    s.save_deployment(_dep(8453, AppStatus.SOLVED, BASE_ADDR))
    s.save_deployment(_dep(1, AppStatus.DRAFT))

    status = get_app_status(s, "app_x")

    # The singular field is the preferred (Base) record — documented trap,
    # kept for app_sync compat...
    assert status["deployment"]["chain_id"] == 8453
    # ...but the per-chain map must never show Base's address under chain 1.
    assert status["deployments"][1]["contract_address"] is None
    assert status["deployments"][1]["status"] == "draft"
    assert status["deployments"][8453]["contract_address"] == BASE_ADDR
