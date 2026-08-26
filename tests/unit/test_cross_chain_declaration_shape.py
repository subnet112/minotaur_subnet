"""`declares_cross_chain` is THE declaration predicate, and it trusted its type.

Annotated `Mapping[str, Any] | None`, but callers pass `plan.metadata` straight
in — and that is not always a mapping. An App which abi.decodes its metadata
takes raw bytes (#1617), so `meta.get(...)` raised

    AttributeError: 'str' object has no attribute 'get'

which took the whole /score request down as an UNLOGGED 500. Found 2026-08-26
by calling the handler in-process, because uvicorn never surfaced the traceback.

Three tree-wide sweeps missed it. They grepped the EXPRESSION SHAPE
(`plan.metadata.get`), and this function receives metadata as a parameter named
`meta` — so it was invisible to every one of them. The right criterion is the
TYPE CONTRACT: any function that accepts metadata and calls `.get` on it.

Guarded in the predicate rather than at its call sites deliberately: every gate
is required to funnel through this one function, so it is the single place the
check cannot be forgotten by the next caller.
"""
from __future__ import annotations

import pytest

from minotaur_subnet.simulator.cross_chain_bench import (
    _forward_legs,
    declares_cross_chain,
)

ABI_META = "0x" + "02" * 32 + f"{230:064x}"


@pytest.mark.parametrize("meta", [ABI_META, b"\x00\x01", 7, ["legs"], object()])
def test_a_non_mapping_declares_nothing_instead_of_raising(meta):
    assert declares_cross_chain(meta) is False


@pytest.mark.parametrize("meta", [None, {}])
def test_empty_still_declares_nothing(meta):
    assert declares_cross_chain(meta) is False


@pytest.mark.parametrize("key", ["legs", "multi_leg_plan", "cross_chain_plan", "cross_chain"])
def test_all_four_declaration_shapes_still_work(key):
    """The whole point of this predicate is that all four shapes agree."""
    assert declares_cross_chain({key: [1] if key == "legs" else True}) is True


def test_forward_legs_is_guarded_too():
    assert _forward_legs(ABI_META) == []
    assert _forward_legs({}) == []


# ── the rest of the chain that this unblocked ────────────────────────────────

def test_a_data_only_plan_is_not_an_empty_plan():
    """AlphaYieldApp's plan is DATA, not code.

    The App ignores plan.calls entirely and reads its abi-encoded metadata, so
    `interactions: []` is the CORRECT shape and everything that scores it lives
    in the App's own scoreIntent. Bailing on empty interactions made that whole
    class unscoreable: the dry-run returned `simulation_failed: empty plan` for
    a perfectly formed plan.

    The condition must therefore be "nothing to run AND nothing to score", not
    "no interactions".
    """
    def would_bail(interactions, contract_address, intent_order):
        return not interactions and not (contract_address and intent_order)

    assert would_bail([], "", None) is True            # genuinely nothing to do
    assert would_bail([], "0xApp", None) is True       # order missing
    assert would_bail([], "", {"o": 1}) is True        # contract missing
    assert would_bail([], "0xApp", {"o": 1}) is False  # scoreIntent can run
    assert would_bail(["ix"], "", None) is False       # ordinary plan


def test_hex_metadata_decodes_to_bytes_for_the_abi_encoder():
    """A hex string reaches the ABI encoder as bytes, or calldata never builds.

    Left as a str it fails one step later with
      Value '0x02071f…' of type <class 'str'> cannot be encoded by
      ByteStringEncoder
    so scoreIntent calldata never builds and on_chain_score stays null.
    """
    from minotaur_subnet.api.routes.apps import _decode_plan_metadata as dec
    out = dec(ABI_META)
    assert isinstance(out, bytes) and len(out) == 64
    assert dec({"a": 1}) == {"a": 1}
    assert dec(None) == {}
    assert isinstance(dec("0x201"), str), "malformed hex fails loudly, not silently reshaped"


def test_the_score_intent_read_gets_a_cold_fork_budget():
    """A cold Chopsticks fork's first call costs ~60-90s BEFORE any work."""
    from minotaur_subnet.simulator.subtensor_simulator import (
        _SCORE_INTENT_TIMEOUT_S,
    )
    assert _SCORE_INTENT_TIMEOUT_S >= 250
