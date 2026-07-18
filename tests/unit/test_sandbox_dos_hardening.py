"""Hardening regressions for the JS scoring sandbox (post red-team, 2026-07-18).

Two fixes, both exercised against the shipped ``runner.js`` / ``sandbox.py``:

1. **``SandboxOverloadedError`` is backpressure, not a score of 0.** On the
   consensus proposal path it must PROPAGATE (opt-in ``propagate_overload=True``)
   so ``proposal_handler`` maps it to a retryable 503 instead of silently scoring
   a legitimate champion 0 and rejecting it under load. Every other caller keeps
   the graceful ``score=0`` default.

2. **A malicious App can SIGSEGV the isolated-vm runner on demand** (an
   isolated-vm 5.0.1 ``dispose()``-with-pending-async bug). The child must drop
   core dumps (no disk/apport churn), the runner must cap its own result, and the
   host must refuse oversized output.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from minotaur_subnet.engine import sandbox as sandbox_mod
from minotaur_subnet.engine.js_engine import JsExecutionEngine
from minotaur_subnet.engine.sandbox import (
    JsRuntimeError,
    JsSandbox,
    SandboxOverloadedError,
    _drop_core_dumps,
)
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    IntentState,
    SimulationResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "minotaur_subnet" / "engine" / "runner.js"
_ISOLATED_VM = _RUNNER.parent / "node_modules" / "isolated-vm"
_NODE = shutil.which("node")

_needs_node = pytest.mark.skipif(
    _NODE is None or not _ISOLATED_VM.exists(),
    reason="node or engine/node_modules/isolated-vm not present",
)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(intent_id="i", interactions=[], deadline=9999999999, nonce=0)


def _sim() -> SimulationResult:
    return SimulationResult(success=True, gas_used=1000)


def _state() -> IntentState:
    return IntentState(
        contract_address="0x" + "AA" * 20, chain_id=1, nonce=0, owner="0x" + "BB" * 20
    )


def _engine_raising(exc: Exception) -> JsExecutionEngine:
    """An engine with one fake app whose sandbox call always raises ``exc``."""
    eng = JsExecutionEngine()
    eng._intents["app"] = "module.exports={score:function(){return 1}}"
    eng._configs["app"] = {}
    eng._manifests["app"] = {}
    eng._sandbox.execute_async = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
    return eng


# ── Fix 1: overload propagation ──────────────────────────────────────────────


class TestOverloadPropagation:
    def test_score_swallows_overload_by_default(self):
        eng = _engine_raising(SandboxOverloadedError("saturated"))
        res = asyncio.run(eng.score("app", _plan(), _sim(), _state()))
        assert res.score == 0.0 and not res.valid
        assert "overload" in res.reason.lower()

    def test_score_propagates_overload_when_opted_in(self):
        eng = _engine_raising(SandboxOverloadedError("saturated"))
        with pytest.raises(SandboxOverloadedError):
            asyncio.run(
                eng.score("app", _plan(), _sim(), _state(), propagate_overload=True)
            )

    def test_validate_propagates_when_opted_in(self):
        eng = _engine_raising(SandboxOverloadedError("saturated"))
        with pytest.raises(SandboxOverloadedError):
            asyncio.run(
                eng.validate("app", _plan(), _sim(), _state(), propagate_overload=True)
            )

    def test_should_trigger_propagates_when_opted_in(self):
        eng = _engine_raising(SandboxOverloadedError("saturated"))
        with pytest.raises(SandboxOverloadedError):
            asyncio.run(eng.should_trigger("app", _state(), propagate_overload=True))

    def test_opt_in_does_not_reraise_ordinary_js_errors(self):
        # The flag ONLY re-raises overload. A normal JS/runtime error must still
        # degrade gracefully to score=0 (not become an unhandled exception).
        eng = _engine_raising(JsRuntimeError("boom"))
        res = asyncio.run(
            eng.score("app", _plan(), _sim(), _state(), propagate_overload=True)
        )
        assert res.score == 0.0 and not res.valid

    def test_consensus_score_via_js_propagates_overload(self):
        # The consensus path (_score_via_js) must opt in, so an overload reaches
        # proposal_handler's 503 map instead of being scored 0.
        from minotaur_subnet.validator import scoring_engine as se_mod

        eng = _engine_raising(SandboxOverloadedError("saturated"))
        se = se_mod.ScoringEngine.__new__(se_mod.ScoringEngine)
        se.js_engine = eng
        with pytest.raises(SandboxOverloadedError):
            asyncio.run(se._score_via_js("app", _plan(), _sim(), _state()))

    def test_overload_propagates_through_verify_and_score_proposal(self, monkeypatch):
        """End-to-end guard: an overload from ``_score_via_js`` must PROPAGATE
        out of ``verify_and_score_proposal`` (→ proposal_handler's 503 map), NOT
        be swallowed by the broad ``except Exception`` at the re-score site — which
        would fall back to the leader's asserted score and rubber-stamp it. This
        drives the full wrapper, not the leaf method, because the swallow lives one
        frame up from ``_score_via_js``.
        """
        import uuid
        from unittest.mock import MagicMock

        from minotaur_subnet.validator import scoring_engine as se_mod

        monkeypatch.setenv("FOLLOWER_PROPOSAL_RESIMULATE", "0")  # use leader-sim path
        monkeypatch.setattr(
            "minotaur_subnet.consensus.app_registry_cache.is_registered_app",
            lambda *a, **k: True,
        )

        # scoring_engine resolves the class via a CALL-TIME
        # ``from minotaur_subnet.engine import SandboxOverloadedError``; resolve
        # it the SAME way here so a prior test's ``importlib.reload`` of the
        # engine (which mints a distinct class object) can't desync identity.
        import minotaur_subnet.engine as engine_pkg

        overload_cls = engine_pkg.SandboxOverloadedError

        se = se_mod.ScoringEngine.__new__(se_mod.ScoringEngine)
        se.store = MagicMock()
        se.store.get_deployment.return_value = MagicMock(contract_address="0x" + "11" * 20)
        se.js_engine = MagicMock()
        se.js_engine.list_loaded_intents.return_value = ["app"]
        # The re-score raises overload (as the real engine now does with
        # propagate_overload=True under sandbox saturation).
        se._score_via_js = AsyncMock(side_effect=overload_cls("saturated"))

        body = {
            "order_id": f"o-{uuid.uuid4()}",
            "plan_hash": f"h-{uuid.uuid4()}",
            "app_id": "app",
            "score": 0.9,  # leader's asserted score — must NOT be rubber-stamped
            "chain_id": 1,
            "submitted_by": "0x" + "22" * 20,
            "plan": {
                "interactions": [],
                "intent_id": "app",
                "deadline": 0,
                "nonce": 0,
                "metadata": {},
            },
            "simulation": {"success": True, "gas_used": 1000},
        }

        with pytest.raises(overload_cls):
            asyncio.run(se.verify_and_score_proposal(body, score_threshold=0.5))


# ── Fix 2: DoS defang (core dumps, result/stdout size) ───────────────────────


class TestCoreDumpDefang:
    def test_child_spawned_with_core_dumps_disabled(self):
        async def go() -> str:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "-c",
                "import resource;print(resource.getrlimit(resource.RLIMIT_CORE))",
                stdout=asyncio.subprocess.PIPE,
                preexec_fn=_drop_core_dumps,
            )
            out, _ = await proc.communicate()
            return out.decode().strip()

        assert asyncio.run(go()) == "(0, 0)"


@_needs_node
class TestResultSizeCap:
    def _run(self, js: str) -> dict:
        payload = json.dumps({"jsCode": js, "functionName": "score", "args": []})
        proc = subprocess.run(
            [_NODE, str(_RUNNER)], input=payload, capture_output=True, text=True, timeout=30
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_oversized_result_becomes_bounded_error(self):
        out = self._run(
            "module.exports={score:function(){return 'x'.repeat(5*1024*1024)}}"
        )
        assert out["success"] is False and out["errorType"] == "ResultTooLarge"

    def test_normal_result_still_returned(self):
        out = self._run("module.exports={score:function(){return {score:0.5}}}")
        assert out["success"] is True and out["result"]["score"] == 0.5


@_needs_node
class TestHostStdoutGuard:
    def test_host_refuses_oversized_stdout(self, monkeypatch):
        # runner.js caps its own result at 4 MB; drop the HOST cap below a normal
        # return so the defence-in-depth guard trips on output runner.js emitted.
        monkeypatch.setattr(sandbox_mod, "_MAX_STDOUT_BYTES", 500)
        # The process-wide sandbox semaphore is lazily bound to the FIRST event
        # loop that awaits it; a prior test in the suite may have bound it to a
        # now-closed loop. Reset so it rebinds to this asyncio.run() loop.
        monkeypatch.setattr(sandbox_mod, "_SANDBOX_SEMAPHORE", None)

        async def go():
            sb = JsSandbox(timeout_ms=5000)
            return await sb.execute_async(
                "module.exports={score:function(){return 'y'.repeat(2000)}}", "score", []
            )

        # Resolve JsRuntimeError from the live module: a prior test may have
        # importlib.reload()'d the engine, replacing the class object that
        # sandbox.py raises (bare ``JsRuntimeError`` here would be the stale one).
        with pytest.raises(sandbox_mod.JsRuntimeError, match="oversized"):
            asyncio.run(go())
