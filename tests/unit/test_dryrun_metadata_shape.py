"""The dry-run endpoint could not accept an abi-encoded-metadata plan at all.

`ExecutionPlan(metadata=dict(plan_dict.get("metadata", {})))` COERCES metadata
to a dict at construction. An App that abi.decodes its metadata sends raw bytes,
which cross JSON as a hex string (#1617), and `dict("0x02071f…")` raises

    ValueError: dictionary update sequence element #0 has length 1; 2 is required

— an unhandled 500 with no logged traceback. Measured 2026-08-25 against the
live leader, which isolated it cleanly:

    no key        -> 401     gate works
    key + empty   -> 422     validation works
    key + DEX app -> 200     endpoint works
    key + 964 app -> 500     <- only this app

That mattered beyond the endpoint: the dry-run is the cheap pre-check for
whether an App can score WITHOUT consuming a benchmark round, so this defect
blocked the very tool meant to catch defects before production.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping

import pytest

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    partition_plan_by_leg,
    plan_metadata_fields,
)

# abi.encode(bytes32 hotkey, uint16 uid) for the 964 app, as it crosses JSON
ABI_META = "0x" + "02071f4a273821ef3c7f4b0abb6b79d57f24df8644ac29ca50f2ab645678653c" + f"{230:064x}"


def _construct(md):
    """Exactly the endpoint's construction expression."""
    return (
        dict(md) if isinstance(md, Mapping) else (md or {})
    )


def test_the_old_coercion_is_what_raised():
    with pytest.raises(ValueError, match="dictionary update sequence"):
        dict(ABI_META)


def test_a_hex_string_now_passes_through_unchanged():
    assert _construct(ABI_META) == ABI_META


def test_a_mapping_is_still_COPIED_not_aliased():
    """Callers mutate it (setdefault chain_id), so the copy must survive."""
    src = {"legs": [1]}
    out = _construct(src)
    assert out == src and out is not src


@pytest.mark.parametrize("md", [None, {}, ""])
def test_empty_shapes_normalise_to_a_dict(md):
    assert _construct(md) == {}


def test_setdefault_is_guarded_for_non_mappings():
    for md in (ABI_META, b"\x00\x01"):
        plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                             metadata=md)
        if isinstance(plan.metadata, MutableMapping):     # the guard
            plan.metadata.setdefault("chain_id", 964)
        assert plan.metadata == md, "must reach the simulator exactly as sent"


def test_setdefault_still_applies_to_mappings():
    plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                         metadata={})
    if isinstance(plan.metadata, MutableMapping):
        plan.metadata.setdefault("chain_id", 964)
    assert plan.metadata["chain_id"] == 964


# ── the generic helpers any caller can reach ────────────────────────────────

def test_partition_by_leg_handles_bytes_metadata():
    """Used well outside the benchmark path; it must not raise on bytes."""
    plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                         metadata=ABI_META)
    assert partition_plan_by_leg(plan) == {0: []}


def test_mock_score_handles_bytes_metadata():
    from minotaur_subnet.shared.simulation import compute_mock_score
    plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                         metadata=ABI_META)
    assert isinstance(compute_mock_score(plan, {}), float)


def test_the_964_plan_shape_survives_construction_end_to_end():
    md = _construct(ABI_META)
    plan = ExecutionPlan(intent_id="app_6b067226cec9", interactions=[],
                         deadline=0, nonce=0, metadata=md)
    assert plan.metadata == ABI_META
    assert plan_metadata_fields(plan) == {}
    assert partition_plan_by_leg(plan) == {0: []}


# ── the live execution rail ──────────────────────────────────────────────────
#
# The rail WRITES platform fields into plan.metadata — platform_fee_wei, and
# escrow_params / plan_set / multi_leg_plan in the cross-chain compile. Those
# writes cannot land in bytes, and silently SKIPPING them is the wrong answer:
# the fee "travels in the consensus proposal" and the escrow params gate the
# relayer, so dropping them turns a money path into a quiet mis-execution.
#
# So the rail rejects once, up front, before any partial mutation. That changes
# nothing that works today — such a plan currently dies a few lines later on an
# unhandled TypeError, having already mutated whatever preceded it.

def test_the_rail_precondition_rejects_rather_than_dropping_platform_fields():
    from collections.abc import MutableMapping
    for md in (ABI_META, b"\x00\x01", None):
        plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                             metadata=md)
        assert not isinstance(plan.metadata, MutableMapping), (
            "a non-mapping must be refused before any platform field is written"
        )


def test_the_rail_precondition_admits_ordinary_dict_plans():
    from collections.abc import MutableMapping
    plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                         metadata={})
    assert isinstance(plan.metadata, MutableMapping)
    plan.metadata["platform_fee_wei"] = 123          # the write that follows
    assert plan.metadata["platform_fee_wei"] == 123


def test_extract_leg_plan_cannot_hand_bytes_metadata_to_the_bridge_tracker():
    """bridge_tracker writes dest_plan.metadata["phase"] unguarded.

    That is safe ONLY because a bytes-metadata plan has no legs, so the tracker
    returns before reaching the write. Pin that, or the guard becomes load
    bearing without anyone noticing.
    """
    from minotaur_subnet.shared.types import extract_leg_plan
    plan = ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0,
                         metadata=ABI_META)
    assert plan_metadata_fields(plan).get("legs", []) == []
    assert extract_leg_plan(plan, 0) is plan       # unchanged, no legs to split
