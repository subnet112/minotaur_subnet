"""Tests for ``minotaur_subnet.shared.env_check``.

Locks in the operator-friendly startup behavior:
  - Missing/empty required env vars → actionable diagnostic on stderr
    + ``sys.exit(78)`` (EX_CONFIG).
  - All required vars set → silent return.
  - Whitespace-only values count as empty (operators sometimes paste
    blank assignments like ``VALIDATOR_REGISTRY_8453=   ``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.shared import env_check as ec


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip relevant env vars before each test."""
    for name in ec.REQUIRED_REGISTRY_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_silent_return_when_all_required_set(isolated_env):
    isolated_env.setenv("VALIDATOR_REGISTRY_8453", "0xAbC")
    isolated_env.setenv("VALIDATOR_REGISTRY_964", "0xDeF")
    # Must not raise / not call sys.exit
    ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")


def test_exits_when_one_missing(isolated_env):
    isolated_env.setenv("VALIDATOR_REGISTRY_8453", "0xAbC")
    # Leave VALIDATOR_REGISTRY_964 unset
    with pytest.raises(SystemExit) as exc:
        ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")
    assert exc.value.code == 78


def test_exits_when_all_missing(isolated_env):
    with pytest.raises(SystemExit) as exc:
        ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")
    assert exc.value.code == 78


def test_whitespace_only_value_counts_as_missing(isolated_env):
    """Operators sometimes paste ``VALIDATOR_REGISTRY_8453=   `` (spaces).
    Should still trigger the diagnostic — empty after strip is empty."""
    isolated_env.setenv("VALIDATOR_REGISTRY_8453", "    ")
    isolated_env.setenv("VALIDATOR_REGISTRY_964", "0xDeF")
    with pytest.raises(SystemExit) as exc:
        ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")
    assert exc.value.code == 78


def test_diagnostic_includes_missing_var_names(isolated_env, capsys):
    isolated_env.setenv("VALIDATOR_REGISTRY_8453", "0xAbC")
    # 964 unset
    with pytest.raises(SystemExit):
        ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")
    captured = capsys.readouterr()
    # Message goes to stderr; the missing-var name must appear so the
    # operator knows which one was empty
    assert "VALIDATOR_REGISTRY_964" in captured.err
    assert "VALIDATOR_REGISTRY_8453" not in captured.err.split("Empty or unset:")[1].split("\n")[0]


def test_diagnostic_points_at_cp_env_example(isolated_env, capsys):
    """The whole point of this module: tell operators to copy
    .env.example. That string must be in the output."""
    with pytest.raises(SystemExit):
        ec.check_required_env_or_exit(ec.REQUIRED_REGISTRY_ENV, process_name="test")
    captured = capsys.readouterr()
    assert "cp .env.example .env" in captured.err


def test_process_name_appears_in_diagnostic(isolated_env, capsys):
    with pytest.raises(SystemExit):
        ec.check_required_env_or_exit(
            ec.REQUIRED_REGISTRY_ENV, process_name="validator daemon",
        )
    captured = capsys.readouterr()
    assert "validator daemon" in captured.err


def test_custom_required_list(isolated_env):
    """Callers can pass their own list, not just REQUIRED_REGISTRY_ENV."""
    isolated_env.setenv("MY_REQUIRED_VAR", "")
    with pytest.raises(SystemExit) as exc:
        ec.check_required_env_or_exit(["MY_REQUIRED_VAR"], process_name="custom")
    assert exc.value.code == 78


# ── Integration: validator + api startup actually wire the check ─────


def test_validator_main_invokes_check_at_top():
    """Regression guard: validator/main.py must call the env check before
    argparse so the message reaches the operator before any other setup."""
    src = (_REPO_ROOT / "minotaur_subnet" / "validator" / "main.py").read_text()
    assert "check_required_env_or_exit" in src, (
        "validator/main.py must call check_required_env_or_exit() — "
        "operators who forgot .env need a clear diagnostic before the "
        "deeper 'no ValidatorRegistry address' crash"
    )
    # Specifically: must be called inside main(), before argparse runs
    main_def_idx = src.index("def main()")
    argparse_idx = src.index("argparse.ArgumentParser", main_def_idx)
    check_idx = src.index("check_required_env_or_exit", main_def_idx)
    assert main_def_idx < check_idx < argparse_idx, (
        "env check must come AFTER main()'s def and BEFORE argparse"
    )


def test_api_startup_invokes_check_at_top():
    """Regression guard: api/startup.py initialize() must call the env
    check right after Secrets Manager hydration (which may legitimately
    supply the registry values from cloud)."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    assert "check_required_env_or_exit" in src, (
        "api/startup.py initialize() must call check_required_env_or_exit()"
    )
    # Must be inside initialize() and after the Secrets Manager hydration
    init_idx = src.index("async def initialize(")
    sm_idx = src.index("hydrate_env_from_secrets_manager", init_idx)
    check_idx = src.index("check_required_env_or_exit", init_idx)
    assert init_idx < sm_idx < check_idx, (
        "env check must run AFTER Secrets Manager hydration so SM-provided "
        "values don't trigger a false positive"
    )
