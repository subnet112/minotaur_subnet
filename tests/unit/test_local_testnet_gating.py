"""Tests for the ``LOCAL_TESTNET=1`` gate on dev-only routes.

Prod safety contract: ``/v1/testnet/faucet``, ``/v1/testnet/faucet_erc20``,
``/v1/native-bittensor/stake``, and ``/v1/apps/{id}/replay-debug`` MUST NOT
be registered on the route table when ``LOCAL_TESTNET`` is unset or != "1".

The 2026-05-25 public-endpoint audit found all four unauthenticated in
prod; gating the router at registration is defense in depth beyond
per-handler auth.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _reset_local_testnet_env():
    """Always restore ``LOCAL_TESTNET`` env to its pre-test value.

    Previously these tests set ``os.environ["LOCAL_TESTNET"] = "1"`` and
    never cleaned up, bleeding into other test files (notably
    ``test_submissions.py::TestRequireRegisteredMiner``) where the M1
    fail-closed gate has a carve-out for ``LOCAL_TESTNET=1``. Caught
    when the full suite went red after PR #34 made the rot visible to
    CI again.
    """
    prev = os.environ.get("LOCAL_TESTNET")
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("LOCAL_TESTNET", None)
        else:
            os.environ["LOCAL_TESTNET"] = prev


DEV_ONLY_PATHS = {
    "/v1/testnet/faucet",
    "/v1/testnet/faucet_erc20",
    "/v1/native-bittensor/stake",
    "/v1/apps/{app_id}/replay-debug",
}


def _registered_paths(app) -> set[str]:
    """Collect every path currently mounted on the FastAPI app."""
    return {getattr(r, "path", "") for r in app.routes}


def _import_fresh_server(env_value: str | None):
    """Drop cached server-module state and re-import with a controlled
    ``LOCAL_TESTNET`` env. Returns the freshly-imported ``api.server`` module.

    FastAPI builds the route table at import time, so we have to reload to
    observe what the env gating did.
    """
    # Drop any modules we need to re-evaluate.
    for mod in [
        "minotaur_subnet.api.server",
        "minotaur_subnet.api.routes.local_testnet",
    ]:
        sys.modules.pop(mod, None)

    if env_value is None:
        os.environ.pop("LOCAL_TESTNET", None)
    else:
        os.environ["LOCAL_TESTNET"] = env_value

    return importlib.import_module("minotaur_subnet.api.server")


def test_dev_routes_not_registered_without_local_testnet_env():
    """LOCAL_TESTNET unset → dev-only paths do not appear on the route table."""
    srv = _import_fresh_server(None)
    paths = _registered_paths(srv.app)
    for p in DEV_ONLY_PATHS:
        assert p not in paths, f"{p} should NOT be registered when LOCAL_TESTNET is unset"


def test_dev_routes_not_registered_when_local_testnet_zero():
    """LOCAL_TESTNET=0 → still gated. Only the literal "1" enables them."""
    srv = _import_fresh_server("0")
    paths = _registered_paths(srv.app)
    for p in DEV_ONLY_PATHS:
        assert p not in paths, f"{p} should NOT be registered when LOCAL_TESTNET=0"


def test_dev_routes_registered_when_local_testnet_one():
    """LOCAL_TESTNET=1 → all four dev-only paths are mounted."""
    srv = _import_fresh_server("1")
    paths = _registered_paths(srv.app)
    for p in DEV_ONLY_PATHS:
        assert p in paths, f"{p} should be registered when LOCAL_TESTNET=1, got: {paths & DEV_ONLY_PATHS}"


def test_local_testnet_module_router_has_expected_routes():
    """Independent of mount-time gating, the router itself defines the
    expected handlers — guards against accidental deletion."""
    # Make sure import succeeds with the env set (some handlers reference
    # services that may noisily warn otherwise; not an actual dependency).
    os.environ["LOCAL_TESTNET"] = "1"
    sys.modules.pop("minotaur_subnet.api.routes.local_testnet", None)
    from minotaur_subnet.api.routes import local_testnet as lt

    router_paths = {r.path for r in lt.router.routes}
    expected = {
        "/testnet/faucet",
        "/testnet/faucet_erc20",
        "/native-bittensor/stake",
        "/apps/{app_id}/replay-debug",
    }
    assert expected.issubset(router_paths), (
        f"local_testnet router lost a handler. Expected {expected}, got {router_paths}"
    )


def test_legacy_files_no_longer_define_the_moved_handlers():
    """The handlers must live in exactly one place. If a future change
    re-adds them to wallets/apps/native_bittensor, both routers would
    register the same path and the gate becomes meaningless."""
    sys.modules.pop("minotaur_subnet.api.routes.wallets", None)
    sys.modules.pop("minotaur_subnet.api.routes.apps", None)
    sys.modules.pop("minotaur_subnet.api.routes.native_bittensor", None)
    from minotaur_subnet.api.routes import wallets, apps, native_bittensor

    def _paths(mod):
        return {r.path for r in mod.router.routes}

    assert "/testnet/faucet" not in _paths(wallets)
    assert "/testnet/faucet_erc20" not in _paths(wallets)
    assert "/apps/{app_id}/replay-debug" not in _paths(apps)
    assert "/native-bittensor/stake" not in _paths(native_bittensor)
