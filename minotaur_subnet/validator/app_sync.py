"""Validator app catalog sync.

A follower validator running the third-party stack does not receive
``create_app`` / ``deploy_app`` calls — those happen on the subnet team's
leader API. Without a sync mechanism the follower's ``AppIntentStore`` would
stay empty and ``JsExecutionEngine`` would have no scoring code loaded, so
incoming order-consensus proposals could not be re-scored and would never be
signed.

This module periodically pulls ``GET /v1/apps/`` and ``GET /v1/apps/{id}/status``
from the configured leader and upserts both ``AppIntentDefinition`` and
``DeploymentResult`` records into the local store. The existing
``_rescan_loop`` in ``validator/main.py`` picks up the new JS on its next
tick and hot-loads it into the engine.

SECURITY: ``js_code`` is fetched from the leader and trusted as-is. There is
no on-chain hash anchor at this layer — a compromised leader could push
malicious JS to followers. Anchoring ``keccak256(js_code)`` on-chain via the
``AppRegistry`` is tracked as a follow-up.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiohttp

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.store.app_intent_store import _definition_from_dict

logger = logging.getLogger(__name__)


def _app_status_from_str(s: str) -> AppStatus:
    try:
        return AppStatus(s)
    except (ValueError, KeyError):
        return AppStatus.DRAFT


def _hash_definition(d: AppIntentDefinition) -> str:
    h = hashlib.sha256()
    h.update(d.js_code.encode())
    h.update(b"|")
    h.update((d.solidity_code or "").encode())
    h.update(b"|")
    h.update(d.name.encode())
    h.update(b"|")
    h.update(d.intent_type.encode())
    h.update(b"|")
    h.update(d.version.encode())
    return h.hexdigest()


def _hash_deployment(d: DeploymentResult) -> str:
    h = hashlib.sha256()
    h.update((d.contract_address or "").encode())
    h.update(b"|")
    h.update(str(d.chain_id).encode())
    h.update(b"|")
    h.update(d.status.value.encode())
    return h.hexdigest()


class ValidatorAppCatalogSync:
    """Periodically pulls the leader's app catalog into the local store."""

    def __init__(
        self,
        store: AppIntentStore,
        leader_url: str,
        poll_interval: float = 60.0,
        request_timeout: float = 15.0,
    ) -> None:
        self.store = store
        self.leader_url = leader_url.rstrip("/")
        self.poll_interval = poll_interval
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._stopped = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Run an initial sync, then start the background poll loop."""
        try:
            await self.sync_once()
        except Exception as exc:
            logger.warning(
                "Initial catalog sync from %s failed: %s (will retry on tick)",
                self.leader_url, exc,
            )
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.poll_interval)
                if self._stopped:
                    break
                await self.sync_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Catalog sync tick failed: %s", exc)

    async def sync_once(self) -> tuple[int, int]:
        """Pull the catalog and upsert changes. Returns (apps_updated, deployments_updated)."""
        list_url = f"{self.leader_url}/v1/apps/"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(list_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GET {list_url} returned {resp.status}")
                data = await resp.json()
                apps_summary = data.get("apps", []) if isinstance(data, dict) else []

            apps_updated = 0
            deployments_updated = 0
            for app_summary in apps_summary:
                if not isinstance(app_summary, dict):
                    continue
                app_id = app_summary.get("app_id")
                if not app_id:
                    continue
                status_url = f"{self.leader_url}/v1/apps/{app_id}/status"
                try:
                    async with session.get(status_url) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "GET %s returned %d; skipping",
                                status_url, resp.status,
                            )
                            continue
                        status_data = await resp.json()
                except aiohttp.ClientError as exc:
                    logger.warning("Fetch %s failed: %s", status_url, exc)
                    continue

                a_updated, d_updated = self._upsert(status_data)
                apps_updated += a_updated
                deployments_updated += d_updated

        if apps_updated or deployments_updated:
            logger.info(
                "App catalog sync: %d app(s) + %d deployment(s) updated from %s",
                apps_updated, deployments_updated, self.leader_url,
            )
        return apps_updated, deployments_updated

    def _upsert(self, status_payload: dict[str, Any]) -> tuple[int, int]:
        if not isinstance(status_payload, dict):
            return 0, 0
        app_dict = status_payload.get("app")
        if not isinstance(app_dict, dict):
            return 0, 0

        try:
            new_def = _definition_from_dict(app_dict)
        except KeyError as exc:
            logger.warning("Malformed app payload (missing %s); skipping", exc)
            return 0, 0

        apps_updated = 0
        existing = self.store.get_app(new_def.app_id)
        if existing is None or _hash_definition(existing) != _hash_definition(new_def):
            self.store.save_app(new_def)
            apps_updated = 1

        # The leader's primary `deployment` carries `abi`; the per-chain
        # `deployments` map does not. Clone the primary ABI to all entries —
        # the compiled bytecode is identical across chains for a given App.
        primary = status_payload.get("deployment")
        primary_abi = primary.get("abi") if isinstance(primary, dict) else None

        deployments_updated = 0
        per_chain = status_payload.get("deployments") or {}
        for chain_id_str, dep_dict in per_chain.items():
            if not isinstance(dep_dict, dict):
                continue
            try:
                chain_id = int(chain_id_str)
            except (TypeError, ValueError):
                continue
            new_dep = DeploymentResult(
                app_id=new_def.app_id,
                status=_app_status_from_str(dep_dict.get("status", "draft")),
                contract_address=dep_dict.get("contract_address"),
                chain_id=chain_id,
                abi=primary_abi,
            )
            existing_dep = self.store.get_deployment(new_def.app_id, chain_id=chain_id)
            if existing_dep is None or _hash_deployment(existing_dep) != _hash_deployment(new_dep):
                self.store.save_deployment(new_dep)
                deployments_updated += 1

        return apps_updated, deployments_updated
