"""Tests for ValidatorAppCatalogSync — follower app catalog pull from leader."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest
from aiohttp import web

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.validator.app_sync import (
    ValidatorAppCatalogSync,
    _hash_definition,
    _hash_deployment,
)


# ── Fake leader fixture ───────────────────────────────────────────────────


class _FakeLeader:
    """Tiny aiohttp app that mimics the leader's /v1/apps/ endpoints."""

    def __init__(self) -> None:
        self.apps: dict[str, AppIntentDefinition] = {}
        self.deployments: dict[str, dict[int, DeploymentResult]] = {}
        self.list_calls = 0
        self.status_calls: dict[str, int] = {}

    def add_app(
        self,
        app_id: str,
        js_code: str,
        chains: list[int] | None = None,
        contract_address: str = "0x" + "ab" * 20,
        status: AppStatus = AppStatus.ACTIVE,
    ) -> None:
        chains = chains or [8453]
        self.apps[app_id] = AppIntentDefinition(
            app_id=app_id,
            name=f"App {app_id}",
            version="1.0.0",
            intent_type="swap",
            js_code=js_code,
            solidity_code="contract X {}",
            config=AppIntentConfig(supported_chains=chains),
            deployer="0x" + "cd" * 20,
            description="test app",
            manifest={"intent_functions": ["execute"]},
        )
        self.deployments[app_id] = {
            chain_id: DeploymentResult(
                app_id=app_id,
                status=status,
                contract_address=contract_address,
                chain_id=chain_id,
                abi=[{"name": "execute", "type": "function"}],
            )
            for chain_id in chains
        }

    async def _list(self, request: web.Request) -> web.Response:
        self.list_calls += 1
        items = []
        for definition in self.apps.values():
            d = asdict(definition)
            d["status"] = "active"
            items.append(d)
        return web.json_response({"apps": items, "total": len(items)})

    async def _status(self, request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]
        self.status_calls[app_id] = self.status_calls.get(app_id, 0) + 1
        definition = self.apps.get(app_id)
        if definition is None:
            return web.json_response({"error": "not found"}, status=404)
        deployments = self.deployments.get(app_id, {})
        primary = next(iter(deployments.values()), None)
        return web.json_response({
            "app_id": app_id,
            "name": definition.name,
            "status": "active",
            "app": asdict(definition),
            "deployment": {
                "contract_address": primary.contract_address,
                "chain_id": primary.chain_id,
                "status": primary.status.value,
                "abi": primary.abi,
            } if primary else None,
            "deployments": {
                str(chain_id): {
                    "contract_address": dep.contract_address,
                    "chain_id": dep.chain_id,
                    "status": dep.status.value,
                }
                for chain_id, dep in deployments.items()
            },
        })

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/apps/", self._list)
        app.router.add_get("/v1/apps/{app_id}/status", self._status)
        return app


