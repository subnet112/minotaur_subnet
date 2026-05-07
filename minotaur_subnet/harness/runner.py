"""In-container runner for IntentSolver benchmarking.

This script runs INSIDE the solver Docker container. It:
1. Imports the solver from /app/solver/solver.py (SOLVER_CLASS)
2. Reads JSON commands from stdin (one per line)
3. Dispatches each command to the appropriate IntentSolver method
4. Writes JSON responses to stdout (one per line)

The runner is the solver-side counterpart to the host-side orchestrator.
Together they implement the benchmarking protocol defined in protocol.py.

Usage (inside container):
    python -m minotaur_subnet.harness.runner

Or directly:
    python /app/harness/runner.py

The solver module is expected at /app/solver/solver.py with a module-level
SOLVER_CLASS attribute pointing to an IntentSolver subclass.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from minotaur_subnet.harness.protocol import (
    Command,
    HarnessRequest,
    HarnessResponse,
    dict_to_intent,
    dict_to_snapshot,
    dict_to_state,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver

logger = logging.getLogger(__name__)

# Default solver module path inside containers
DEFAULT_SOLVER_PATH = "/app/solver/solver.py"


class SolverRunner:
    """Manages the lifecycle of a solver instance inside a container.

    Reads JSON commands from an input stream, dispatches to the solver,
    and writes JSON responses to an output stream.
    """

    def __init__(
        self,
        solver: IntentSolver,
        input_stream: Any = None,
        output_stream: Any = None,
    ) -> None:
        self.solver = solver
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout
        self._running = True

    def run(self) -> None:
        """Main loop: read commands, dispatch, respond."""
        logger.info("SolverRunner started, waiting for commands...")

        for line in self._input:
            line = line.strip()
            if not line:
                continue

            try:
                request = HarnessRequest.from_json(line)
            except (json.JSONDecodeError, KeyError) as exc:
                self._send(HarnessResponse.fail(
                    f"Invalid request: {exc}", "ProtocolError"
                ))
                continue

            response = self._dispatch(request)
            self._send(response)

            if request.command == Command.SHUTDOWN:
                break

        logger.info("SolverRunner shutting down")

    def _dispatch(self, request: HarnessRequest) -> HarnessResponse:
        """Route a command to the appropriate handler."""
        start = time.monotonic()

        try:
            handler = self._handlers.get(request.command)
            if handler is None:
                return HarnessResponse.fail(
                    f"Unknown command: {request.command}", "ProtocolError"
                )
            result = handler(self, request.params)
            elapsed = time.monotonic() - start
            logger.debug(
                "Command %s completed in %.1fms",
                request.command, elapsed * 1000,
            )
            return HarnessResponse.ok(result)

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error(
                "Command %s failed after %.1fms: %s",
                request.command, elapsed * 1000, exc,
            )
            return HarnessResponse.fail(
                str(exc), type(exc).__name__,
            )

    def _send(self, response: HarnessResponse) -> None:
        """Write a response as a JSON line to stdout."""
        self._output.write(response.to_json() + "\n")
        self._output.flush()

    # ── Command handlers ─────────────────────────────────────────────────

    def _handle_initialize(self, params: dict[str, Any]) -> None:
        config = params.get("config", {})
        self.solver.initialize(config)
        return None

    def _handle_metadata(self, params: dict[str, Any]) -> dict[str, Any]:
        meta = self.solver.metadata()
        return asdict(meta)

    def _handle_generate_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        intent = dict_to_intent(params["intent"])
        state = dict_to_state(params["state"])
        snapshot = dict_to_snapshot(params["snapshot"])

        plan = self.solver.generate_plan(intent, state, snapshot)
        return _plan_to_dict(plan)

    def _handle_check_trigger(self, params: dict[str, Any]) -> bool:
        intent = dict_to_intent(params["intent"])
        state = dict_to_state(params["state"])
        snapshot = dict_to_snapshot(params["snapshot"])

        return self.solver.check_trigger(intent, state, snapshot)

    def _handle_on_benchmark_start(self, params: dict[str, Any]) -> None:
        intent_count = params.get("intent_count", 0)
        self.solver.on_benchmark_start(intent_count)
        return None

    def _handle_on_benchmark_end(self, params: dict[str, Any]) -> None:
        results = params.get("results", [])
        self.solver.on_benchmark_end(results)
        return None

    def _handle_serialize_state(self, params: dict[str, Any]) -> str:
        state_bytes = self.solver.serialize_state()
        return base64.b64encode(state_bytes).decode("ascii")

    def _handle_restore_state(self, params: dict[str, Any]) -> None:
        state_b64 = params.get("state_b64", "")
        if state_b64:
            state_bytes = base64.b64decode(state_b64)
            self.solver.restore_state(state_bytes)
        return None

    def _handle_quote(self, params: dict[str, Any]) -> dict[str, Any]:
        intent = dict_to_intent(params["intent"])
        state = dict_to_state(params["state"])
        snapshot = dict_to_snapshot(params["snapshot"])

        result = self.solver.quote(intent, state, snapshot)
        return asdict(result) if result is not None else {}

    def _handle_supported_tokens(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        chain_id = int(params["chain_id"])
        # Optional on the solver — return empty list if not implemented so
        # the host can surface a clean 501 instead of a hard failure.
        fn = getattr(self.solver, "supported_tokens", None)
        if fn is None:
            return []
        return list(fn(chain_id) or [])

    def _handle_shutdown(self, params: dict[str, Any]) -> None:
        self._running = False
        return None

    # Handler dispatch table
    _handlers: dict[str, Any] = {
        Command.INITIALIZE: _handle_initialize,
        Command.METADATA: _handle_metadata,
        Command.GENERATE_PLAN: _handle_generate_plan,
        Command.CHECK_TRIGGER: _handle_check_trigger,
        Command.QUOTE: _handle_quote,
        Command.SUPPORTED_TOKENS: _handle_supported_tokens,
        Command.ON_BENCHMARK_START: _handle_on_benchmark_start,
        Command.ON_BENCHMARK_END: _handle_on_benchmark_end,
        Command.SERIALIZE_STATE: _handle_serialize_state,
        Command.RESTORE_STATE: _handle_restore_state,
        Command.SHUTDOWN: _handle_shutdown,
    }


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    """Convert an ExecutionPlan to a JSON-safe dict.

    Provides both snake_case and camelCase keys for JS interop.
    """
    interactions = []
    for ix in plan.interactions:
        interactions.append({
            "target": ix.target,
            "value": ix.value,
            "call_data": ix.call_data,
            "callData": ix.call_data,
            "chain_id": ix.chain_id,
            "chainId": ix.chain_id,
        })

    return {
        "intent_id": plan.intent_id,
        "intentId": plan.intent_id,
        "interactions": interactions,
        "deadline": plan.deadline,
        "nonce": plan.nonce,
        "metadata": plan.metadata,
    }


def load_solver(solver_path: str = DEFAULT_SOLVER_PATH) -> IntentSolver:
    """Load a solver class from a Python file and instantiate it.

    The file must define a module-level SOLVER_CLASS attribute pointing
    to an IntentSolver subclass.

    Args:
        solver_path: Path to the solver.py file.

    Returns:
        An uninitialized IntentSolver instance.

    Raises:
        FileNotFoundError: If solver_path doesn't exist.
        AttributeError: If SOLVER_CLASS is not defined.
        TypeError: If SOLVER_CLASS is not an IntentSolver subclass.
    """
    path = Path(solver_path)
    if not path.exists():
        raise FileNotFoundError(f"Solver not found: {solver_path}")

    spec = importlib.util.spec_from_file_location("solver_module", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {solver_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    solver_class = getattr(module, "SOLVER_CLASS", None)
    if solver_class is None:
        raise AttributeError(
            f"Module {solver_path} must define SOLVER_CLASS attribute"
        )

    if not (isinstance(solver_class, type) and issubclass(solver_class, IntentSolver)):
        raise TypeError(
            f"SOLVER_CLASS must be an IntentSolver subclass, got {solver_class}"
        )

    return solver_class()


def main() -> None:
    """Entry point for the in-container runner."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # Logs go to stderr, protocol goes to stdout
    )

    solver_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOLVER_PATH

    try:
        solver = load_solver(solver_path)
        logger.info("Loaded solver from %s", solver_path)
    except Exception as exc:
        # Fatal: can't even load the solver
        error_response = HarnessResponse.fail(
            f"Failed to load solver: {exc}", type(exc).__name__
        )
        sys.stdout.write(error_response.to_json() + "\n")
        sys.stdout.flush()
        sys.exit(1)

    runner = SolverRunner(solver)
    runner.run()


if __name__ == "__main__":
    main()
