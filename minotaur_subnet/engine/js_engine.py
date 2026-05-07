"""
JsExecutionEngine - Core engine for executing JS scoring functions.

This is the primary interface used by validators to score execution plans.
It manages loaded App Intent scoring modules and delegates execution to
the JsSandbox (Node.js subprocess).

Usage:
    engine = JsExecutionEngine(timeout_ms=5000)
    await engine.load_intent("my-swap-app", js_source_code)
    result = await engine.score("my-swap-app", plan, simulation, state)
"""

import logging
from dataclasses import asdict
from typing import Any

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    IntentState,
    ScoreResult,
    SimulationResult,
)

from .context import JsContext
from .sandbox import JsSandbox, JsSandboxError, JsTimeoutError

logger = logging.getLogger(__name__)


class IntentNotLoadedError(Exception):
    """Raised when trying to use an intent that hasn't been loaded."""
    pass


class JsExecutionEngine:
    """Executes JS scoring functions for App Intents on validators.

    The engine maintains a registry of loaded JS scoring modules (keyed by
    app_id). When score() or validate() is called, it builds the appropriate
    context, invokes the JS function via the sandbox, and converts the result
    back to Python dataclasses.
    """

    def __init__(self, timeout_ms: int = 10000, max_memory_mb: int = 128):
        """Initialize the engine.

        Args:
            timeout_ms: Default timeout for JS execution in milliseconds.
            max_memory_mb: Maximum memory for each JS execution process.
        """
        self.timeout_ms = timeout_ms
        self.max_memory_mb = max_memory_mb
        self._sandbox = JsSandbox(timeout_ms=timeout_ms, max_memory_mb=max_memory_mb)

        # Registry of loaded intents: app_id -> js_code
        self._intents: dict[str, str] = {}

        # Cache of intent configs (extracted from JS module on load)
        self._configs: dict[str, dict[str, Any]] = {}

        # Cache of intent manifests (extracted from JS module on load)
        self._manifests: dict[str, dict[str, Any]] = {}

        logger.info(
            "JsExecutionEngine initialized (timeout=%dms, memory=%dMB)",
            timeout_ms,
            max_memory_mb,
        )

    async def load_intent(self, app_id: str, js_code: str) -> None:
        """Load/register a JS scoring function for an app.

        The JS code is validated by attempting to extract its config. If the
        code is malformed or doesn't export the expected interface, an error
        is raised immediately rather than at scoring time.

        Args:
            app_id: Unique identifier for the App Intent.
            js_code: JavaScript source code implementing the AppIntent interface.

        Raises:
            JsSandboxError: If the JS code is invalid or can't be loaded.
        """
        logger.info("Loading intent: %s", app_id)

        # Validate by extracting the config
        config = await self._extract_config(js_code)
        if config is None:
            logger.warning(
                "Intent %s has no config export; loading anyway", app_id
            )
            config = {}

        # Extract manifest (optional — doesn't block loading)
        manifest = await self._extract_manifest(js_code)
        if manifest is not None:
            logger.info("Intent %s has manifest with %d function(s)",
                        app_id, len(manifest.get("intent_functions", [])))

        self._intents[app_id] = js_code
        self._configs[app_id] = config
        self._manifests[app_id] = manifest or {}

        logger.info(
            "Intent loaded: %s (name=%s, version=%s)",
            app_id,
            config.get("name", "unknown"),
            config.get("version", "unknown"),
        )

    async def unload_intent(self, app_id: str) -> None:
        """Unload a scoring function.

        Args:
            app_id: The app to unload.
        """
        removed = self._intents.pop(app_id, None)
        self._configs.pop(app_id, None)
        self._manifests.pop(app_id, None)
        if removed:
            logger.info("Intent unloaded: %s", app_id)
        else:
            logger.warning("Attempted to unload unknown intent: %s", app_id)

    async def score(
        self,
        app_id: str,
        plan: ExecutionPlan,
        simulation: SimulationResult,
        state: IntentState,
    ) -> ScoreResult:
        """Execute the JS scoring function and return the score.

        This is the primary method used by validators. It:
        1. Builds the JS context from simulation/state data
        2. Invokes the JS score() function via the sandbox
        3. Converts the result to a ScoreResult

        Args:
            app_id: Which App Intent to score against.
            plan: The execution plan submitted by a solver.
            simulation: Result of simulating the plan.
            state: Current on-chain state of the intent.

        Returns:
            ScoreResult with the score, validity, and breakdown.
        """
        self._require_loaded(app_id)
        js_code = self._intents[app_id]

        context = JsContext(
            chain_id=state.chain_id,
            contract_address=state.contract_address,
        )
        ctx_dict = context.build_context(simulation, state)

        # Prepare args matching the JS signature: score(plan, state, context)
        plan_dict = _plan_to_dict(plan)
        state_dict = ctx_dict["state"]

        try:
            raw_result = await self._sandbox.execute_async(
                js_code, "score", [plan_dict, state_dict, ctx_dict]
            )
        except JsTimeoutError:
            logger.error("Scoring timed out for intent %s", app_id)
            return ScoreResult(
                score=0.0,
                valid=False,
                reason="JS scoring function timed out",
            )
        except JsSandboxError as exc:
            logger.error("Scoring failed for intent %s: %s", app_id, exc)
            return ScoreResult(
                score=0.0,
                valid=False,
                reason=f"JS scoring error: {exc}",
            )

        return _parse_score_result(raw_result)

    async def validate(
        self,
        app_id: str,
        plan: ExecutionPlan,
        simulation: SimulationResult,
        state: IntentState,
    ) -> ScoreResult:
        """Run JS validation (structural checks before scoring).

        Calls the validate() function exported by the JS module. If the module
        does not export validate(), returns a default valid result.

        Args:
            app_id: Which App Intent to validate against.
            plan: The execution plan to validate.
            simulation: Result of simulating the plan.
            state: Current on-chain state of the intent.

        Returns:
            ScoreResult where score=1.0 if valid, score=0.0 if invalid.
        """
        self._require_loaded(app_id)
        js_code = self._intents[app_id]

        context = JsContext(
            chain_id=state.chain_id,
            contract_address=state.contract_address,
        )
        ctx_dict = context.build_context(simulation, state)

        plan_dict = _plan_to_dict(plan)
        state_dict = ctx_dict["state"]

        try:
            raw_result = await self._sandbox.execute_async(
                js_code, "validate", [plan_dict, state_dict, ctx_dict]
            )
        except JsTimeoutError:
            logger.error("Validation timed out for intent %s", app_id)
            return ScoreResult(
                score=0.0,
                valid=False,
                reason="JS validation function timed out",
            )
        except JsSandboxError as exc:
            # If validate() doesn't exist, treat as valid (optional function)
            if "not found in module.exports" in str(exc):
                logger.debug(
                    "Intent %s has no validate() function; defaulting to valid",
                    app_id,
                )
                return ScoreResult(score=1.0, valid=True)
            logger.error("Validation failed for intent %s: %s", app_id, exc)
            return ScoreResult(
                score=0.0,
                valid=False,
                reason=f"JS validation error: {exc}",
            )

        return _parse_validation_result(raw_result)

    async def should_trigger(
        self,
        app_id: str,
        state: IntentState,
    ) -> bool:
        """For auto-triggered intents: check if conditions are met.

        Calls the shouldTrigger() function exported by the JS module.
        Returns False if the function doesn't exist or throws an error.

        Args:
            app_id: Which App Intent to check.
            state: Current on-chain state of the intent.

        Returns:
            True if the intent should be triggered, False otherwise.
        """
        self._require_loaded(app_id)
        js_code = self._intents[app_id]

        context = JsContext(
            chain_id=state.chain_id,
            contract_address=state.contract_address,
        )
        # For trigger checks, we pass an empty simulation (no plan yet)
        empty_sim = SimulationResult(success=False, gas_used=0)
        ctx_dict = context.build_context(empty_sim, state)

        state_dict = ctx_dict["state"]

        try:
            result = await self._sandbox.execute_async(
                js_code, "shouldTrigger", [state_dict, ctx_dict]
            )
        except JsSandboxError as exc:
            if "not found in module.exports" in str(exc):
                logger.debug(
                    "Intent %s has no shouldTrigger() function; returning False",
                    app_id,
                )
                return False
            logger.error(
                "shouldTrigger failed for intent %s: %s", app_id, exc
            )
            return False

        return bool(result)

    def list_loaded_intents(self) -> list[str]:
        """List currently loaded app IDs."""
        return list(self._intents.keys())

    def get_intent_config(self, app_id: str) -> dict[str, Any] | None:
        """Get the cached config for a loaded intent."""
        return self._configs.get(app_id)

    def get_manifest(self, app_id: str) -> dict[str, Any] | None:
        """Get the cached manifest for a loaded intent."""
        manifest = self._manifests.get(app_id)
        return manifest if manifest else None

    def list_manifests(self) -> dict[str, dict[str, Any]]:
        """Return all cached manifests (non-empty only)."""
        return {k: v for k, v in self._manifests.items() if v}

    # ── Private helpers ──────────────────────────────────────────────────

    def _require_loaded(self, app_id: str) -> None:
        """Raise if the intent isn't loaded."""
        if app_id not in self._intents:
            raise IntentNotLoadedError(
                f"Intent '{app_id}' is not loaded. "
                f"Call load_intent() first. "
                f"Loaded: {list(self._intents.keys())}"
            )

    async def _extract_config(self, js_code: str) -> dict[str, Any] | None:
        """Extract the config object from a JS scoring module.

        This validates that the JS code is parseable and exports something.
        Returns the config dict or None if no config is exported.
        """
        # We wrap the JS code to extract just the config property
        wrapper = js_code + "\n\nmodule.exports = module.exports.config || null;\n"
        try:
            # Use a short timeout for config extraction
            sandbox = JsSandbox(timeout_ms=2000, max_memory_mb=64)
            result = await sandbox.execute_async(
                wrapper, "__extract_config__", []
            )
            return result
        except JsSandboxError:
            # Config extraction via function call won't work with the wrapper
            # approach above. Instead, let's just evaluate and read the config
            # directly from the module exports.
            pass

        # Alternative: evaluate the code and check if module.exports.config exists
        # by wrapping in a function that returns it
        extract_code = (
            js_code
            + "\n\n"
            + "module.exports.__extract_config__ = function() { "
            + "  return module.exports.config || null; "
            + "};\n"
        )
        try:
            sandbox = JsSandbox(timeout_ms=2000, max_memory_mb=64)
            result = await sandbox.execute_async(
                extract_code, "__extract_config__", []
            )
            return result
        except JsSandboxError as exc:
            logger.warning("Could not extract config from JS code: %s", exc)
            return None

    async def _extract_manifest(self, js_code: str) -> dict[str, Any] | None:
        """Extract the manifest object from a JS scoring module.

        Returns the manifest dict or None if no manifest is exported.
        """
        extract_code = (
            js_code
            + "\n\n"
            + "module.exports.__extract_manifest__ = function() { "
            + "  return module.exports.manifest || null; "
            + "};\n"
        )
        try:
            sandbox = JsSandbox(timeout_ms=2000, max_memory_mb=64)
            result = await sandbox.execute_async(
                extract_code, "__extract_manifest__", []
            )
            return result
        except JsSandboxError as exc:
            logger.debug("Could not extract manifest from JS code: %s", exc)
            return None


