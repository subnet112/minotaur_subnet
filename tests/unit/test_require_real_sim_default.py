"""Env-matrix unit tests for ``require_real_sim_default`` (harness/orchestrator.py).

``require_real_sim_default`` is the fail-CLOSED simulation gate. A champion can
only be adopted on REAL Anvil simulation scores, never on the fabricated mock
(``~min*1.05`` success, gameable into a passing score). It is consensus-relevant:
the result MUST be uniform across validators, so the env-parsing precedence has
to be exact and predictable.

The canonical 5 cases (prod-default-on, testnet-unset-off, prod-empty-env-on,
prod-explicit-off, testnet-explicit-on) are already covered in
``test_adopt_rule.py``. This file pins the *edge* of the parser that those do
NOT exercise:

  * ``LOCAL_TESTNET`` activates the relaxed branch ONLY for the exact stripped
    literal ``"1"`` — every other value (``"0"``, ``"true"``, ``"yes"``) falls
    THROUGH to the fail-closed prod branch.
  * the truthy set for ``BENCHMARK_REQUIRE_REAL_SIM`` is exactly
    ``{1, true, yes, on}``, case-insensitive, with surrounding whitespace
    stripped; anything else (``"2"``, ``"no"``, ``"false"``, garbage) is FALSE.
  * whitespace-only / blank ``LOCAL_TESTNET`` does NOT relax the gate.

Read against orchestrator.py:107-118.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.harness.orchestrator import require_real_sim_default


def _set(monkeypatch, local_testnet, require_real_sim):
    """Apply the two env vars; ``None`` means delete (absent from environ)."""
    if local_testnet is None:
        monkeypatch.delenv("LOCAL_TESTNET", raising=False)
    else:
        monkeypatch.setenv("LOCAL_TESTNET", local_testnet)
    if require_real_sim is None:
        monkeypatch.delenv("BENCHMARK_REQUIRE_REAL_SIM", raising=False)
    else:
        monkeypatch.setenv("BENCHMARK_REQUIRE_REAL_SIM", require_real_sim)


# ── LOCAL_TESTNET only activates on the exact stripped literal "1" ────────────


@pytest.mark.parametrize(
    "local_testnet",
    ["0", "2", "true", "yes", "on", "TESTNET", "false", " ", "10", "11", "01"],
)
def test_local_testnet_non_one_falls_through_to_prod_fail_closed(
    monkeypatch, local_testnet
):
    """Any LOCAL_TESTNET value that isn't the stripped literal "1" must NOT relax
    the gate: with no BENCHMARK_REQUIRE_REAL_SIM it falls through to prod -> True.

    This is the safety-critical edge: a typo'd / truthy-looking LOCAL_TESTNET
    must fail CLOSED, not silently open the gate."""
    _set(monkeypatch, local_testnet, None)
    assert require_real_sim_default() is True


def test_local_testnet_blank_falls_through_to_prod(monkeypatch):
    """Empty-string LOCAL_TESTNET (set but blank) is not "1" -> prod branch."""
    _set(monkeypatch, "", None)
    assert require_real_sim_default() is True


def test_local_testnet_whitespace_padded_one_activates_testnet(monkeypatch):
    """LOCAL_TESTNET is ``.strip()``-ed, so " 1 " DOES count as the testnet
    literal -> relaxed branch -> with no override, OFF."""
    _set(monkeypatch, "  1  ", None)
    assert require_real_sim_default() is False


# ── BENCHMARK_REQUIRE_REAL_SIM truthy spellings (prod branch) ─────────────────


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_prod_explicit_truthy_spellings_on(monkeypatch, val):
    """All canonical truthy spellings on prod -> True."""
    _set(monkeypatch, None, val)
    assert require_real_sim_default() is True


@pytest.mark.parametrize("val", ["TRUE", "True", "Yes", "YES", "On", "ON", "tRuE"])
def test_prod_truthy_is_case_insensitive(monkeypatch, val):
    """``.lower()`` makes the truthy match case-insensitive on prod -> True."""
    _set(monkeypatch, None, val)
    assert require_real_sim_default() is True


@pytest.mark.parametrize("val", [" 1 ", "\t1\n", "  true  ", " on"])
def test_prod_truthy_is_whitespace_stripped(monkeypatch, val):
    """Surrounding whitespace is stripped before the truthy check -> True."""
    _set(monkeypatch, None, val)
    assert require_real_sim_default() is True


@pytest.mark.parametrize(
    "val", ["0", "no", "false", "off", "2", "disabled", "n", "y", "enable", "yess"]
)
def test_prod_non_truthy_values_are_off(monkeypatch, val):
    """On prod the empty string is the ONLY non-truthy value that flips to ON
    (the empty-env fix). Every other non-truthy value -> False (fail OPEN to the
    mock is the *explicit* operator choice, not an accidental garbage default)."""
    _set(monkeypatch, None, val)
    assert require_real_sim_default() is False


def test_prod_whitespace_only_value_is_on(monkeypatch):
    """A whitespace-only override strips to "" -> the empty-env path -> ON.

    The empty-env fix keys on the *stripped* value being "", so "   " must be
    treated identically to "" and default the gate ON (fail closed)."""
    _set(monkeypatch, None, "   ")
    assert require_real_sim_default() is True


# ── testnet branch: explicit override required to turn the gate back ON ───────


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON", "  Yes  "])
def test_testnet_explicit_truthy_turns_gate_on(monkeypatch, val):
    """Under LOCAL_TESTNET=1 the gate is OFF by default but an explicit truthy
    BENCHMARK_REQUIRE_REAL_SIM (any spelling) turns it back ON."""
    _set(monkeypatch, "1", val)
    assert require_real_sim_default() is True


@pytest.mark.parametrize("val", ["", "0", "no", "false", "off", "garbage", "   "])
def test_testnet_empty_or_falsy_override_stays_off(monkeypatch, val):
    """Under LOCAL_TESTNET=1 the empty string and any non-truthy value leave the
    gate OFF. NOTE: unlike the prod branch, the testnet branch has NO empty-env
    promotion — "" stays OFF here. This asymmetry is the documented testnet
    default (testnet configs may run with no Anvil simulator)."""
    _set(monkeypatch, "1", val)
    assert require_real_sim_default() is False


# ── full default: nothing set at all -> fail closed ──────────────────────────


def test_no_envs_at_all_defaults_fail_closed(monkeypatch):
    """The production posture with a totally clean environment: both vars absent
    -> the gate is ON (real sim required). This is the single most important
    invariant: an operator who configures nothing gets the SAFE default."""
    _set(monkeypatch, None, None)
    assert require_real_sim_default() is True


def test_returns_strict_bool(monkeypatch):
    """The gate is consensus-relevant and consumed as a bool; the return must be
    an actual ``bool`` (a truthy non-bool would diverge under ``is True``
    comparisons across validators)."""
    _set(monkeypatch, None, None)
    result = require_real_sim_default()
    assert isinstance(result, bool)
    _set(monkeypatch, "1", None)
    assert isinstance(require_real_sim_default(), bool)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
