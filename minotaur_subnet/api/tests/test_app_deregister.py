"""deregister_app service: schedule a round-anchored retirement, keep order rows."""

from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.api.services.app_lifecycle import deregister_app, RETIRE_LEAD_EPOCHS
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)


def _store():
    tmp = tempfile.TemporaryDirectory()
    return AppIntentStore(Path(tmp.name) / "s.db"), tmp


def _app(store, app_id="app1", chains=(8453, 1)):
    store.save_app(AppIntentDefinition(
        app_id=app_id, name=app_id, version="1.0.0", intent_type="swap",
        js_code="x", config=AppIntentConfig(supported_chains=list(chains)),
    ))


def test_deregister_schedules_retiring_with_effective_epoch():
    store, tmp = _store()
    try:
        _app(store)
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.SOLVED, chain_id=1))
        store.save_order({"order_id": "o1", "app_id": "app1", "status": "filled",
                          "chain_id": 8453, "params": {}})

        res = deregister_app(store, "app1", current_epoch=1000)
        assert res["status"] == "retiring"
        assert res["retire_effective_epoch"] == 1000 + RETIRE_LEAD_EPOCHS
        assert res["scheduled_chains"] == [1, 8453]

        for cid in (8453, 1):
            dep = store.get_deployment("app1", chain_id=cid)
            assert dep.status == AppStatus.RETIRING
            assert dep.retire_effective_epoch == 1000 + RETIRE_LEAD_EPOCHS
            # Not yet effective at the current epoch; effective at/after the cutover.
            assert not dep.is_effectively_retired(1000)
            assert dep.is_effectively_retired(1000 + RETIRE_LEAD_EPOCHS)

        # Order rows preserved (deregister, not delete).
        assert [o["order_id"] for o in store.list_orders(app_id="app1")] == ["o1"]
    finally:
        tmp.cleanup()


def test_deregister_effective_epoch_survives_store_roundtrip():
    # The stamped epoch must persist (SQLite (de)serialization) or followers can't
    # anchor the cutover.
    store, tmp = _store()
    try:
        _app(store, chains=(8453,))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        deregister_app(store, "app1", current_epoch=500)
        # Fresh read from the DB.
        dep = store.get_deployments("app1")[8453]
        assert dep.status == AppStatus.RETIRING
        assert dep.retire_effective_epoch == 500 + RETIRE_LEAD_EPOCHS
    finally:
        tmp.cleanup()


def test_deregister_skips_mid_deploy():
    store, tmp = _store()
    try:
        _app(store)
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.DEPLOYING, chain_id=1))

        res = deregister_app(store, "app1", current_epoch=10)
        assert res["scheduled_chains"] == [8453]
        assert res["skipped"] == [{"chain_id": 1, "reason": "deploy in progress"}]
        assert store.get_deployment("app1", chain_id=1).status == AppStatus.DEPLOYING
    finally:
        tmp.cleanup()


def test_deregister_without_open_round_errors():
    store, tmp = _store()
    try:
        _app(store, chains=(8453,))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        res = deregister_app(store, "app1", current_epoch=None)
        assert "error" in res
        # Nothing scheduled — deployment untouched.
        assert store.get_deployment("app1", chain_id=8453).status == AppStatus.ACTIVE
    finally:
        tmp.cleanup()


def test_deregister_unknown_app():
    store, tmp = _store()
    try:
        assert "error" in deregister_app(store, "nope", current_epoch=1)
    finally:
        tmp.cleanup()


def test_deregister_is_idempotent():
    store, tmp = _store()
    try:
        _app(store, chains=(8453,))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        deregister_app(store, "app1", current_epoch=100)
        res2 = deregister_app(store, "app1", current_epoch=200)  # already RETIRING
        assert res2["scheduled_chains"] == []
        # The original cutover is not moved by a second call.
        assert store.get_deployment("app1", chain_id=8453).retire_effective_epoch == 100 + RETIRE_LEAD_EPOCHS
    finally:
        tmp.cleanup()
