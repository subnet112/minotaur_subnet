"""Tests for the audit surface-reduction PR (H3 + H4 + H5, 2026-05-25 audit).

Each test asserts that a publicly-reachable endpoint or middleware
configuration that the audit flagged is now closed off.
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


def _import_fresh_server(env_overrides: dict[str, str | None]):
    """Re-import api.server with controlled env. FastAPI builds the route
    table at import time, so we have to reload to observe the gating."""
    sys.modules.pop("minotaur_subnet.api.server", None)
    sys.modules.pop("minotaur_subnet.api.routes.local_testnet", None)
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.import_module("minotaur_subnet.api.server")


# ─────────────────────────────────────────────────────────────────────
# H3 — legacy /signatures route deleted from relayer
# ─────────────────────────────────────────────────────────────────────


def test_h3_relayer_no_signatures_route():
    """The legacy POST /signatures path must not be registered anymore."""
    from minotaur_subnet.relayer import main as relayer_main
    # The route registration happens inside create_app(); inspect what it
    # would attach. We can't easily build the app without instantiating
    # the EvmRelayer/etc., so do a textual check instead.
    src = Path(relayer_main.__file__).read_text()
    assert 'app.router.add_post("/signatures"' not in src, (
        "legacy /signatures route must be removed (H3 audit fix)"
    )


def test_h3_relayer_no_handle_submit_signature():
    """The handler function backing /signatures must not exist on RelayerService."""
    from minotaur_subnet.relayer import main as relayer_main
    assert not hasattr(relayer_main.RelayerService, "handle_submit_signature"), (
        "RelayerService.handle_submit_signature should be deleted (H3 audit fix)"
    )


def test_h3_relayer_no_signature_collector_import():
    """SignatureCollector was only used by the deleted handler — drop the import."""
    from minotaur_subnet.relayer import main as relayer_main
    src = Path(relayer_main.__file__).read_text()
    assert "from minotaur_subnet.relayer.signature_collector" not in src, (
        "SignatureCollector import is unused after H3 — remove it"
    )


# ─────────────────────────────────────────────────────────────────────
# H4 — /docs, /redoc, /openapi.json hidden in prod
# ─────────────────────────────────────────────────────────────────────


def test_h4_docs_disabled_when_no_env_flag():
    """Default deployment (prod) has docs/redoc/openapi disabled."""
    srv = _import_fresh_server({"LOCAL_TESTNET": None, "EXPOSE_OPENAPI": None})
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths


def test_h4_docs_enabled_when_local_testnet():
    """LOCAL_TESTNET=1 re-enables docs for dev workflows."""
    srv = _import_fresh_server({"LOCAL_TESTNET": "1", "EXPOSE_OPENAPI": None})
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    assert "/docs" in paths
    assert "/openapi.json" in paths


def test_h4_docs_enabled_when_expose_openapi():
    """Operator opt-in: EXPOSE_OPENAPI=1 enables docs even in prod."""
    srv = _import_fresh_server({"LOCAL_TESTNET": None, "EXPOSE_OPENAPI": "1"})
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    assert "/docs" in paths
    assert "/openapi.json" in paths


# ─────────────────────────────────────────────────────────────────────
# H5 — CORS no longer allow_origins=["*"] by default
# ─────────────────────────────────────────────────────────────────────


def _cors_middleware_options(app):
    """Extract the CORSMiddleware kwargs the app was built with."""
    for mw in app.user_middleware:
        if mw.cls.__name__ == "CORSMiddleware":
            return mw.kwargs
    return None


def test_h5_cors_locked_down_by_default():
    """Default (prod) CORS allow_origins must be the published frontend,
    not the wildcard."""
    srv = _import_fresh_server({
        "LOCAL_TESTNET": None, "CORS_ALLOW_ORIGINS": None, "EXPOSE_OPENAPI": None,
    })
    opts = _cors_middleware_options(srv.app)
    assert opts is not None, "CORSMiddleware should still be wired"
    assert opts["allow_origins"] == ["https://app.minotaursubnet.com"]
    assert opts["allow_origins"] != ["*"], "wildcard CORS is the audit finding"


def test_h5_cors_wildcard_only_in_local_testnet():
    """LOCAL_TESTNET=1 keeps the open-everywhere default for dev."""
    srv = _import_fresh_server({"LOCAL_TESTNET": "1", "CORS_ALLOW_ORIGINS": None})
    opts = _cors_middleware_options(srv.app)
    assert opts["allow_origins"] == ["*"]


def test_h5_cors_override_via_env():
    """Operators can extend the allow-list via comma-separated env."""
    srv = _import_fresh_server({
        "LOCAL_TESTNET": None,
        "CORS_ALLOW_ORIGINS": "https://app.minotaursubnet.com,https://staging.example.com",
    })
    opts = _cors_middleware_options(srv.app)
    assert opts["allow_origins"] == [
        "https://app.minotaursubnet.com",
        "https://staging.example.com",
    ]


def test_h5_cors_methods_restricted_to_explicit_list():
    """``allow_methods=['*']`` was paired with the wildcard origin — narrow
    to the actual verbs we serve. Side-effect tightening."""
    srv = _import_fresh_server({"LOCAL_TESTNET": None, "CORS_ALLOW_ORIGINS": None})
    opts = _cors_middleware_options(srv.app)
    assert "*" not in opts["allow_methods"]
    # The standard HTTP verbs are present
    assert {"GET", "POST", "OPTIONS"}.issubset(set(opts["allow_methods"]))
