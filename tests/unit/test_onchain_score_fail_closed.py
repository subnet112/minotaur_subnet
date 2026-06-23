"""#258 — the default-ON, fail-CLOSED on-chain dual-scoring gate.

`onchain_score_fail_closed()` (minotaur_subnet/shared/simulation.py) decides
whether the on-chain veto FAILS CLOSED when a contract is present but the
on-chain score reads back `None`. It is a fleet-uniform security invariant: the
leader (`blockloop/order_processor`) and every follower
(`validator/scoring_engine`) share it, so a typo must never silently weaken a
single validator and split it off the order quorum.

Spec (simulation.py:26-52):
  * unset env                     -> True  (secure default, fail-CLOSED)
  * one of {0, false, no, off}    -> False (explicit break-glass, fail-OPEN),
                                     case-insensitive + surrounding whitespace
                                     stripped
  * anything else (incl. "", "1",
    "true", "yes", garbage)       -> True  (stays fail-CLOSED)

`test_block_loop.py::test_onchain_score_fail_closed_flag` already smoke-tests
unset / "1" / "off" / "0". This file fills the rest of the matrix the safety
invariant depends on: the OTHER fail-open words, case-insensitivity, whitespace
stripping, empty string, garbage, and the `_FAIL_OPEN_VALUES` set contents.
"""

import pytest

from minotaur_subnet.shared.simulation import (
    _FAIL_OPEN_VALUES,
    onchain_score_fail_closed,
)

_ENV = "ONCHAIN_SCORE_FAIL_CLOSED"


def test_fail_open_value_set_is_exactly_the_documented_four():
    """The only values that relax the gate are {0, false, no, off}. Locking the
    set down means a future edit that adds e.g. "" can't silently broaden the
    fail-open surface without tripping this test."""
    assert _FAIL_OPEN_VALUES == frozenset({"0", "false", "no", "off"})


def test_unset_is_fail_closed(monkeypatch):
    """Secure default: no env var at all -> fail-CLOSED (True)."""
    monkeypatch.delenv(_ENV, raising=False)
    assert onchain_score_fail_closed() is True


@pytest.mark.parametrize("value", sorted(_FAIL_OPEN_VALUES))
def test_each_documented_fail_open_value_relaxes_to_false(monkeypatch, value):
    """Every member of _FAIL_OPEN_VALUES is an explicit break-glass -> False."""
    monkeypatch.setenv(_ENV, value)
    assert onchain_score_fail_closed() is False


@pytest.mark.parametrize("value", ["0", "FALSE", "False", "No", "NO", "Off", "OFF"])
def test_fail_open_values_are_case_insensitive(monkeypatch, value):
    """The fail-open match lower()s the raw value, so any casing relaxes."""
    monkeypatch.setenv(_ENV, value)
    assert onchain_score_fail_closed() is False


@pytest.mark.parametrize("value", ["  off  ", "\tfalse", "no\n", " 0 "])
def test_surrounding_whitespace_is_stripped_before_match(monkeypatch, value):
    """A padded compose value (" off ") still relaxes — raw is .strip()ed."""
    monkeypatch.setenv(_ENV, value)
    assert onchain_score_fail_closed() is False


@pytest.mark.parametrize(
    "value",
    [
        "1",        # the canonical explicit-ON value
        "true",     # truthy, but NOT a fail-open keyword
        "yes",      # truthy, but NOT a fail-open keyword
        "on",       # NOT in the set ("off" is, "on" is not)
        "enabled",
        "disabled", # tempting near-miss, must NOT fail-open
        "2",
        "-1",
        "falsey",   # superstring of "false", must NOT match
        "offf",     # superstring of "off", must NOT match
        "garbage",
        "none",
        "null",
    ],
)
def test_other_and_garbage_values_stay_fail_closed(monkeypatch, value):
    """Anything outside the documented set keeps the secure default (True).
    A typo can never silently weaken the gate."""
    monkeypatch.setenv(_ENV, value)
    assert onchain_score_fail_closed() is True


def test_empty_string_stays_fail_closed(monkeypatch):
    """An empty value (e.g. `ONCHAIN_SCORE_FAIL_CLOSED=` in compose) is NOT a
    member of _FAIL_OPEN_VALUES, so it must remain fail-CLOSED (True), not
    accidentally read as 'falsey'."""
    monkeypatch.setenv(_ENV, "")
    assert onchain_score_fail_closed() is True


def test_whitespace_only_stays_fail_closed(monkeypatch):
    """A whitespace-only value strips to "" -> not fail-open -> fail-CLOSED."""
    monkeypatch.setenv(_ENV, "   ")
    assert onchain_score_fail_closed() is True


def test_return_type_is_strictly_bool(monkeypatch):
    """Callers compare with `is`/use it as a gate; guarantee a real bool both
    when fail-closed and fail-open."""
    monkeypatch.delenv(_ENV, raising=False)
    assert isinstance(onchain_score_fail_closed(), bool)
    monkeypatch.setenv(_ENV, "off")
    assert isinstance(onchain_score_fail_closed(), bool)
