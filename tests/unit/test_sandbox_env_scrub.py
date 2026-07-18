"""Tests for the JS-sandbox environment allowlist (SECURITY 2026-07-18).

Root cause of the credential-exfil incident: the Node sandbox subprocess was
spawned with no ``env=``, so it inherited the api container's full environment
— including RELAYER_PRIVATE_KEY and every other secret. Node's ``vm`` is not a
security boundary, so untrusted scoring JS that escapes the sandbox could read
``process.env`` and exfiltrate secrets (the relayer key was stolen this way).

The fix passes a strict, secret-free allowlist as ``env=``. These tests lock in
that (a) the allowlist keeps ONLY the non-secret RPC/domain inputs runner.js
reads and drops all secrets, and (b) end-to-end, a real sandbox escape can no
longer read a secret that is present in the PARENT environment.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.engine.sandbox import JsSandbox, _sandbox_child_env  # noqa: E402

_FAKE_SECRET = "0xDEADBEEF_fake_relayer_key_MUST_NOT_LEAK"

_SECRET_NAMES = [
    "RELAYER_PRIVATE_KEY",
    "VALIDATOR_PRIVATE_KEY",
    "ADMIN_API_KEY",
    "SOLVER_REPO_TOKEN",
    "SOLVER_ROUND_INTERNAL_API_KEY",
    "SUBMISSION_GIT_CLONE_PASSWORD",
    "ONEINCH_API_KEY",
    "ZEROX_API_KEY",
]


def test_child_env_drops_every_secret_keeps_functional(monkeypatch):
    for name in _SECRET_NAMES:
        monkeypatch.setenv(name, "super-secret-" + name)
    monkeypatch.setenv("ANVIL_RPC_URL", "http://anvil:8545")
    monkeypatch.setenv("BASE_RPC_URL", "http://anvil-base:8545")
    monkeypatch.setenv("RPC_URL_1", "http://rpc1")
    monkeypatch.setenv("RPC_URL_8453", "http://rpc8453")
    monkeypatch.setenv("JS_SCORING_ALLOWED_DOMAINS", "example.com,foo.bar")

    env = _sandbox_child_env()

    # Every secret is dropped.
    for name in _SECRET_NAMES:
        assert name not in env, f"{name} must never reach the JS sandbox subprocess"

    # The non-secret inputs runner.js actually reads are preserved verbatim.
    assert env["ANVIL_RPC_URL"] == "http://anvil:8545"
    assert env["BASE_RPC_URL"] == "http://anvil-base:8545"
    assert env["RPC_URL_1"] == "http://rpc1"
    assert env["RPC_URL_8453"] == "http://rpc8453"
    assert env["JS_SCORING_ALLOWED_DOMAINS"] == "example.com,foo.bar"

    # It is a strict allowlist: nothing outside the four kinds gets through.
    allowed = {"ANVIL_RPC_URL", "BASE_RPC_URL", "JS_SCORING_ALLOWED_DOMAINS"}
    for k in env:
        assert k in allowed or k.startswith("RPC_URL_"), f"unexpected var leaked to sandbox: {k}"


def test_child_env_excludes_parent_only_sandbox_vars(monkeypatch):
    # JS_SANDBOX_* are read by the Python parent, never by runner.js.
    monkeypatch.setenv("JS_SANDBOX_MAX_CONCURRENT", "8")
    monkeypatch.setenv("JS_SANDBOX_ACQUIRE_TIMEOUT_SEC", "9.0")
    env = _sandbox_child_env()
    assert "JS_SANDBOX_MAX_CONCURRENT" not in env
    assert "JS_SANDBOX_ACQUIRE_TIMEOUT_SEC" not in env


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.asyncio
async def test_sandbox_escape_cannot_read_secret_from_env(monkeypatch):
    """End-to-end: a secret in the PARENT env is invisible to escaping JS.

    Uses the classic Node ``vm`` escape (reach the host ``process`` via the
    Function constructor). Whether or not the escape reaches a process object,
    the scrubbed child env has no RELAYER_PRIVATE_KEY, so the secret must never
    appear in the result.
    """
    monkeypatch.setenv("RELAYER_PRIVATE_KEY", _FAKE_SECRET)
    sandbox = JsSandbox(timeout_ms=5000, max_memory_mb=64)
    js = (
        "module.exports = { score: function () {"
        "  try {"
        "    var proc = this.constructor.constructor('return process')();"
        "    return { leaked: (proc && proc.env && proc.env.RELAYER_PRIVATE_KEY) || null };"
        "  } catch (e) {"
        "    return { leaked: null, err: String(e) };"
        "  }"
        "} };"
    )
    result = await sandbox.execute_async(js, "score", [])
    assert _FAKE_SECRET not in str(result), f"secret leaked from sandbox env: {result!r}"
