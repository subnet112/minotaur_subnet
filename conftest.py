"""Repo-root pytest conftest — test-run isolation for process-global state.

APP_INTENTS_STORE_PATH: ``api/server.py`` instantiates the SQLite-backed
``AppIntentStore`` at MODULE IMPORT time from this env var; when it is unset
(every developer/CI pytest run), the store falls back to the repo-relative
``minotaur_subnet/store/data/store.db``. That file is git-ignored and survives
between runs, so any test that exercises store-writing routes (e.g. the native
Bittensor permission endpoints) leaks rows into it and LATER runs flake on the
accumulated state (``test_native_permission_routes_*`` count assertions fail
once a previous run's rows are visible — order-dependent, machine-local).

Redirect the default to a fresh per-run temp directory before any test module
imports the server. ``setdefault`` keeps explicit operator overrides working,
and tests that setenv/delenv the variable themselves (e.g.
``test_store_persist_default.py``) still do so within their own monkeypatch
scope. The developer's local ``store/data/store.db`` is never touched by tests
again.

This must live at the REPO ROOT (not tests/conftest.py): test files also exist
under ``minotaur_subnet/`` (harness, api/tests, epoch), and only the rootdir
conftest is loaded for every collected path.
"""

from __future__ import annotations

import os
import tempfile

_test_store_dir = tempfile.mkdtemp(prefix="minotaur-test-app-store-")
os.environ.setdefault(
    "APP_INTENTS_STORE_PATH", os.path.join(_test_store_dir, "store.json")
)
