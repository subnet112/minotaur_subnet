"""Strategy tester — validates generated strategy code before submission.

Tests generated strategy code by:
1. Writing to a temp file and importing via load_strategy()
2. Verifying STRATEGY_CLASS export and APP_ID match
3. Running generate_plan() against synthetic intents
4. Validating plan structure
5. Optionally scoring the plan via the validator's JS scoring endpoint
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import tempfile
from pathlib import Path
from typing import Any

from minotaur_subnet.sdk.strategy import Strategy
from minotaur_subnet.sdk.intent_solver import MarketSnapshot
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    ExecutionPlan,
    IntentState,
    TriggerType,
)
from minotaur_subnet.harness.screening import _validate_plan_structure
from minotaur_subnet.harness.snapshot import build_synthetic_snapshot
from minotaur_subnet.miner.agent.app_discovery import AppContext

logger = logging.getLogger(__name__)


_TRANSIENT_FAILURE_PATTERNS: tuple[str, ...] = (
    "Anvil unavailable",
    "Connection refused",
    "Connection reset",
    "ConnectionError",
    "ClientConnectorError",
    "TimeoutError",
    "timed out",
    "ServerDisconnectedError",
    "503 Service Unavailable",
    "502 Bad Gateway",
    "504 Gateway Timeout",
    "Scoring request failed",
)


def _is_transient_failure(message: str) -> bool:
    """True when the score message points to infra noise (Anvil down, RPC
    flap, network blip), not a real strategy bug. Callers use this to
    skip the submission gate rather than penalize the WIP for an outage.
    """
    if not message:
        return False
    low = message.lower()
    for pat in _TRANSIENT_FAILURE_PATTERNS:
        if pat.lower() in low:
            return True
    return False


_ETH_FALLBACK_PARAMS: dict[str, Any] = {
    "input_token":  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
    "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "input_amount": "1000000000000000000",
}
_FAKE_CONTRACT = "0x5aAdFB43eF8dAF45DD80F4676345b7676f1D70e3"


def _pick_test_fixture(
    app_context: AppContext | None,
    scenario_name: str | None = None,
) -> tuple[int, str, dict[str, Any], str, str, int | None]:
    """Resolve (chain_id, contract, params, intent_function, scenario_name,
    fork_block) for a single synthetic test plan.

    The 6th element ``fork_block`` is None for manifest scenarios (simulator
    will reset to latest head) and an int for historical-order replays
    (simulator will rewind to that block so pool prices match the order's
    creation time).

    When ``scenario_name`` is given, the named scenario is returned (matched
    case-sensitively against the manifest's benchmark_scenarios). Otherwise
    the first chain-matching scenario is returned — the legacy behaviour.

    Falls back to Ethereum defaults + a fake contract when the app_context
    is missing, so callers without AppContext still get something runnable.
    """
    if app_context is None:
        return 1, _FAKE_CONTRACT, dict(_ETH_FALLBACK_PARAMS), "execute", "", None

    chain_id = (app_context.supported_chains or [1])[0]
    contract = app_context.contract_address or _FAKE_CONTRACT

    manifest = app_context.manifest or {}
    scenarios = manifest.get("benchmark_scenarios", []) or []

    if scenario_name:
        for scen in scenarios:
            if scen.get("name") != scenario_name:
                continue
            chains = scen.get("chains") or []
            if chains and chain_id not in chains:
                continue
            if scen.get("params"):
                return (
                    chain_id,
                    contract,
                    dict(scen["params"]),
                    scen.get("intent_function") or "execute",
                    scen.get("name") or "",
                    None,
                )

    # Default: first chain-matching scenario.
    for scen in scenarios:
        chains = scen.get("chains") or []
        if chain_id in chains and scen.get("params"):
            return (
                chain_id,
                contract,
                dict(scen["params"]),
                scen.get("intent_function") or "execute",
                scen.get("name") or "",
                None,
            )
    # Fall back to the first intent function's example_params.
    for fn in manifest.get("intent_functions", []) or []:
        ex = fn.get("example_params")
        if ex:
            return chain_id, contract, dict(ex), fn.get("name") or "execute", "example", None
    # Manifest present but no usable params — keep chain+contract correct
    # but seed Ethereum-style token params as a degraded fallback.
    return chain_id, contract, dict(_ETH_FALLBACK_PARAMS), "execute", "", None


def _all_test_fixtures(
    app_context: AppContext | None,
    historical_scenarios: list[dict[str, Any]] | None = None,
) -> list[tuple[int, str, dict[str, Any], str, str, int | None]]:
    """Return every chain-matching benchmark_scenario as a test fixture,
    PLUS any historical-order scenarios supplied by the caller.

    Used by the score-all path so the miner can benchmark its strategy
    the same way the live validator will. The live benchmark has two
    stages: (1) manifest benchmark_scenarios, (2) replay of sampled
    historical filled orders. Without Stage-2 coverage here, Claude
    iterates to "aggregate 0.84" on Stage-1 while Stage-2 replays
    score 0 across the board and drag the real benchmark down to 0.25.

    The caller (e.g. mcp_server.score_strategy_all) is responsible for
    fetching historical scenarios from the validator API and passing
    them in — this module intentionally avoids network calls.

    Falls back to the single _pick_test_fixture result when no
    benchmark_scenarios match the app's chain and no historical
    scenarios are supplied, so callers always get at least one fixture.
    """
    if app_context is None and not historical_scenarios:
        single = _pick_test_fixture(None)
        return [single]

    chain_id = (app_context.supported_chains or [1])[0] if app_context else 1
    contract = (app_context.contract_address if app_context else None) or _FAKE_CONTRACT
    manifest = (app_context.manifest if app_context else None) or {}
    scenarios = manifest.get("benchmark_scenarios", []) or []

    matching: list[tuple[int, str, dict[str, Any], str, str, int | None]] = []

    # Stage 1: manifest benchmark_scenarios. fork_block=None means
    # simulate at upstream latest head.
    for scen in scenarios:
        chains = scen.get("chains") or []
        if chains and chain_id not in chains:
            continue
        if not scen.get("params"):
            continue
        matching.append((
            chain_id,
            contract,
            dict(scen["params"]),
            scen.get("intent_function") or "execute",
            scen.get("name") or "",
            None,
        ))

    # Stage 2: replay of sampled historical filled orders. Each order
    # carries its original `block_number`; we rewind the anvil fork to
    # that block before simulating so pool prices match the state when
    # the order was actually filled. Without this rewind, the swap
    # succeeds at current-head prices even when the original
    # min_output_amount can't be delivered at the historical price —
    # miner-side score falsely reads ~0.6 while the live validator
    # (which uses historical-fork semantics) scores 0.0.
    for order in (historical_scenarios or []):
        params = order.get("params")
        if not isinstance(params, dict):
            continue
        ord_id = order.get("order_id", "")
        label = f"hist:{ord_id}" if ord_id else "hist:?"
        try:
            block_num = int(order["block_number"]) if order.get("block_number") is not None else None
        except (ValueError, TypeError):
            block_num = None
        matching.append((
            int(order.get("chain_id", chain_id) or chain_id),
            contract,
            dict(params),
            order.get("intent_function", "swap") or "swap",
            label,
            block_num,
        ))

    if not matching:
        matching.append(_pick_test_fixture(app_context))
    return matching


def _state_params(state: IntentState) -> dict[str, Any]:
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    return state.raw_params_view()


def load_strategy(strategy_path: str) -> Strategy:
    """Load a strategy class from a Python file and instantiate it.

    The file must define a module-level STRATEGY_CLASS attribute pointing
    to a Strategy subclass.

    Args:
        strategy_path: Path to the strategy .py file.

    Returns:
        An instantiated Strategy.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        AttributeError: If STRATEGY_CLASS is not defined.
        TypeError: If STRATEGY_CLASS is not a Strategy subclass.
    """
    path = Path(strategy_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy not found: {strategy_path}")

    spec = importlib.util.spec_from_file_location("strategy_module", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {strategy_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    strategy_class = getattr(module, "STRATEGY_CLASS", None)
    if strategy_class is None:
        raise AttributeError(
            f"Module {strategy_path} must define STRATEGY_CLASS attribute"
        )

    if not (isinstance(strategy_class, type) and issubclass(strategy_class, Strategy)):
        raise TypeError(
            f"STRATEGY_CLASS must be a Strategy subclass, got {strategy_class}"
        )

    return strategy_class()


class StrategyTester:
    """Tests generated strategy code before accepting it.

    Performs import, APP_ID verification, plan structural validation,
    and optional JS scoring via the validator API.
    """

    def _generate_test_plan(
        self,
        code: str,
        expected_app_id: str,
        app_context: AppContext | None = None,
        scenario_name: str | None = None,
    ) -> tuple[bool, str, ExecutionPlan | None, IntentState | None]:
        """Generate a plan from strategy code using synthetic data.

        When ``app_context`` is provided, the synthetic state is built to
        match the app's actual deployment: chain_id comes from
        supported_chains, contract_address from the deployment, and token
        params from the manifest's benchmark_scenarios / example_params.
        Without this, any app on a chain other than Ethereum scores 0
        because the strategy is called with wrong-chain token addresses.

        Returns (passed, message, plan_or_none, state_or_none).
        """
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, prefix="strategy_",
            ) as f:
                f.write(code)
                tmp_path = f.name

            try:
                strategy = load_strategy(tmp_path)
            except Exception as exc:
                return False, f"Import failed: {exc}", None, None

            if strategy.APP_ID != expected_app_id:
                return False, (
                    f"APP_ID mismatch: expected {expected_app_id!r}, "
                    f"got {strategy.APP_ID!r}"
                ), None, None

            chain_id, contract_address, params, intent_fn, _scen, _fb = _pick_test_fixture(
                app_context, scenario_name=scenario_name,
            )
            snapshot = build_synthetic_snapshot(chain_id=chain_id)
            intent = AppIntentDefinition(
                app_id=expected_app_id,
                name="Test Intent",
                version="1.0.0",
                intent_type="swap",
                js_code="// test",
                config=AppIntentConfig(
                    supported_chains=[chain_id],
                    trigger_type=TriggerType.USER_TRIGGERED,
                ),
            )
            state = IntentState(
                contract_address=contract_address,
                chain_id=chain_id,
                nonce=1,
                owner="0x0000000000000000000000000000000000000001",
                raw_params=params,
                control={"_intent_function": intent_fn},
            )

            try:
                plan = strategy.generate_plan(intent, state, snapshot)
            except Exception as exc:
                return False, f"generate_plan failed: {exc}", None, None

            error = _validate_plan_structure(plan, intent, snapshot)
            if error:
                return False, f"Invalid plan: {error}", None, None

            return (
                True,
                f"OK: {len(plan.interactions)} interactions",
                plan,
                state,
            )

        except Exception as exc:
            return False, f"Unexpected error: {exc}", None, None

        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass

    def test_strategy_with_context(
        self,
        code: str,
        expected_app_id: str,
        app_context: AppContext | None,
    ) -> tuple[bool, str]:
        """Variant of test_strategy that takes an AppContext."""
        passed, msg, _plan, _state = self._generate_test_plan(
            code, expected_app_id, app_context=app_context,
        )
        return passed, msg

    def test_strategy(
        self,
        code: str,
        expected_app_id: str,
    ) -> tuple[bool, str]:
        """Test a strategy code string. Returns (passed, message).

        Args:
            code: Python source code for the strategy.
            expected_app_id: The app_id this strategy should handle.

        Returns:
            Tuple of (passed: bool, message: str).
        """
        passed, msg, _plan, _state = self._generate_test_plan(code, expected_app_id)
        return passed, msg

    async def score_strategy_full(
        self,
        code: str,
        app_id: str,
        validator_url: str,
        app_context: AppContext | None = None,
        include_historical: bool = True,
    ) -> tuple[float, str, list[dict]]:
        """Run pre-submission scoring across the EXACT same fixture set the
        validator's benchmark_worker uses: every chain-matching manifest
        scenario PLUS the historical-replay scenarios fetched from the
        validator's ``/v1/apps/{app_id}/historical-scenarios`` endpoint.

        This is the source of truth for "what will the validator score
        this submission?" — same simulator, same fixtures. The cheap
        3-scenario sample (``score_strategy_sampled``) drifts from the
        real benchmark when manifest-passing strategies regress on
        historical replays; this method eliminates that drift.

        Returns ``(mean_score, message, per_scenario)`` where:
          - ``mean_score``: arithmetic mean across all real (non-transient)
            scenarios; 0.0 if all failed.
          - ``message``: short summary including manifest/historical means
            and the worst-scoring scenario's reason.
          - ``per_scenario``: list of ``{scenario, score, reason, transient}``
            dicts.

        Cost: ~5-10s per scenario × ~19 scenarios ≈ 60-90s total. Use
        for the submission gate; not for per-iteration Claude self-tests
        (Claude has the cheaper ``score_strategy_sampled``).
        """
        import aiohttp
        # Fetch historical scenarios so we replay the same orders the
        # validator will. Soft failure: if history isn't available we
        # still run manifest scenarios.
        historical: list[dict] = []
        if include_historical:
            try:
                base = validator_url.rstrip("/")
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{base}/v1/apps/{app_id}/historical-scenarios",
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            historical = data.get("scenarios") or []
            except Exception as exc:
                logger.debug("historical scenarios fetch failed: %s", exc)

        fixtures = _all_test_fixtures(app_context, historical_scenarios=historical)
        # Route through ``_score_one_fixture`` (the same path
        # ``score_strategy_all`` uses) so we honour the fixture tuple's
        # full params + fork_block — critical for historical replays
        # whose per-order params can't be addressed by ``scenario_name``
        # alone. Loading the strategy once outside the loop saves N
        # redundant tempfile/import cycles.
        #
        # ``_score_one_fixture`` reads VALIDATOR_URL from os.environ
        # (defaulting to localhost:8080) — but the miner container
        # doesn't normally set VALIDATOR_URL, so the gate's HTTP calls
        # used to fail with "Connection refused" inside the container.
        # Push the loop's validator_url into the env once so every
        # ``_score_one_fixture`` HTTP call lands on the real validator.
        import os as _os_set
        _os_set.environ["VALIDATOR_URL"] = validator_url
        try:
            from minotaur_subnet.miner.agent.mcp_server import (
                _load_strategy_from_code, _score_one_fixture,
            )
            strategy = _load_strategy_from_code(code)
        except Exception as exc:
            return 0.0, f"strategy load failed: {exc}", []
        per_scenario: list[dict] = []
        manifest_scores: list[float] = []
        historical_scores: list[float] = []
        # Rate-limit between /score calls. Each call makes ~5-10 RPC
        # requests to the validator's Anvil fork (impersonate, deal,
        # send-tx, wait-for-receipt, snapshot, revert). 19 calls back-
        # to-back right after Claude's MCP-tool flurry of similar calls
        # overwhelms Anvil and triggers transient errors that mark
        # every sample as transient. A small inter-call delay avoids
        # the contention spike. Tunable via MINER_PRESIM_DELAY_MS.
        import os as _os
        _delay = max(0.0, float(_os.environ.get("MINER_PRESIM_DELAY_MS", "300"))) / 1000
        # Pre-gate cooldown — give Anvil time to clear the request queue
        # left over from Claude's MCP-tool burst. The gate runs RIGHT
        # AFTER Claude completes, when Anvil is still digesting ~30+
        # /score-equivalent calls from the writer's score_strategy_all
        # tool calls. Without a pause, every gate sample lands on a
        # contended Anvil and trips transient detection. Tunable via
        # MINER_PRESIM_PRE_DELAY_S.
        _pre_delay = max(0.0, float(_os.environ.get("MINER_PRESIM_PRE_DELAY_S", "30")))
        if _pre_delay > 0:
            logger.info(
                "score_strategy_full: cooling down %.0fs before 19-scenario batch "
                "(let Anvil drain Claude's MCP-tool calls)", _pre_delay,
            )
            await asyncio.sleep(_pre_delay)
        for idx, fix in enumerate(fixtures):
            if idx > 0 and _delay > 0:
                await asyncio.sleep(_delay)
            scen_name = fix[4]
            try:
                result = await asyncio.to_thread(
                    _score_one_fixture, strategy, app_id, fix,
                )
            except Exception as exc:
                result = {"error": str(exc)}
            # If a single sample tripped a transient error, retry it
            # once after a longer pause. Single-sample retries are
            # cheap (~5s each) and dramatically improve the gate's
            # signal-to-noise ratio when Anvil flaps mid-batch.
            _msg_check = (
                (result.get("reason") or result.get("message") or result.get("error") or "")
                if isinstance(result, dict) else ""
            )
            if _is_transient_failure(_msg_check):
                await asyncio.sleep(2.0)
                try:
                    result = await asyncio.to_thread(
                        _score_one_fixture, strategy, app_id, fix,
                    )
                except Exception as exc:
                    result = {"error": str(exc)}
            if isinstance(result, dict):
                score = float(result.get("score") or 0.0)
                msg = (
                    result.get("reason") or result.get("message")
                    or result.get("error") or ""
                )
            else:
                score, msg = 0.0, str(result)
            transient = _is_transient_failure(msg)
            if transient:
                logger.warning(
                    "[gate] transient detected for %s: %s",
                    scen_name, (msg or "")[:200],
                )
            entry = {
                "scenario": scen_name, "score": float(score),
                "reason": msg, "transient": transient,
            }
            per_scenario.append(entry)
            if transient:
                continue
            if scen_name.startswith("hist:"):
                historical_scores.append(float(score))
            else:
                manifest_scores.append(float(score))

        all_scores = manifest_scores + historical_scores
        if not all_scores:
            return 0.0, "all samples transient", per_scenario
        mean = sum(all_scores) / len(all_scores)
        m_mean = sum(manifest_scores) / len(manifest_scores) if manifest_scores else 0.0
        h_mean = sum(historical_scores) / len(historical_scores) if historical_scores else 0.0
        worst = min(per_scenario, key=lambda r: r["score"])
        msg = (
            f"full benchmark: mean={mean:.4f} manifest={m_mean:.4f} "
            f"historical={h_mean:.4f} ({len(manifest_scores)}m+{len(historical_scores)}h) "
            f"worst={worst['scenario']}@{worst['score']:.4f}: {worst['reason'][:100]}"
        )
        return mean, msg, per_scenario

    async def score_strategy_sampled(
        self,
        code: str,
        app_id: str,
        validator_url: str,
        app_context: AppContext | None = None,
        sample_count: int = 3,
    ) -> tuple[float, str, list[dict]]:
        """Run pre-submission scoring across the first ``sample_count``
        chain-matching scenarios and return the *aggregate* signal.

        Single-scenario gating is fragile: one bad sample (``Anvil
        unavailable`` during the daily recycle window, or one specific
        scenario hitting an edge case) drives the gate to 0.0 even when
        the other 18 scenarios all pass. We average across a small
        sample to smooth that out before deciding to block a submission.

        Returns ``(mean_score, message, per_scenario)`` where:
          - ``mean_score``: arithmetic mean across passing scenarios; 0.0
            if all failed or none ran.
          - ``message``: short summary including the worst-scoring
            scenario's reason — that's still the most actionable feedback
            for Claude's next iteration.
          - ``per_scenario``: list of ``{scenario, score, reason, transient}``
            dicts so the loop can decide whether failures are transient
            infra noise (Anvil unavailable) or real strategy bugs.
        """
        from minotaur_subnet.miner.agent.strategy_tester import (
            _all_test_fixtures,
        )
        # Build fixtures — manifest scenarios only, no historical replays.
        # Historical replays are slower (3-5s each) and would push the
        # pre-sim past the 60s cycle interval. The miner's actual
        # benchmark feedback (score_strategy_all) covers historical.
        fixtures = _all_test_fixtures(app_context, historical_scenarios=[])
        fixtures = fixtures[: max(1, int(sample_count))]
        # Reuse score_strategy's plumbing per-scenario via _pick_test_fixture
        # cycling — but simpler: drive the existing score_strategy method
        # and tag results with the scenario name we passed in.
        per_scenario: list[dict] = []
        scores: list[float] = []
        for fix in fixtures:
            scen_name = fix[4]
            score, msg = await self.score_strategy(
                code, app_id, validator_url, app_context=app_context,
                scenario_name=scen_name,
            )
            transient = _is_transient_failure(msg)
            per_scenario.append({
                "scenario": scen_name,
                "score": float(score),
                "reason": msg,
                "transient": transient,
            })
            if not transient:
                scores.append(float(score))
        if not scores:
            # Every sample was transient (e.g. Anvil down). Surface that
            # so the caller can skip the gate rather than fail it.
            return 0.0, "all samples transient", per_scenario
        mean = sum(scores) / len(scores)
        worst = min(per_scenario, key=lambda x: x["score"])
        msg = (
            f"sampled {len(scores)} scenarios mean={mean:.4f} "
            f"worst={worst['scenario']}@{worst['score']:.4f}: "
            f"{worst['reason'][:120]}"
        )
        return mean, msg, per_scenario

    async def score_strategy(
        self,
        code: str,
        app_id: str,
        validator_url: str,
        app_context: AppContext | None = None,
        scenario_name: str | None = None,
    ) -> tuple[float, str]:
        """Score a strategy's plan against the validator's JS scoring engine.

        Generates a plan from synthetic state, then POSTs it to the
        validator's ``POST /v1/apps/{app_id}/score`` endpoint for Anvil
        simulation and JS scoring.

        Args:
            code: Python source code for the strategy.
            app_id: The app_id this strategy handles.
            validator_url: Base URL of the validator.

        Returns:
            Tuple of (score: float, message: str). Score is 0.0 on failure.
        """
        import aiohttp

        passed, msg, plan, state = self._generate_test_plan(
            code, app_id, app_context=app_context,
            scenario_name=scenario_name,
        )
        if not passed or plan is None or state is None:
            return 0.0, f"Structural test failed: {msg}"

        # Serialize plan for the score endpoint
        plan_dict = {
            "intent_id": plan.intent_id,
            "interactions": [
                {
                    "target": ix.target,
                    "value": ix.value,
                    "call_data": ix.call_data,
                    "chain_id": ix.chain_id,
                }
                for ix in plan.interactions
            ],
            "deadline": plan.deadline,
            "nonce": plan.nonce,
            "metadata": plan.metadata or {},
        }
        params = _state_params(state)

        try:
            from minotaur_subnet.miner.signing import signed_headers
            path = f"/v1/apps/{app_id}/score"
            url = f"{validator_url.rstrip('/')}{path}"
            payload = {"plan": plan_dict, "params": params}
            # /apps/{id}/score is gated (admin key OR a signed miner). Sign the
            # call with the miner's hotkey; required=False so a local
            # unauthenticated testnet still works when no wallet is configured,
            # while against the real leader this authenticates instead of 401.
            headers = signed_headers("POST", path, required=False) or None
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return 0.0, f"Score endpoint returned {resp.status}: {text[:200]}"
                    result = await resp.json()
                    score = result.get("score", 0.0)
                    valid = result.get("valid", False)
                    reason = result.get("reason", "")
                    sim_mode = result.get("simulation_mode", "unknown")
                    return score, (
                        f"score={score:.4f}, valid={valid}, "
                        f"sim={sim_mode}, reason={reason}"
                    )
        except Exception as exc:
            return 0.0, f"Scoring request failed: {exc}"
