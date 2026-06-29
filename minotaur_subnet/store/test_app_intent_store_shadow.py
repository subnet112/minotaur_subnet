"""Backward-compatible load of legacy app rows that still carry the removed
``shadow_js_code`` key.

The dual-slot shadow scorer is gone (the relative rule now reads the LIVE
scorer's ``metadata.raw_output``), so ``AppIntentDefinition`` no longer has a
``shadow_js_code`` field. A pre-cutover serialized row may still contain a stale
``shadow_js_code`` key on disk — loading it must IGNORE the key gracefully, never
raise a TypeError/KeyError.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.store.app_intent_store import _definition_from_dict


def test_legacy_row_with_shadow_key_loads_and_ignores_it():
    # A pre-cutover serialized app row carries the removed shadow_js_code key.
    legacy = {
        "app_id": "legacy",
        "name": "Legacy",
        "version": "1.0.0",
        "intent_type": "swap",
        "js_code": "function score(){}",
        "shadow_js_code": "function shadow(){}",  # stale key — must be ignored
    }
    defn = _definition_from_dict(legacy)
    assert defn.app_id == "legacy"
    assert defn.js_code == "function score(){}"
    # The removed field must not be reconstructed onto the definition.
    assert not hasattr(defn, "shadow_js_code")


def test_row_without_shadow_key_loads():
    row = {
        "app_id": "plain",
        "name": "Plain",
        "version": "1.0.0",
        "intent_type": "swap",
        "js_code": "function score(){}",
    }
    defn = _definition_from_dict(row)
    assert defn.app_id == "plain"
    assert not hasattr(defn, "shadow_js_code")
