"""`plan.metadata` is not always a dict, and the scoring path assumed it was.

An App that `abi.decode`s its metadata takes raw bytes — pinned in #1617:
"solvers return bytes for metadata an App abi-decodes; a dict still becomes
JSON". AlphaYieldApp (chain 964) is exactly that shape: its plan is DATA, not
code, carrying `abi.encode(bytes32 hotkey, uint16 uid)` and no interactions.

Putting it in the live corpus on 2026-08-25 produced ~300 tracebacks in 20
minutes and scored 0 for every miner on every row, each attempt still burning a
sandbox solver run:

    'str' object does not support item assignment
    'str' object has no attribute 'get'
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from minotaur_subnet.shared.types import ExecutionPlan, plan_metadata_fields


def _plan(metadata, interactions=None):
    return ExecutionPlan(intent_id="i", interactions=interactions or [],
                         deadline=0, nonce=0, metadata=metadata)


# ── the helper ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("md", [None, "a string", b"\x00\x01", 7, ["a"]])
def test_non_mappings_read_as_empty(md):
    assert plan_metadata_fields(_plan(md)) == {}


def test_a_real_mapping_passes_through_unchanged():
    md = {"chain_id": 8453, "legs": [1]}
    assert plan_metadata_fields(_plan(md)) is md


# ── the substring trap, which is worse than the crash ────────────────────────

def test_membership_on_a_string_is_a_substring_test_not_a_key_test():
    """Why the fix is isinstance and not try/except.

    The old guard was `if "chain_id" not in plan.metadata`. On a str that is a
    SUBSTRING test: it passed here for the wrong reason (and would SKIP for the
    wrong reason on metadata that happened to contain the text), so the bug
    could have been a silent wrong answer rather than a loud crash.
    """
    md = 'abi-ish blob mentioning chain_id inside it'
    assert ("chain_id" in md) is True          # substring — not a key
    assert plan_metadata_fields(_plan(md)) == {}   # correctly: no fields


# ── the call sites ───────────────────────────────────────────────────────────

def test_chain_id_injection_never_mutates_non_dict_metadata():
    """The orchestrator block that raised. Skipping it is safe because chain
    selection comes from the AUTHORITATIVE chain_id kwarg, not from metadata."""
    from collections.abc import MutableMapping
    for md in (b"\x00\x01", "blob"):
        plan = _plan(md)
        if plan.metadata is None:
            plan.metadata = {}
        if isinstance(plan.metadata, MutableMapping):        # the guard
            plan.metadata.setdefault("chain_id", 964)
        assert plan.metadata == md, "metadata must be left exactly as the solver sent it"


def test_chain_id_injection_still_works_for_dict_metadata():
    from collections.abc import MutableMapping
    plan = _plan({"legs": []})
    if isinstance(plan.metadata, MutableMapping):
        plan.metadata.setdefault("chain_id", 964)
    assert plan.metadata["chain_id"] == 964


def test_injection_does_not_clobber_an_existing_chain_id():
    from collections.abc import MutableMapping
    plan = _plan({"chain_id": 1})
    if isinstance(plan.metadata, MutableMapping):
        plan.metadata.setdefault("chain_id", 964)
    assert plan.metadata["chain_id"] == 1


def test_routing_falls_back_when_metadata_carries_no_hint():
    """A bytes-metadata plan simply has no chain hint; the ladder handles it."""
    ix = SimpleNamespace(chain_id=8453)
    plan = _plan(b"\x00\x01", interactions=[ix])
    hint = plan_metadata_fields(plan).get("chain_id")
    assert hint is None
    assert (hint if hint is not None else plan.interactions[0].chain_id) == 8453


def test_the_964_app_shape_end_to_end_reads_clean():
    """AlphaYieldApp: abi-encoded bytes metadata, and NO interactions at all."""
    plan = _plan(b"\x00" * 64, interactions=[])
    assert plan_metadata_fields(plan).get("chain_id") is None
    assert plan_metadata_fields(plan).get("legs") is None
    assert plan_metadata_fields(plan).get("executor") is None
    assert plan_metadata_fields(plan).get("dst_chain_id") is None
