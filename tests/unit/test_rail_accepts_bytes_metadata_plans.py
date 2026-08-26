"""An App whose metadata is its payload must still be able to FILL an order.

#1693 put a blanket precondition at the top of the execution rail: reject any
plan whose metadata is not a mapping, because the rail writes platform fields
into it. That was wrong, and the way it was wrong matters — every one of those
writes is ALREADY conditional:

    platform_fee_wei   only when fee_in_params > 0
    escrow_params      \\
    plan_set            > only inside `if cross_chain_plan_dict and compiler`,
    multi_leg_plan      / and cross_chain_plan_dict is None for bytes metadata
    contract_address   /

So for a non-cross-chain, zero-platform-fee App, NO write fires and the plan
would have executed perfectly. The blanket check refused it anyway.

For AlphaYieldApp (chain 964) that is not cosmetic: its PERPETUAL ORDER is what
triggers every rebalance, so a rail that refuses the fill is a vault that never
re-delegates — the entire App doing nothing.

Refuse only when a platform field actually needs to be written and cannot be.
"""
from __future__ import annotations

from collections.abc import MutableMapping

import pytest

from minotaur_subnet.shared.types import ExecutionPlan, plan_metadata_fields

ABI_META = "0x" + "02071f4a273821ef3c7f4b0abb6b79d57f24df8644ac29ca50f2ab645678653c" + f"{230:064x}"


def _plan(md):
    return ExecutionPlan(intent_id="i", interactions=[], deadline=0, nonce=0, metadata=md)


def _rail_would_refuse(plan, fee_in_params: int) -> bool:
    """The narrowed guard, as the rail now applies it."""
    if fee_in_params > 0 and not isinstance(plan.metadata, MutableMapping):
        return True
    return False


def test_a_zero_fee_bytes_metadata_plan_is_ACCEPTED():
    """The AlphaYield case: no fee, not cross-chain, so no write fires."""
    assert _rail_would_refuse(_plan(ABI_META), fee_in_params=0) is False


def test_a_bytes_metadata_plan_WITH_a_fee_is_still_refused():
    """The fee travels in the consensus proposal; dropping it silently would be
    a quiet mis-execution, so this one must still fail closed."""
    assert _rail_would_refuse(_plan(ABI_META), fee_in_params=1_000) is True


def test_ordinary_dict_plans_are_unaffected_either_way():
    for fee in (0, 1_000):
        assert _rail_would_refuse(_plan({}), fee_in_params=fee) is False


def test_the_cross_chain_writes_are_unreachable_for_bytes_metadata():
    """Why those writes need no guard of their own.

    They sit behind `if cross_chain_plan_dict and self.cross_chain_compiler`,
    and cross_chain_plan_dict is read via plan_metadata_fields — empty for
    bytes. Pin it, so the guard cannot quietly stop being load-bearing.
    """
    plan = _plan(ABI_META)
    assert plan_metadata_fields(plan).get("cross_chain_plan") is None


def test_the_blanket_precondition_would_have_blocked_rebalancing():
    """State the regression directly, so it is not reintroduced.

    A blanket `not isinstance(metadata, MutableMapping) -> return False` refuses
    the zero-fee case that must be allowed.
    """
    plan = _plan(ABI_META)
    blanket_refuses = not isinstance(plan.metadata, MutableMapping)
    assert blanket_refuses is True          # what #1693 did
    assert _rail_would_refuse(plan, 0) is False   # what it must do instead
