"""The App-scoring JS sandbox runs UNTRUSTED code in a REAL V8 isolate
(isolated-vm), NOT Node's ``vm`` (which is explicitly not a security boundary).

Incident 2026-07-18: a malicious App escaped ``vm.createContext`` via an injected
helper's ``.constructor`` — ``ethCall.constructor('return process')()`` — reached
the host ``process.env`` + ``require('fs'|'child_process')`` + the docker socket,
exfiltrated every secret, and spawned a host-filesystem-mounting container.
isolated-vm removes the host realm entirely: no ``process``, no ``require``, no
``Buffer``, no network. These probes run against the ACTUAL ``runner.js`` the
validator ships, over its real stdin/stdout JSON protocol.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
_ISOLATED_VM = _RUNNER.parent / "node_modules" / "isolated-vm"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None or not _ISOLATED_VM.exists(),
    reason=(
        "node or engine/node_modules/isolated-vm not present — run "
        "`npm --prefix minotaur_subnet/engine install --omit=dev` first"
    ),
)

# A canary secret placed in the CHILD env: an escape would read it from
# process.env. isolated-vm must make it unreachable.
_CANARY = "s3cr3t-should-never-leak-9f1c2"


def _run(js_code: str, fn: str = "score", args=None) -> dict:
    payload = json.dumps({"jsCode": js_code, "functionName": fn, "args": args or []})
    proc = subprocess.run(
        [_NODE, str(_RUNNER)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
            "FAKE_RELAYER_PRIVATE_KEY": _CANARY,
        },
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    return json.loads(line)


def _score(body: str, args=None) -> dict:
    """Wrap a function body as ``module.exports = { score: <body> }`` and run it."""
    return _run(f"module.exports = {{ score: {body} }};", "score", args)


class TestScoringStillWorks:
    """isolated-vm keeps every capability real scoring code needs."""

    def test_sync(self):
        out = _score("function(x){ return x * 2 + Math.sqrt(16); }", [10])
        assert out["success"] is True and out["result"] == 24

    def test_async_and_bigint(self):
        out = _score(
            "async function(){ return (BigInt('10000000000000000000') * 2n).toString(); }"
        )
        assert out["success"] is True and out["result"] == "20000000000000000000"

    def test_rpc_bridges_present(self):
        out = _score(
            "function(){ return typeof ethCall + ',' + typeof httpGet + ',' + typeof ethBlockNumber; }"
        )
        assert out["result"] == "function,function,function"

    def test_console_does_not_break_result(self):
        out = _score("function(){ console.log('a'); console.error('b'); return 'ok'; }")
        assert out["result"] == "ok"

    def test_missing_export_is_a_clean_error(self):
        out = _run("module.exports = { other: function(){ return 1; } };", "score")
        assert out["success"] is False and "not found" in out["error"]


class TestNoHostEscape:
    """No path from guest JS reaches the host realm."""

    def test_incident_constructor_gadget_blocked(self):
        # The EXACT gadget the attacker used against vm.createContext.
        out = _score(
            "function(){ try { var p = this.constructor.constructor('return process')();"
            " return 'ESCAPED:' + typeof p; } catch(e) { return 'blocked'; } }"
        )
        assert "ESCAPED" not in str(out.get("result")), out

    def test_function_constructor_yields_no_process(self):
        out = _score(
            "function(){ try { return typeof Function('return process')(); }"
            " catch(e) { return 'threw'; } }"
        )
        assert out.get("result") in ("undefined", "threw"), out

    @pytest.mark.parametrize(
        "expr",
        ["require", "process", "Buffer", "global.process", "globalThis.process", "module.require"],
    )
    def test_host_globals_absent(self, expr):
        out = _score(
            f"function(){{ try {{ return typeof ({expr}); }} catch(e) {{ return 'threw'; }} }}"
        )
        assert out.get("result") in ("undefined", "threw"), f"{expr} leaked: {out}"

    def test_cannot_require_fs(self):
        out = _score(
            "function(){ try { require('fs'); return 'GOT_FS'; } catch(e) { return 'blocked'; } }"
        )
        assert out.get("result") != "GOT_FS", out

    def test_env_canary_unreachable(self):
        # Even with FAKE_RELAYER_PRIVATE_KEY set in the child env, the guest
        # cannot read process.env — the escape's whole purpose.
        out = _score(
            "function(){ try { return String(process.env.FAKE_RELAYER_PRIVATE_KEY); }"
            " catch(e) { return 'blocked'; } }"
        )
        assert _CANARY not in str(out.get("result")), out

    def test_ssrf_docker_socket_blocked(self):
        # The host-side httpGet enforces the SSRF blocklist. Assert the ACTUAL
        # rejection reason — not merely != 'REACHED', which also holds when the
        # bridge itself is broken ("Reference is not a function"), the regression
        # that once slipped past a weaker assertion.
        out = _score(
            "async function(){ try { await httpGet('http://docker-socket-proxy:2375/version');"
            " return 'REACHED'; } catch(e) { return e.message; } }"
        )
        r = str(out.get("result"))
        assert "internal/private" in r and "Reference" not in r, out


def _run_full(js_code, fn="score", args=None):
    """Like _run but also returns the child's stderr (for the console bridge)."""
    payload = json.dumps({"jsCode": js_code, "functionName": fn, "args": args or []})
    proc = subprocess.run(
        [_NODE, str(_RUNNER)], input=payload, capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
             "JS_SCORING_ALLOWED_DOMAINS": ""},
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    return json.loads(line), proc.stderr


class TestHostBridgesWork:
    """The RPC/HTTP bridges must REACH the real host logic. A regression (e.g. a
    double-wrapped ivm.Reference, or a host rejection escaping the isolate) makes
    every ethCall/httpGet throw 'Reference is not a function' / crash the runner,
    silently mis-scoring every submission that verifies chain state. These assert
    the real host code path is hit, not just that the wrapper exists."""

    def test_httpget_hits_ssrf_blocklist(self):
        out = _score(
            "async function(){ try{ await httpGet('http://docker-socket-proxy:2375/x'); return 'REACHED'; }"
            " catch(e){ return e.message; } }"
        )
        r = str(out.get("result"))
        assert "internal/private" in r and "Reference" not in r, out

    def test_httpget_deny_by_default(self):
        out = _score(
            "async function(){ try{ await httpGet('https://example.com/'); return 'REACHED'; }"
            " catch(e){ return e.message; } }"
        )
        assert "no allowed domains" in str(out.get("result")), out

    def test_ethcall_hits_rpc_logic(self):
        out = _score(
            "async function(){ try{ return await ethCall(999,'0x0','0x'); } catch(e){ return e.message; } }"
        )
        assert "No RPC URL" in str(out.get("result")), out

    def test_ethblocknumber_hits_rpc_logic(self):
        out = _score(
            "async function(){ try{ return await ethBlockNumber(999); } catch(e){ return e.message; } }"
        )
        assert "No RPC URL" in str(out.get("result")), out

    def test_console_reaches_stderr(self):
        out, err = _run_full(
            "module.exports = { score: function(){ console.log('CANARY_9f3'); console.error('E_9f3'); return 'ok'; } };"
        )
        assert out.get("result") == "ok" and "CANARY_9f3" in err and "E_9f3" in err, (out, err)
