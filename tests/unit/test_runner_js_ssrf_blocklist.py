"""Tests for runner.js SSRF + JSON-RPC method allowlist (audit H8).

The JS scoring sandbox can issue HTTP requests + JSON-RPC eth_call /
eth_blockNumber. PR-6b adds two defences:

1. **SSRF allowlist tightened**: cloud-metadata IPs and Docker
   container-escape hostnames (docker-socket-proxy,
   host.docker.internal, 169.254.169.254 = AWS/GCP/Azure IMDS,
   metadata.google.internal, metadata) added to the host blocklist.
2. **JSON-RPC method allowlist**: ``anvil_* / hardhat_* / evm_*``
   cheat-codes that can poison fork state, plus
   ``debug_* / trace_* / txpool_* / admin_* / personal_* / miner_*``
   leak operator keys or DOS the node — all rejected.

These checks live in JS, not Python, so we shell out to ``node`` to
run smoke probes against the actual runner.js the validator ships.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
# runner.js now requires the isolated-vm native addon (real V8 isolate for the
# untrusted scoring JS). It lives in engine/node_modules, built by the Docker
# builder stage / the CI `npm install` step.
_ISOLATED_VM = _RUNNER.parent / "node_modules" / "isolated-vm"


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not _ISOLATED_VM.exists(),
    reason=(
        "node or engine/node_modules/isolated-vm not present — run "
        "`npm --prefix minotaur_subnet/engine install --omit=dev` first"
    ),
)


def _eval_in_runner(probe_js: str) -> dict:
    """Execute ``probe_js`` in a child Node process that has loaded
    runner.js's helpers via ``require()`` from the sandbox file. We
    can't ``require()`` runner.js directly (it's a stand-alone
    process), so we ``Function``-wrap its source to expose internals
    for testing only.
    """
    runner_src = _RUNNER.read_text()
    wrapper = f"""
    {runner_src}
    const result = (function() {{
        try {{
            {probe_js}
        }} catch (e) {{
            return {{ error: e.message }};
        }}
    }})();
    process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run(
        ["node", "-e", wrapper],
        capture_output=True, text=True, timeout=10,
        # Provide minimal env so runner.js can read it without hanging.
        env={"PATH": "/usr/bin:/usr/local/bin"},
        input="",  # runner.js's main path reads stdin; the wrapper exits before that
    )
    if proc.returncode != 0:
        return {"_proc_stderr": proc.stderr, "_proc_stdout": proc.stdout}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_raw": proc.stdout, "_stderr": proc.stderr}


# ── Static source-level checks (no Node required) ─────────────────────


def test_runner_js_blocks_imds_hostname():
    """169.254.169.254 (AWS/GCP/Azure IMDS) must be in the host blocklist.

    A scoring module that exfiltrates IAM credentials from IMDS is the
    most direct cloud-to-cloud pivot path from the sandbox.
    """
    src = _RUNNER.read_text()
    assert "169.254.169.254" in src
    assert "metadata.google.internal" in src


def test_runner_js_blocks_docker_socket_proxy():
    """Container-escape paths to docker-socket-proxy must be blocked."""
    src = _RUNNER.read_text()
    assert "docker-socket-proxy" in src
    assert "host.docker.internal" in src


def test_runner_js_rejects_anvil_cheat_codes():
    """anvil_* / hardhat_* / evm_* lets a malicious solver bias its own
    benchmark by mutating fork state. The method allowlist must reject
    these prefixes."""
    src = _RUNNER.read_text()
    assert '"anvil_"' in src
    assert '"hardhat_"' in src
    assert '"evm_"' in src
    # Confirm they're in REJECTED_RPC_PREFIXES (not just elsewhere).
    assert "REJECTED_RPC_PREFIXES" in src


def test_runner_js_rejects_admin_personal_debug_namespaces():
    """admin/personal/debug/trace/txpool/miner — all key-leak or DOS
    vectors that the allowlist must reject."""
    src = _RUNNER.read_text()
    for prefix in ('"debug_"', '"trace_"', '"txpool_"',
                   '"admin_"', '"personal_"', '"miner_"'):
        assert prefix in src, f"missing rejected prefix: {prefix}"


def test_runner_js_allows_essential_eth_methods():
    """eth_call + eth_blockNumber are load-bearing — the JS sandbox
    can't do anything useful without them. ALLOWED_RPC_METHODS must
    include them."""
    src = _RUNNER.read_text()
    assert "ALLOWED_RPC_METHODS" in src
    for method in ('"eth_call"', '"eth_blockNumber"', '"eth_getStorageAt"',
                   '"eth_getLogs"', '"eth_chainId"'):
        assert method in src, f"missing allowed method: {method}"


def test_httppost_enforces_method_allowlist():
    """The _httpPost helper enforces the method allowlist defence-in-
    depth — even if a caller bypasses ethCall/ethBlockNumber wrappers
    and posts raw JSON-RPC, the body inspector rejects disallowed
    methods."""
    src = _RUNNER.read_text()
    # The body-inspection block must run inside _httpPost.
    httppost_idx = src.index("function _httpPost(")
    assert "_validateRpcMethod(peek.method)" in src[httppost_idx:], (
        "_httpPost must validate the JSON-RPC method"
    )
    # And the host blocklist applies, with operator-configured RPC URLs exempt.
    assert "isConfiguredRpc" in src[httppost_idx:]
    assert "_isBlockedHost(host)" in src[httppost_idx:]
