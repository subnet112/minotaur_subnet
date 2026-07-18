"""
JsSandbox - Sandboxed JS execution via Node.js subprocess.

Executes JavaScript code in a real V8 isolate (isolated-vm) with:
- No host realm at all (no require/import, no process, no Buffer, no network)
- Configurable timeout (enforced by both Python asyncio and the isolate)
- JSON-based communication over stdin/stdout

The actual sandboxing is done by runner.js using isolated-vm — a separate V8
heap with no path back to the host process. Node's ``vm.createContext`` (the
prior mechanism) is explicitly NOT a security boundary and was escaped in the
2026-07-18 incident; do not reintroduce it for untrusted code.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import asdict
from typing import Any

try:
    import resource  # POSIX-only; used to disable core dumps in the Node child
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows dev box)
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Path to the Node.js runner script (co-located with this module)
_RUNNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.js")


class JsSandboxError(Exception):
    """Base exception for sandbox errors."""
    pass


class JsTimeoutError(JsSandboxError):
    """Raised when JS execution exceeds the timeout."""
    pass


class JsRuntimeError(JsSandboxError):
    """Raised when JS code throws an error during execution."""
    pass


class SandboxOverloadedError(JsSandboxError):
    """Raised when the sandbox concurrency cap is saturated.

    Caller should map this to HTTP 503 (Service Unavailable) so the leader
    can retry, rather than crashing the daemon or queuing unbounded work.
    Audit H9: prevents a proposal flood from spawning unbounded Node.js
    subprocesses (each capped at ~128 MB heap).
    """
    pass


# Process-wide concurrency cap on Node subprocess spawns. Both the
# order-consensus path (validator/scoring_engine.py) and the champion-
# consensus path (api/routes/submissions/) invoke JsSandbox; one
# semaphore caps total in-flight subprocesses so a flood can't take
# down the host.
#
# Each subprocess now holds TWO heaps: the Node process heap
# (--max-old-space-size, ~128 MB) AND the isolated-vm isolate's own heap
# (ivm memoryLimit = 128 MB, runner.js) ≈ ~256 MB peak per subprocess. At the
# default cap of 4 that is ~1 GB peak — survivable on the 4 GB prod box
# alongside the 7 Python services, but bump JS_SANDBOX_MAX_CONCURRENT via env
# only on boxes that can absorb the larger working set.
_SANDBOX_CONCURRENCY = int(os.environ.get("JS_SANDBOX_MAX_CONCURRENT", "4"))
_SANDBOX_ACQUIRE_TIMEOUT_SEC = float(
    os.environ.get("JS_SANDBOX_ACQUIRE_TIMEOUT_SEC", "5.0")
)
_SANDBOX_SEMAPHORE: asyncio.Semaphore | None = None
_SANDBOX_SEMAPHORE_LOCK = asyncio.Lock()

# Hard ceiling on bytes read back from the Node child's stdout. runner.js already
# caps its own serialized result (MAX_RESULT_BYTES = 4 MB), so this is a
# defence-in-depth guard against a runaway/oversized response ballooning the api
# process memory — comfortably above any legitimate scoring result.
_MAX_STDOUT_BYTES = int(
    os.environ.get("JS_SANDBOX_MAX_STDOUT_BYTES", str(8 * 1024 * 1024))
)


def _drop_core_dumps() -> None:
    """``preexec_fn`` for the Node child — disable core dumps.

    A malicious App's scoring JS can deterministically SIGSEGV the isolated-vm
    runner (a ``dispose()``-with-pending-async bug in isolated-vm 5.0.1): the JSON
    result line is flushed BEFORE the crash so scoring stays correct, but without
    this every such submission would also write a core file (disk pressure + apport
    churn) and is trivially repeatable. ``setrlimit(RLIMIT_CORE, 0)`` is a single
    async-signal-safe syscall — safe from ``preexec_fn`` — and is scoped to the
    child, so the api process's own core dumps are unaffected. Best-effort.
    """
    if resource is not None:
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            pass


# ── SECURITY: sandbox subprocess environment allowlist ──────────────────────
# ``create_subprocess_exec`` inherits the FULL parent environment by default.
# The api container env holds RELAYER_PRIVATE_KEY, VALIDATOR_PRIVATE_KEY,
# ADMIN_API_KEY, SOLVER_REPO_TOKEN, SUBMISSION_GIT_CLONE_PASSWORD, etc. This
# env-scrub is DEFENCE-IN-DEPTH: runner.js now runs guest JS in a real V8
# isolate (isolated-vm), which has no host realm to escape into, but stripping
# the child's secrets means even a hypothetical addon/host bug can't hand
# scoring JS a credential. We pass an EXPLICIT allowlist as ``env=`` so the Node
# child sees ONLY the non-secret inputs runner.js reads — the per-chain RPC
# endpoints + the HTTP domain allowlist. Everything else (all secrets) is
# dropped. runner.js needs nothing more: node is exec'd by absolute path
# (``shutil.which`` in the parent), requires only built-ins + the isolated-vm
# native addon (resolved from ``engine/node_modules`` by Node relative to the
# runner.js path — no NODE_PATH), and takes its heap cap via a CLI flag — so
# PATH/NODE_OPTIONS are unneeded.
_SANDBOX_ENV_PASSTHROUGH = frozenset(
    {"ANVIL_RPC_URL", "BASE_RPC_URL", "JS_SCORING_ALLOWED_DOMAINS"}
)
_SANDBOX_ENV_RPC_RE = re.compile(r"^RPC_URL_\d+$")  # per-chain override, e.g. RPC_URL_8453


def _sandbox_child_env() -> dict[str, str]:
    """The MINIMAL, secret-free environment for the Node sandbox subprocess.

    Returns only the RPC endpoint URLs and the HTTP domain allowlist that
    ``runner.js`` reads from ``process.env`` — never any credential. This is a
    full replacement env (not additive), so the child inherits NOTHING else.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k in _SANDBOX_ENV_PASSTHROUGH or _SANDBOX_ENV_RPC_RE.match(k)
    }