# ── Module-level helpers ─────────────────────────────────────────────────


def _plan_to_dict(plan: ExecutionPlan) -> dict[str, Any]:
    """Convert an ExecutionPlan to a plain dict for JS consumption.

    Provides both snake_case (Python) and camelCase (JS) keys.
    """
    interactions = []
    for ix in plan.interactions:
        interactions.append(
            {
                "target": ix.target,
                "value": ix.value,
                "call_data": ix.call_data,
                "callData": ix.call_data,
                "chain_id": ix.chain_id,
                "chainId": ix.chain_id,
            }
        )

    return {
        "intent_id": plan.intent_id,
        "intentId": plan.intent_id,
        "interactions": interactions,
        "deadline": plan.deadline,
        "nonce": plan.nonce,
        "metadata": plan.metadata,
    }


def _parse_score_result(raw: Any) -> ScoreResult:
    """Parse a raw JS result into a ScoreResult dataclass.

    Handles various shapes the JS code might return.
    """
    if raw is None:
        return ScoreResult(
            score=0.0,
            valid=False,
            reason="JS score function returned null/undefined",
        )

    if isinstance(raw, (int, float)):
        # Simple numeric score
        return ScoreResult(score=float(raw), valid=True)

    if not isinstance(raw, dict):
        return ScoreResult(
            score=0.0,
            valid=False,
            reason=f"JS score function returned unexpected type: {type(raw).__name__}",
        )

    score = float(raw.get("score", 0.0))
    # Clamp to [0, 1]
    score = max(0.0, min(1.0, score))

    breakdown = raw.get("breakdown", {})
    if not isinstance(breakdown, dict):
        breakdown = {}

    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    return ScoreResult(
        score=score,
        valid=True,
        reason=raw.get("reason", ""),
        breakdown={k: float(v) for k, v in breakdown.items() if isinstance(v, (int, float))},
        metadata=metadata,
    )


def _parse_validation_result(raw: Any) -> ScoreResult:
    """Parse a raw JS validation result into a ScoreResult.

    JS validate() returns { valid: boolean, reason?: string }.
    We map this to ScoreResult for consistency.
    """
    if raw is None:
        return ScoreResult(
            score=0.0,
            valid=False,
            reason="JS validate function returned null/undefined",
        )

    if isinstance(raw, bool):
        return ScoreResult(score=1.0 if raw else 0.0, valid=raw)

    if not isinstance(raw, dict):
        return ScoreResult(
            score=0.0,
            valid=False,
            reason=f"JS validate returned unexpected type: {type(raw).__name__}",
        )

    valid = bool(raw.get("valid", False))
    reason = str(raw.get("reason", ""))

    return ScoreResult(
        score=1.0 if valid else 0.0,
        valid=valid,
        reason=reason,
    )
