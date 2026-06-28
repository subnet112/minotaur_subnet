"""Backward-compatible persistence of AppIntentDefinition.shadow_js_code.

The shadow scoring JS must round-trip through the SQLite store, and rows written
before the field existed (no ``shadow_js_code`` key) must still load — as None.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared.types import AppIntentDefinition
from minotaur_subnet.store.app_intent_store import AppIntentStore, _definition_from_dict


_SHADOW_JS = "function score(p,s,c){ return { score: 1, valid: true }; } // shadow padding"


def _mk(app_id: str, shadow=None) -> AppIntentDefinition:
    return AppIntentDefinition(
        app_id=app_id,
        name="Shadow App",
        version="1.0.0",
        intent_type="swap",
        js_code="function score(p,s,c){ return { score: 0.5 }; } // live padding",
        shadow_js_code=shadow,
    )


def test_shadow_js_code_round_trips(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.db")
    store.save_app(_mk("with-shadow", shadow=_SHADOW_JS))

    loaded = store.get_app("with-shadow")
    assert loaded is not None
    assert loaded.shadow_js_code == _SHADOW_JS
    # The live JS is untouched by the shadow field.
    assert "live padding" in loaded.js_code


def test_none_shadow_round_trips(tmp_path):
    store = AppIntentStore(store_path=tmp_path / "store.db")
    store.save_app(_mk("no-shadow", shadow=None))

    loaded = store.get_app("no-shadow")
    assert loaded is not None
    assert loaded.shadow_js_code is None


def test_legacy_row_without_key_loads_as_none():
    # A pre-shadow serialized app row has no 'shadow_js_code' key at all.
    legacy = {
        "app_id": "legacy",
        "name": "Legacy",
        "version": "1.0.0",
        "intent_type": "swap",
        "js_code": "function score(){}",
        # no shadow_js_code key — must default to None, never KeyError.
    }
    defn = _definition_from_dict(legacy)
    assert defn.shadow_js_code is None
    assert defn.app_id == "legacy"