async def _get_sandbox_semaphore() -> asyncio.Semaphore:
    """Lazy double-checked construction of the process-wide semaphore.

    Constructing eagerly at import time would bind it to whichever event
    loop happened to import this module first; that has bitten us before
    when tests use a separate loop. Build on first await instead.
    """
    global _SANDBOX_SEMAPHORE
    if _SANDBOX_SEMAPHORE is None:
        async with _SANDBOX_SEMAPHORE_LOCK:
            if _SANDBOX_SEMAPHORE is None:
                _SANDBOX_SEMAPHORE = asyncio.Semaphore(_SANDBOX_CONCURRENCY)
    return _SANDBOX_SEMAPHORE


class JsSandbox:
    """Sandboxed JS execution environment using Node.js subprocess.

    Each call to execute() or execute_async() spawns a fresh Node.js process,
    ensuring complete isolation between invocations. Within that process the JS
    code runs inside an isolated-vm isolate (a separate V8 heap) with no access
    to Node built-ins or the host realm.
    """

    def __init__(self, timeout_ms: int = 5000, max_memory_mb: int = 128):
        """Initialize the sandbox.

        Args:
            timeout_ms: Maximum execution time in milliseconds.
            max_memory_mb: Maximum memory for the Node.js process (V8 heap limit).
        """
        self.timeout_ms = timeout_ms
        self.max_memory_mb = max_memory_mb
        self._node_path = self._find_node()

    @staticmethod
    def _find_node() -> str:
        """Locate the Node.js binary."""
        node = shutil.which("node")
        if node is None:
            raise JsSandboxError(
                "Node.js is required but not found on PATH. "
                "Install Node.js (v18+) to use the JS execution engine."
            )
        return node

    def execute(self, js_code: str, function_name: str, args: list[Any]) -> Any:
        """Execute a JS function synchronously (blocking).

        Creates a new event loop if needed. Prefer execute_async() when
        running inside an existing async context.
        """
        return asyncio.get_event_loop().run_until_complete(
            self.execute_async(js_code, function_name, args)
        )

    async def execute_async(
        self, js_code: str, function_name: str, args: list[Any]
    ) -> Any:
        """Execute a JS function in the sandbox asynchronously.

        Acquires the process-wide concurrency semaphore before spawning
        the Node subprocess. See ``_SANDBOX_CONCURRENCY`` for the cap and
        rationale (audit H9).

        Args:
            js_code: The JavaScript module source code (must set module.exports).
            function_name: Name of the exported function to call.
            args: Arguments to pass to the function (must be JSON-serializable).

        Returns:
            The return value from the JS function (deserialized from JSON).

        Raises:
            JsTimeoutError: If execution exceeds timeout_ms.
            JsRuntimeError: If the JS code throws an error.
            SandboxOverloadedError: If the process-wide concurrency cap is
                saturated (configurable via JS_SANDBOX_MAX_CONCURRENT).
            JsSandboxError: For other failures (Node not found, invalid input, etc.).
        """
        sem = await _get_sandbox_semaphore()
        try:
            await asyncio.wait_for(
                sem.acquire(), timeout=_SANDBOX_ACQUIRE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            raise SandboxOverloadedError(
                f"JS sandbox saturated (cap={_SANDBOX_CONCURRENCY}, "
                f"acquire timeout={_SANDBOX_ACQUIRE_TIMEOUT_SEC}s) — "
                "refusing further proposals"
            )
        try:
            return await self._do_execute_async(js_code, function_name, args)
        finally:
            sem.release()

    async def _do_execute_async(
        self, js_code: str, function_name: str, args: list[Any]
    ) -> Any:
        """Inner implementation — see ``execute_async`` for semantics."""
        # Prepare the payload for runner.js
        payload = {
            "jsCode": js_code,
            "functionName": function_name,
            "args": _make_json_safe(args),
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        # Build the Node.js command with memory limit
        node_args = [
            f"--max-old-space-size={self.max_memory_mb}",
            _RUNNER_PATH,
        ]

        timeout_seconds = self.timeout_ms / 1000.0

        try:
            proc = await asyncio.create_subprocess_exec(
                self._node_path,
                *node_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # SECURITY: strict allowlist — the child must NEVER inherit the
                # container's secrets (see _sandbox_child_env). Without this the
                # Node process holds RELAYER_PRIVATE_KEY et al. and a vm escape
                # exfiltrates them.
                env=_sandbox_child_env(),
                # Disable core dumps in the child: untrusted scoring JS can
                # deterministically SIGSEGV the runner (see _drop_core_dumps).
                preexec_fn=_drop_core_dumps if resource is not None else None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=payload_bytes),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                raise JsTimeoutError(
                    f"JS execution timed out after {self.timeout_ms}ms"
                )

        except JsTimeoutError:
            raise
        except OSError as exc:
            raise JsSandboxError(f"Failed to start Node.js process: {exc}") from exc

        # Log any stderr output (JS console.log/warn/error)
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                for line in stderr_text.split("\n"):
                    logger.debug("JS stderr: %s", line)

        # Parse the JSON response from runner.js
        if not stdout:
            raise JsSandboxError(
                "Node.js process produced no output. "
                f"Exit code: {proc.returncode}"
            )

        # Defence-in-depth: runner.js caps its own result, but never parse an
        # oversized blob (a runaway response ballooning api memory).
        if len(stdout) > _MAX_STDOUT_BYTES:
            raise JsRuntimeError(
                f"JS sandbox produced oversized output ({len(stdout)} bytes > "
                f"{_MAX_STDOUT_BYTES}-byte cap) — refusing to parse"
            )

        stdout_text = stdout.decode("utf-8", errors="replace").strip()

        try:
            response = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise JsSandboxError(
                f"Failed to parse Node.js output as JSON: {exc}. "
                f"Raw output: {stdout_text[:500]}"
            ) from exc

        if not response.get("success"):
            error_msg = response.get("error", "Unknown JS error")
            error_type = response.get("errorType", "RuntimeError")
            if error_type == "TimeoutError":
                raise JsTimeoutError(f"JS execution timed out: {error_msg}")
            raise JsRuntimeError(f"JS {error_type}: {error_msg}")

        return response.get("result")


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert dataclasses and other non-JSON types to dicts/primitives."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _make_json_safe(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(item) for item in obj]
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    # Fallback: try str()
    return str(obj)
