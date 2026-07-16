"""catalog_fingerprint: leader/follower catalog convergence detection."""

from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.validator.app_sync import catalog_fingerprint
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)

CHAIN = 8453


def _store():
    tmp = tempfile.TemporaryDirectory()
    return AppIntentStore(Path(tmp.name) / "s.db"), tmp


def _seed(store, app_id="a", status=AppStatus.ACTIVE):
    store.save_app(AppIntentDefinition(
        app_id=app_id, name=app_id, version="1.0.0", intent_type="swap",
        js_code="code", config=AppIntentConfig(supported_chains=[CHAIN]),
    ))
    store.save_deployment(DeploymentResult(
        app_id=app_id, status=status, contract_address="0xabc", chain_id=CHAIN))


def test_identical_catalogs_have_identical_fingerprints():
    leader, lt = _store()
    follower, ft = _store()
    try:
        _seed(leader)
        _seed(follower)
        assert catalog_fingerprint(leader) == catalog_fingerprint(follower) != ""
    finally:
        lt.cleanup(); ft.cleanup()


def test_retirement_changes_fingerprint():
    store, tmp = _store()
    try:
        _seed(store)
        before = catalog_fingerprint(store)
        store.update_deployment_status("a", CHAIN, AppStatus.RETIRED)
        assert catalog_fingerprint(store) != before, "retire must flip the fingerprint"
    finally:
        tmp.cleanup()


def test_divergent_status_detected():
    leader, lt = _store()
    follower, ft = _store()
    try:
        _seed(leader, status=AppStatus.RETIRED)
        _seed(follower, status=AppStatus.ACTIVE)  # follower missed the retirement
        assert catalog_fingerprint(leader) != catalog_fingerprint(follower)
    finally:
        lt.cleanup(); ft.cleanup()


def test_empty_catalog_is_stable_not_error():
    store, tmp = _store()
    try:
        assert catalog_fingerprint(store) == catalog_fingerprint(store)
    finally:
        tmp.cleanup()
