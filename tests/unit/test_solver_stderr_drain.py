"""The solver's stderr is drained so a chatty solver can't deadlock the pipe.

Prod symptom (2026-06-26): the genesis champion solver kept dying mid-benchmark
with "Solver process is not running", so it produced 0 reference quotes and scored
0 on every scenario — making the whole benchmark meaningless. Root cause: the
solver subprocess is launched with stderr=PIPE but nothing read it; the verbose
baseline solver filled the ~64KB kernel pipe buffer and BLOCKED on its next stderr
write, stalling quoting until the per-command timeout killed it, after which every
later scenario saw a dead process. Reproduced: the real image blocked after ~158KB
of unread stderr.

These tests pin that the session drains stderr (no deadlock) and surfaces the tail
in the crash error.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.orchestrator import SolverCrashedError, SolverSession


async def _spawn(script: str) -> SolverSession:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return SolverSession(proc, label="test")


# Per stdin line: emit ~4.2KB to stderr, then a JSON response to stdout. Without
# draining stderr this deadlocks once the pipe fills (~64KB, i.e. ~16 requests).
_FLOODER = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stderr.write('noise ' * 700 + '\\n'); sys.stderr.flush()\n"
    "    sys.stdout.write('{\"success\": true, \"result\": null}\\n'); sys.stdout.flush()\n"
)


@pytest.mark.asyncio
async def test_chatty_solver_does_not_deadlock():
    # 60 requests => ~250KB of stderr, far past the 64KB pipe buffer. Without the
    # drain this blocks and the per-command timeout fires; wait_for makes a
    # regression FAIL loudly instead of hanging the suite.
    session = await _spawn(_FLOODER)
    try:
        for _ in range(60):
            await asyncio.wait_for(session.initialize({}), timeout=20)
        # The drain kept a tail of the noisy stderr for diagnostics.
        assert session._stderr_tail
        assert "noise" in session._stderr_snapshot()
    finally:
        await session.kill()


@pytest.mark.asyncio
async def test_crash_error_carries_stderr_tail():
    # A solver that logs a fatal line then exits without responding: the crash
    # error must carry the captured stderr instead of swallowing it.
    script = (
        "import sys, time\n"
        "sys.stderr.write('boom: fatal init error\\n'); sys.stderr.flush()\n"
        "time.sleep(0.4)\n"           # stay alive so the drain captures the line
        "sys.stdin.readline()\n"      # then consume the request
        "sys.exit(1)\n"               # ...and exit without a response
    )
    session = await _spawn(script)
    await asyncio.sleep(0.25)  # let the drain capture 'boom' during the sleep
    with pytest.raises(SolverCrashedError) as ei:
        await asyncio.wait_for(session.initialize({}), timeout=20)
    assert "boom: fatal init error" in str(ei.value)
    await session.kill()


@pytest.mark.asyncio
async def test_no_stderr_pipe_is_a_noop():
    # A session whose process has no stderr (stderr=None) must not crash on
    # construction — the drain simply no-ops.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys\nfor _ in sys.stdin: sys.stdout.write('{\"success\": true}\\n'); sys.stdout.flush()\n",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
    )
    session = SolverSession(proc, label="test")
    assert session._stderr_snapshot() == "no stderr captured"
    await session.kill()
