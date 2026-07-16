"""deregister_app service: retire every deployment, keep order rows."""

from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.api.services.app_lifecycle import deregister_app
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


def test_deregister_retires_all_deployments():
    store, tmp = _store()
    try:
        _app(store)
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.SOLVED, chain_id=1))
        store.save_order({"order_id": "o1", "app_id": "app1", "status": "filled",
                          "chain_id": 8453, "params": {}})

        res = deregister_app(store, "app1")
        assert res["status"] == "deregistered"
        assert res["retired_chains"] == [1, 8453]
        assert store.get_deployment("app1", chain_id=8453).status == AppStatus.RETIRED
        assert store.get_deployment("app1", chain_id=1).status == AppStatus.RETIRED
        # Order rows are preserved (deregister, not delete).
        assert [o["order_id"] for o in store.list_orders(app_id="app1")] == ["o1"]
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

        res = deregister_app(store, "app1")
        assert res["retired_chains"] == [8453]
        assert res["skipped"] == [{"chain_id": 1, "reason": "deploy in progress"}]
        assert store.get_deployment("app1", chain_id=1).status == AppStatus.DEPLOYING
    finally:
        tmp.cleanup()


def test_deregister_unknown_app():
    store, tmp = _store()
    try:
        assert "error" in deregister_app(store, "nope")
    finally:
        tmp.cleanup()


def test_deregister_is_idempotent():
    store, tmp = _store()
    try:
        _app(store, chains=(8453,))
        store.save_deployment(DeploymentResult(
            app_id="app1", status=AppStatus.ACTIVE, chain_id=8453))
        deregister_app(store, "app1")
        res2 = deregister_app(store, "app1")  # already retired
        assert res2["retired_chains"] == []
        assert res2["status"] == "deregistered"
    finally:
        tmp.cleanup()
