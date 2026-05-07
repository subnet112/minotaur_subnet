"""Tests for AWS Secrets Manager env-hydration."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from minotaur_subnet.api import secrets_loader


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "VALIDATOR_KEY_0", "VALIDATOR_KEY_1", "VALIDATOR_KEY_2",
        "RELAYER_PRIVATE_KEY", "ANTHROPIC_API_KEY",
        "SOLVER_REPO_TOKEN", "MINER_PRIVATE_KEY", "MINER_HOTKEY",
        "SKIP_SECRETS_MANAGER_LOAD",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_skip_env_var_opts_out(monkeypatch):
    monkeypatch.setenv("SKIP_SECRETS_MANAGER_LOAD", "1")
    with patch("boto3.client") as boto_client:
        outcome = secrets_loader.hydrate_env_from_secrets_manager()
    boto_client.assert_not_called()
    assert outcome.env_vars_set == 0


def test_no_boto3_returns_cleanly():
    """Fresh dev envs without boto3 installed shouldn't error at boot."""
    with patch.dict("sys.modules", {"boto3": None}):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()
    assert outcome.env_vars_set == 0


def test_fetches_and_hydrates(monkeypatch):
    validator_payload = {
        "key_0": "0x" + "aa" * 32,
        "key_1": "0x" + "bb" * 32,
        "key_2": "0x" + "cc" * 32,
    }
    relayer_payload = {"relayer_private_key": "0x" + "dd" * 32}
    anthropic_value = "sk-ant-api03-TESTING"

    sm = MagicMock()
    def get_secret(SecretId):
        if SecretId == "minotaur/production/validator-keys":
            return {"SecretString": json.dumps(validator_payload)}
        if SecretId == "minotaur/production/relayer-key":
            return {"SecretString": json.dumps(relayer_payload)}
        if SecretId == "minotaur/production/anthropic-api-key":
            return {"SecretString": anthropic_value}  # raw string, field=None
        raise AssertionError(f"unexpected secret id: {SecretId}")
    sm.get_secret_value = get_secret
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("boto3.client", return_value=sm):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()

    assert outcome.env_vars_set == 5  # 3 validator keys + 1 relayer + 1 anthropic
    assert outcome.secrets_fetched == 3
    assert outcome.secrets_failed == []
    assert os.environ["VALIDATOR_KEY_0"] == validator_payload["key_0"]
    assert os.environ["VALIDATOR_KEY_1"] == validator_payload["key_1"]
    assert os.environ["VALIDATOR_KEY_2"] == validator_payload["key_2"]
    assert os.environ["RELAYER_PRIVATE_KEY"] == relayer_payload["relayer_private_key"]
    assert os.environ["ANTHROPIC_API_KEY"] == anthropic_value


def test_existing_env_preserved(monkeypatch):
    """Operator-set env wins over SM — useful for debugging with a rotated key."""
    monkeypatch.setenv("VALIDATOR_KEY_0", "0x" + "ee" * 32)

    sm = MagicMock()
    sm.get_secret_value = MagicMock(return_value={
        "SecretString": json.dumps({
            "key_0": "0x" + "aa" * 32,
            "key_1": "0x" + "bb" * 32,
            "key_2": "0x" + "cc" * 32,
        }),
    })

    with patch("boto3.client", return_value=sm):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()

    assert os.environ["VALIDATOR_KEY_0"] == "0x" + "ee" * 32  # unchanged
    assert os.environ["VALIDATOR_KEY_1"] == "0x" + "bb" * 32  # from SM
    # Env-set override doesn't count as env_vars_set. 2 validator keys (1 + 2)
    # are hydrated; the others (relayer, anthropic) are in static secrets too
    # but the test's sm stub doesn't provide them so they fail silently.
    assert outcome.env_vars_set >= 2


def test_secret_fetch_failure_falls_back_to_env(monkeypatch, caplog):
    """SM outage must not halt boot — fall through to existing env."""
    monkeypatch.setenv("RELAYER_PRIVATE_KEY", "0x" + "ff" * 32)

    sm = MagicMock()
    sm.get_secret_value = MagicMock(side_effect=ConnectionError("SM unreachable"))
    with patch("boto3.client", return_value=sm):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()

    assert outcome.env_vars_set == 0
    # All static secrets failed (validator, relayer, anthropic = 3)
    assert len(outcome.secrets_failed) == 3
    assert os.environ["RELAYER_PRIVATE_KEY"] == "0x" + "ff" * 32  # env still there


def test_malformed_secret_string_logged_not_raised(monkeypatch):
    sm = MagicMock()
    sm.get_secret_value = MagicMock(return_value={"SecretString": "not-json-{"})
    with patch("boto3.client", return_value=sm):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()
    # validator + relayer expect JSON; anthropic accepts raw. So the raw-string
    # secret parses as ANTHROPIC_API_KEY even with a non-JSON body; the two
    # JSON-field secrets fail.
    assert os.environ.get("ANTHROPIC_API_KEY") == "not-json-{"
    assert len(outcome.secrets_failed) == 2


def test_miner_hotkey_triggers_miner_secret_fetch(monkeypatch):
    """When MINER_HOTKEY is set, loader fetches the miner's own key + PAT."""
    monkeypatch.setenv("MINER_HOTKEY", "alpha")
    monkeypatch.delenv("MINER_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SOLVER_REPO_TOKEN", raising=False)

    sm = MagicMock()
    def get_secret(SecretId):
        if SecretId == "minotaur/production/miner-alpha/key":
            return {"SecretString": '{"signing_private_key": "0x" + ' + '"a"' * 32 + '}'}
        if SecretId == "minotaur/production/miner-alpha/github-pat":
            return {"SecretString": "github_pat_ALPHA_TOKEN"}
        return {"SecretString": "{}"}
    sm.get_secret_value = get_secret

    with patch("boto3.client", return_value=sm):
        outcome = secrets_loader.hydrate_env_from_secrets_manager()

    # github-pat is a raw string (no JSON field), loaded verbatim
    assert os.environ.get("SOLVER_REPO_TOKEN") == "github_pat_ALPHA_TOKEN"
    # The validator secrets (fields that aren't configured) just log warnings
    assert outcome.env_vars_set >= 1


def test_raw_string_secret_for_pat(monkeypatch):
    """PATs are stored as raw strings (no JSON) so field=None uses the whole
    SecretString verbatim."""
    monkeypatch.setenv("MINER_HOTKEY", "charlie")
    monkeypatch.delenv("SOLVER_REPO_TOKEN", raising=False)

    sm = MagicMock()
    def get_secret(SecretId):
        if SecretId == "minotaur/production/miner-charlie/github-pat":
            return {"SecretString": "github_pat_CHARLIE_TOKEN"}
        # For the miner key, return valid JSON so we don't break
        return {"SecretString": '{"signing_private_key": "0xbb"}'}
    sm.get_secret_value = get_secret

    with patch("boto3.client", return_value=sm):
        secrets_loader.hydrate_env_from_secrets_manager()

    assert os.environ.get("SOLVER_REPO_TOKEN") == "github_pat_CHARLIE_TOKEN"