class _RunningLeader:
    """Async context that spins up a _FakeLeader on a random localhost port."""

    def __init__(self) -> None:
        self.fake = _FakeLeader()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.url: str = ""

    async def __aenter__(self) -> "_RunningLeader":
        self._runner = web.AppRunner(self.fake.build_app())
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Pull the OS-assigned port back out of the underlying socket.
        socks = self._site._server.sockets  # type: ignore[attr-defined]
        port = socks[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield AppIntentStore(store_path=Path(tmp) / "store.json")


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_writes_app_and_deployment(store):
    async with _RunningLeader() as leader:
        leader.fake.add_app(
            "app-1",
            js_code="function score(){return 1;}",
            chains=[8453, 964],
        )
        sync = ValidatorAppCatalogSync(
            store=store, leader_url=leader.url, poll_interval=3600,
        )
        apps_updated, deps_updated = await sync.sync_once()

    assert apps_updated == 1
    assert deps_updated == 2
    saved = store.get_app("app-1")
    assert saved is not None
    assert saved.js_code == "function score(){return 1;}"
    assert saved.intent_type == "swap"
    base_dep = store.get_deployment("app-1", chain_id=8453)
    btevm_dep = store.get_deployment("app-1", chain_id=964)
    assert base_dep is not None and base_dep.contract_address == "0x" + "ab" * 20
    assert btevm_dep is not None and btevm_dep.chain_id == 964
    # Primary ABI is cloned onto every per-chain deployment.
    assert base_dep.abi == [{"name": "execute", "type": "function"}]
    assert btevm_dep.abi == [{"name": "execute", "type": "function"}]


@pytest.mark.asyncio
async def test_unchanged_app_is_a_noop_on_resync(store):
    async with _RunningLeader() as leader:
        leader.fake.add_app("app-1", js_code="function score(){return 1;}")
        sync = ValidatorAppCatalogSync(
            store=store, leader_url=leader.url, poll_interval=3600,
        )
        first = await sync.sync_once()
        second = await sync.sync_once()

        assert first == (1, 1)
        assert second == (0, 0)
        assert leader.fake.list_calls == 2
        assert leader.fake.status_calls["app-1"] == 2


@pytest.mark.asyncio
async def test_js_update_is_picked_up(store):
    async with _RunningLeader() as leader:
        leader.fake.add_app("app-1", js_code="function score(){return 1;}")
        sync = ValidatorAppCatalogSync(
            store=store, leader_url=leader.url, poll_interval=3600,
        )
        await sync.sync_once()
        original_hash = _hash_definition(store.get_app("app-1"))

        leader.fake.add_app("app-1", js_code="function score(){return 2;}")
        apps_updated, _ = await sync.sync_once()

        assert apps_updated == 1
        saved = store.get_app("app-1")
        assert saved.js_code == "function score(){return 2;}"
        assert _hash_definition(saved) != original_hash


@pytest.mark.asyncio
async def test_deployment_status_change_is_picked_up(store):
    async with _RunningLeader() as leader:
        leader.fake.add_app(
            "app-1",
            js_code="function score(){return 1;}",
            status=AppStatus.SOLVING,
        )
        sync = ValidatorAppCatalogSync(
            store=store, leader_url=leader.url, poll_interval=3600,
        )
        await sync.sync_once()
        assert store.get_deployment("app-1").status == AppStatus.SOLVING

        leader.fake.add_app(
            "app-1",
            js_code="function score(){return 1;}",
            status=AppStatus.ACTIVE,
        )
        apps_updated, deps_updated = await sync.sync_once()

    # JS code unchanged so app count is 0; only deployment moves.
    assert apps_updated == 0
    assert deps_updated == 1
    assert store.get_deployment("app-1").status == AppStatus.ACTIVE


@pytest.mark.asyncio
async def test_unreachable_leader_does_not_crash_initial_sync(store):
    """Initial sync against a dead URL should log + return without raising."""
    sync = ValidatorAppCatalogSync(
        store=store,
        leader_url="http://127.0.0.1:1",  # nothing listening
        poll_interval=3600,
        request_timeout=1.0,
    )
    await sync.start()
    await sync.stop()
    assert store.list_apps() == []


def test_hash_helpers_are_deterministic():
    """The change-detection hashes must be stable for identical inputs."""
    a = AppIntentDefinition(
        app_id="x", name="n", version="1", intent_type="swap",
        js_code="code", solidity_code="src",
    )
    b = AppIntentDefinition(
        app_id="x", name="n", version="1", intent_type="swap",
        js_code="code", solidity_code="src",
    )
    assert _hash_definition(a) == _hash_definition(b)

    d1 = DeploymentResult(
        app_id="x", status=AppStatus.ACTIVE,
        contract_address="0xabc", chain_id=8453,
    )
    d2 = DeploymentResult(
        app_id="x", status=AppStatus.ACTIVE,
        contract_address="0xabc", chain_id=8453,
    )
    assert _hash_deployment(d1) == _hash_deployment(d2)


# ── prune: deletion propagation for absent non-operational apps ───────────


def _seed_local_app(store, app_id: str, status: AppStatus, contract: str | None):
    """A locally-known app the (fake) leader does NOT list."""
    store.save_app(AppIntentDefinition(
        app_id=app_id,
        name=f"Local {app_id}",
        version="1.0.0",
        intent_type="swap",
        js_code="// local",
        solidity_code="contract L {}",
        config=AppIntentConfig(supported_chains=[8453]),
        deployer="0x" + "ee" * 20,
        description="local-only app",
        manifest={"intent_functions": ["execute"]},
    ))
    store.save_deployment(DeploymentResult(
        app_id=app_id,
        status=status,
        contract_address=contract,
        chain_id=8453,
        abi=None,
    ))


@pytest.mark.asyncio
async def test_prune_removes_absent_draft(store):
    _seed_local_app(store, "app_stale_draft", AppStatus.DRAFT, None)
    async with _RunningLeader() as leader:
        leader.fake.add_app("app_live", js_code="// live")
        sync = ValidatorAppCatalogSync(
            store, leader.url, is_follower=lambda: True,
        )
        await sync.sync_once()
    ids = {a.app_id for a in store.list_apps()}
    assert "app_stale_draft" not in ids  # deletion propagated
    assert "app_live" in ids
    # Cascade: no dangling deployment row either.
    assert store.get_deployment("app_stale_draft") is None


@pytest.mark.asyncio
async def test_prune_never_touches_operational_absent_app(store):
    # An app this follower can actively score against is NEVER auto-deleted
    # on the strength of one leader listing — logged for a human instead.
    _seed_local_app(store, "app_active_local", AppStatus.ACTIVE, "0x" + "ab" * 20)
    async with _RunningLeader() as leader:
        leader.fake.add_app("app_live", js_code="// live")
        sync = ValidatorAppCatalogSync(
            store, leader.url, is_follower=lambda: True,
        )
        await sync.sync_once()
    assert "app_active_local" in {a.app_id for a in store.list_apps()}


@pytest.mark.asyncio
async def test_prune_skips_on_empty_leader_catalog(store):
    # A degenerate EMPTY catalog (misconfigured/bootstrapping leader) must
    # never mass-delete a follower's store — even the non-operational rows.
    _seed_local_app(store, "app_stale_draft", AppStatus.DRAFT, None)
    async with _RunningLeader() as leader:
        sync = ValidatorAppCatalogSync(
            store, leader.url, is_follower=lambda: True,
        )
        await sync.sync_once()
    assert "app_stale_draft" in {a.app_id for a in store.list_apps()}


def test_delete_app_cascades_deployments(store):
    _seed_local_app(store, "app_x", AppStatus.DRAFT, None)
    assert store.get_deployment("app_x") is not None
    assert store.delete_app("app_x") is True
    assert store.get_app("app_x") is None
    assert store.get_deployment("app_x") is None
