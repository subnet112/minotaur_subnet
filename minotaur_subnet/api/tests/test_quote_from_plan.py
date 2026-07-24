"""Tests for POST /apps/{app_id}/quote — quoting entirely from generate_plan.

The /quote endpoint must derive ``estimated_output`` SOLELY from the solver's
``generate_plan()`` plan, simulated the same way the benchmark scores it — NOT
from ``solver.quote()`` (a separate, frozen generic pool-router that returns "0"
for many routable tokens, e.g. DONALDPUMP). These tests use fakes (no anvil):
a solver whose ``quote()`` would fail/return 0 but whose ``generate_plan()``
produces a delivering plan, plus a fake simulation runner returning a
``SimulationResult`` with the delivered output in ``token_transfers``.

Follows the same test patterns as test_routes.py / test_submissions.py.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Disable background workers before importing the app
os.environ["DISABLE_BENCHMARK_WORKER"] = "1"
os.environ["DISABLE_BLOCK_LOOP"] = "1"

from fastapi.testclient import TestClient

from minotaur_subnet.api.server import app
from minotaur_subnet.api.routes import orders as orders_module
from minotaur_subnet.harness import orchestrator as orchestrator_module
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    SimulationResult,
    TokenTransfer,
)


# The concrete bug case: a UniV4-hook token quote() returns "0" for, but whose
# generate_plan delivers ~4.84e25 (exact wei string).
_DONALDPUMP = "0x24bc862e4a8aca815facc8d0275b1eb2e266db07"
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_DEPLOYED = "0x000000000000000000000000000000000000c0de"
_DEAD = "0x000000000000000000000000000000000000dead"
_DELIVERED = "48400000000000000000000000"  # ~4.84e25
_ROUTE = "uniV4:USDC->DONALDPUMP hook=0xd60d6b218116cfd801e28f78d011a203d2b068cc"
_CHAIN = 8453


class _FakeSolver:
    """quote() would blow up / return 0 for DONALDPUMP; generate_plan delivers.

    quote() raising here PROVES the endpoint never depends on it.
    """

    def __init__(self, quote_zero: bool = False) -> None:
        self._quote_zero = quote_zero

    def quote(self, app_def, state, *a, **k):
        if self._quote_zero:
            from minotaur_subnet.shared.types import QuoteResult
            return QuoteResult(estimated_output="0")
        raise AssertionError("get_quote must NOT call solver.quote()")

    def generate_plan(self, app_def, state, *a, **k):
        return ExecutionPlan(
            intent_id="app:swap",
            interactions=[
                Interaction(target=_DONALDPUMP, value="0", call_data="0x", chain_id=_CHAIN),
            ],
            deadline=0,
            nonce=0,
            metadata={"route": _ROUTE},
        )


class _FakeSimRunner:
    """Returns a SimulationResult whose token_transfers deliver the output.

    Spies on every simulate() call so tests can assert the handler now runs the
    benchmark's scoreIntent path (non-None ``intent_order``). An optional
    ``result`` override lets a test inject a sim carrying ``metadata.raw_output``.
    """

    def __init__(
        self, result: SimulationResult | None = None, fork_block: int = 21_000_000,
    ) -> None:
        # ``simulator`` is truthy (→ _has_sim True) and exposes current_fork_block
        # so the quote path's fork-pin capture works. ``fork_block`` is the block
        # the fork reports landing on after a cold (head) re-fork.
        self.simulator = SimpleNamespace(
            current_fork_block=lambda chain_id=None: fork_block,
        )
        self.calls: list[SimpleNamespace] = []
        self._result = result

    async def simulate(
        self, plan, order, contract_address, intent_order, is_cross_chain, deployed,
        fork_block=None, pin_only=False,
    ):
        self.calls.append(SimpleNamespace(
            plan=plan, order=order, contract_address=contract_address,
            intent_order=intent_order, is_cross_chain=is_cross_chain, deployed=deployed,
            fork_block=fork_block, pin_only=pin_only,
        ))
        if self._result is not None:
            return self._result
        return SimulationResult(
            success=True,
            gas_used=180000,
            token_transfers=[
                # input leg (pulled from the user) — must be IGNORED by the sum
                TokenTransfer(token=_USDC, from_addr=_DEAD, to_addr=_DONALDPUMP, amount="1000000000"),
                # delivered output to the recipient — this is what we quote
                TokenTransfer(token=_DONALDPUMP, from_addr=_DEPLOYED, to_addr=_DEAD, amount=_DELIVERED),
            ],
        )


class _FakeStore:
    def __init__(self) -> None:
        self.attempts: list[tuple] = []

    def get_app(self, app_id):
        return SimpleNamespace(
            app_id=app_id, name="DexAggregator",
            js_code="function score(){return 1;}",
        )

    def get_deployment(self, app_id, chain_id=None):
        return SimpleNamespace(
            contract_address=_DEPLOYED,
            chain_id=chain_id or _CHAIN,
            status=SimpleNamespace(is_operational=lambda: True),
        )

    def record_quote_attempt(self, app_id, success=True, error=""):
        self.attempts.append((app_id, success, error))


class TestQuoteFromGeneratePlan(unittest.TestCase):
    """POST /apps/{id}/quote quotes ENTIRELY from generate_plan + sim."""

    def setUp(self):
        # Disable the per-IP rate limit so repeated runs don't 429.
        self._prev_rl = os.environ.get("QUOTE_RATE_LIMIT_PER_MINUTE")
        os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = "0"
        orders_module.set_js_engine(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        # Preserve the real benchmark intent-order builder — some tests patch it
        # to return a sentinel (the handler lazy-imports it from orchestrator, so
        # patching the module attribute is what the import resolves at call time).
        self._real_build_intent_order = orchestrator_module._build_benchmark_intent_order
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_block_loop(None)
        orders_module.set_app_store(None)
        orchestrator_module._build_benchmark_intent_order = self._real_build_intent_order
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        if self._prev_rl is None:
            os.environ.pop("QUOTE_RATE_LIMIT_PER_MINUTE", None)
        else:
            os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = self._prev_rl

    def _post_quote(self):
        return self.client.post(
            f"/v1/apps/testapp/quote",
            json={
                "intent_function": "swap",
                "chain_id": _CHAIN,
                "params": {
                    "input_token": _USDC,
                    "output_token": _DONALDPUMP,
                    "input_amount": "1000000000",
                },
            },
        )

    def test_quote_uses_plan_delivered_output_when_quote_raises(self):
        """quote() raising must NOT stop the endpoint returning the plan's output."""
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=_FakeSimRunner())
        )

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()

        # estimated_output is the simulated plan's DELIVERED output (exact wei),
        # summed from the output-token transfer to the recipient — the input-leg
        # USDC transfer is excluded.
        self.assertEqual(data["estimated_output_gross"], _DELIVERED)
        self.assertNotEqual(data["estimated_output"], "0")
        # Fee is in wrapped-native (WETH), not the output token, so net == gross.
        self.assertEqual(data["estimated_output"], _DELIVERED)
        # Route/gas are also derived from the plan + its simulation.
        self.assertEqual(data["route_summary"], _ROUTE)
        self.assertGreater(data["gas_estimate"], 180000)  # swap gas + framework overhead

    def test_quote_nonzero_even_when_solver_quote_returns_zero(self):
        """The exact bug: quote()=='0' but generate_plan delivers — quote is non-zero."""
        store = _FakeStore()
        orders_module.set_app_store(store)
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=True), _simulation_runner=_FakeSimRunner())
        )

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["estimated_output"], _DELIVERED)
        # The quote attempt is recorded as a success (non-zero output).
        self.assertTrue(store.attempts and store.attempts[-1][1] is True)

    def test_passes_scoreintent_intent_order_to_sim(self):
        """The handler must run the benchmark's scoreIntent path — i.e. pass a
        NON-None intent_order (4th positional arg) to _sim_runner.simulate, not
        the bare-interaction path (None) which delivers 0 for exotic tokens."""
        # Sentinel intent_order so we don't need a real manifest/encoder here.
        _sentinel = {
            "order_id": "bench_test",
            "app": _DEPLOYED,
            "submitted_by": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        }
        _captured: dict = {}

        def _fake_builder(state, plan, manifest=None):
            # The handler must pin the encoded receiver to the funded submitted_by.
            _captured["receiver"] = state.raw_params_view().get("receiver")
            return _sentinel

        orchestrator_module._build_benchmark_intent_order = _fake_builder

        runner = _FakeSimRunner()
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)

        # scoreIntent path: exactly one simulate() call with a non-None intent_order.
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNotNone(runner.calls[0].intent_order)
        self.assertEqual(runner.calls[0].intent_order, _sentinel)
        # The simulator's scoreIntent branch gates on `contract_address AND
        # intent_order` — passing None here silently demotes the sim to the
        # bare-interaction path (0 delivered), which is exactly the V2
        # chain-1 zero-quote bug's final layer.
        self.assertEqual(runner.calls[0].contract_address, _DEPLOYED)
        # Revert-avoidance: the order's submitted_by == the intent_order's source
        # == the receiver the encoder was handed (the pre-funded Anvil default).
        self.assertEqual(
            runner.calls[0].order.submitted_by,
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        )
        self.assertEqual(
            _captured["receiver"], "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        )

    def test_manifest_lazy_loads_into_shared_engine_when_missing(self):
        """An app the shared engine has never scored (e.g. a fresh V2
        deployment with no live orders yet) has no cached manifest. The quote
        path must lazy-load the app's js_code into the engine — not silently
        degrade to the bare-interaction sim, which measures 0 delivered (the
        DEX Aggregator V2 chain-1 zero-quote bug)."""
        _MANIFEST = {"intent_functions": [{"name": "swap"}]}

        class _FakeEngine:
            def __init__(self):
                self.loaded: list = []
                self._manifests: dict = {}

            def get_manifest(self, app_id):
                return self._manifests.get(app_id)

            async def load_intent(self, app_id, js_code):
                self.loaded.append((app_id, js_code))
                self._manifests[app_id] = _MANIFEST

        _captured: dict = {}

        def _fake_builder(state, plan, manifest=None):
            _captured["manifest"] = manifest
            # Like the real builder: no manifest → no intent_order (bare path).
            return {"submitted_by": "0xf39F"} if manifest else None

        orchestrator_module._build_benchmark_intent_order = _fake_builder

        engine = _FakeEngine()
        orders_module.set_js_engine(engine)
        runner = _FakeSimRunner()
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)

        # The engine was lazily fed the app's js_code exactly once…
        self.assertEqual(
            engine.loaded, [("testapp", "function score(){return 1;}")],
        )
        # …the builder received the freshly extracted manifest…
        self.assertEqual(_captured["manifest"], _MANIFEST)
        # …and the sim therefore ran the scoreIntent path, not the bare one.
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNotNone(runner.calls[0].intent_order)

    def test_estimated_output_from_scoreintent_raw_output_metadata(self):
        """estimated_output must read the scoreIntent gained value the live scorer
        emits on ``sim.metadata['raw_output']`` (now populated by the scoreIntent
        path) — not just the token-transfer fallback."""
        _raw = "99999999999999999999999999"  # exact-wei scoreIntent output
        sim = SimulationResult(success=True, gas_used=180000)
        sim.metadata = {"raw_output": _raw}  # what the live raw-output scorer emits

        orchestrator_module._build_benchmark_intent_order = (
            lambda state, plan, manifest=None: {"submitted_by": "0xabc"}
        )

        runner = _FakeSimRunner(result=sim)
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["estimated_output_gross"], _raw)
        self.assertEqual(data["estimated_output"], _raw)

    def test_identical_quote_hits_cache_and_skips_second_sim(self):
        """A second identical quote within the TTL must be served from the plan
        cache — NOT re-run the expensive fork sim. Regression guard for the
        cache actually being consulted end-to-end."""
        runner = _FakeSimRunner()
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        r1 = self._post_quote()
        r2 = self._post_quote()
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        # Exactly ONE simulate() across two identical quotes — the 2nd hit cache.
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(r1.json()["estimated_output"], r2.json()["estimated_output"])

    def test_different_params_miss_cache_and_resimulate(self):
        """A quote with different params must NOT be served the prior sim's
        result — the key includes the params, so it re-simulates."""
        runner = _FakeSimRunner()
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        def _post(amount):
            return self.client.post(
                "/v1/apps/testapp/quote",
                json={
                    "intent_function": "swap",
                    "chain_id": _CHAIN,
                    "params": {
                        "input_token": _USDC,
                        "output_token": _DONALDPUMP,
                        "input_amount": amount,
                    },
                },
            )

        self.assertEqual(_post("1000000000").status_code, 200)
        self.assertEqual(_post("2000000000").status_code, 200)
        self.assertEqual(len(runner.calls), 2)  # distinct params → two sims

    def test_fork_pin_cold_then_warm_reuse(self):
        """The FIRST quote in a window re-forks to head (fork_block=None,
        pin_only=False) and records the block it landed on; a later DISTINCT
        quote (misses the plan cache) reuses that pinned block (pin_only=True)
        so the sim runs on a cache-warm fork instead of re-forking to head."""
        runner = _FakeSimRunner(fork_block=21_000_123)
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=runner)
        )

        def _post(amount):
            return self.client.post(
                "/v1/apps/testapp/quote",
                json={
                    "intent_function": "swap",
                    "chain_id": _CHAIN,
                    "params": {
                        "input_token": _USDC,
                        "output_token": _DONALDPUMP,
                        "input_amount": amount,
                    },
                },
            )

        # Cold: first quote re-forks to head and captures the pin.
        self.assertEqual(_post("1000000000").status_code, 200)
        self.assertIsNone(runner.calls[0].fork_block)
        self.assertFalse(runner.calls[0].pin_only)
        self.assertEqual(orders_module._quote_fork_pin_get(_CHAIN), 21_000_123)

        # Warm: a DISTINCT quote (misses the plan cache) reuses the pinned block.
        self.assertEqual(_post("2000000000").status_code, 200)
        self.assertEqual(runner.calls[1].fork_block, 21_000_123)
        self.assertTrue(runner.calls[1].pin_only)


# ── Integration: the REAL _build_benchmark_intent_order address invariant ──────
# The handler-level tests above stub the builder, so they prove get_quote pins
# raw_params["receiver"] == order.submitted_by == _bench_receiver. These two
# exercise the REAL builder (only the ABI byte-encoder is stubbed — it needs a
# real manifest) to prove the OTHER half: intent_order["submitted_by"] resolves
# to that same receiver. Together they close the revert-avoidance invariant —
# SimulationRunner funds order.submitted_by, scoreIntent pulls from
# intent_order["submitted_by"]; if they differ the pull reverts → 0.

def test_real_builder_pins_submitted_by_to_receiver(monkeypatch):
    from minotaur_subnet.api.services import app_service
    from minotaur_subnet.shared.types import IntentState
    monkeypatch.setattr(
        app_service, "build_intent_params_hex_from_manifest",
        lambda *a, **k: "deadbeef",  # bypass real ABI encoding (needs a manifest)
    )
    receiver = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    state = IntentState(
        contract_address=_DEPLOYED, chain_id=_CHAIN, nonce=0, owner=receiver,
        raw_params={"receiver": receiver, "token_in": _USDC, "token_out": _DONALDPUMP},
        control={"_intent_function": "swap"},
    )
    plan = ExecutionPlan(
        intent_id="app:swap", interactions=[], deadline=0, nonce=0, metadata={},
    )
    io = orchestrator_module._build_benchmark_intent_order(
        state, plan, manifest={"functions": {}},
    )
    assert io is not None, "real builder should build an intent_order given a manifest"
    assert io["submitted_by"] == receiver


def test_real_builder_maps_sentinel_receiver_to_anvil_default(monkeypatch):
    # address(1) is the scenarios' dummy receiver; the builder (and get_quote's
    # _bench_receiver) both map it to the pre-funded Anvil default, so they agree.
    from minotaur_subnet.api.services import app_service
    from minotaur_subnet.shared.types import IntentState
    monkeypatch.setattr(
        app_service, "build_intent_params_hex_from_manifest",
        lambda *a, **k: "deadbeef",
    )
    anvil = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    state = IntentState(
        contract_address=_DEPLOYED, chain_id=_CHAIN, nonce=0, owner=anvil,
        raw_params={"receiver": "0x0000000000000000000000000000000000000001"},
        control={"_intent_function": "swap"},
    )
    plan = ExecutionPlan(
        intent_id="app:swap", interactions=[], deadline=0, nonce=0, metadata={},
    )
    io = orchestrator_module._build_benchmark_intent_order(
        state, plan, manifest={"functions": {}},
    )
    assert io is not None
    assert io["submitted_by"] == anvil


class _RetireStore:
    """App store whose single deployment carries a configurable retirement status.

    Uses the REAL ``DeploymentResult`` so ``is_effectively_retired`` (the gate's
    predicate) is exercised, not faked.
    """

    def __init__(self, status, retire_effective_epoch=None) -> None:
        from minotaur_subnet.shared.types import DeploymentResult
        self._dep = DeploymentResult(
            app_id="testapp", status=status, chain_id=_CHAIN,
            contract_address=_DEPLOYED, retire_effective_epoch=retire_effective_epoch,
        )
        self.attempts: list[tuple] = []

    def get_app(self, app_id):
        return SimpleNamespace(
            app_id=app_id, name="DexAggregator",
            js_code="function score(){return 1;}",
        )

    def get_deployment(self, app_id, chain_id=None):
        return self._dep

    def record_quote_attempt(self, app_id, success=True, error=""):
        self.attempts.append((app_id, success, error))


class TestQuoteRetirementGate(unittest.TestCase):
    """POST /apps/{id}/quote must reject a deregistered deployment (RETIRED, or
    RETIRING past its round-anchored cutover) — mirroring the order path — while
    still quoting a live or pre-cutover app. See app_lifecycle.deregister_app."""

    def setUp(self):
        self._prev_rl = os.environ.get("QUOTE_RATE_LIMIT_PER_MINUTE")
        os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = "0"
        orders_module.set_js_engine(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_block_loop(None)
        orders_module.set_app_store(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        if self._prev_rl is None:
            os.environ.pop("QUOTE_RATE_LIMIT_PER_MINUTE", None)
        else:
            os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = self._prev_rl

    def _post_quote(self):
        return self.client.post(
            "/v1/apps/testapp/quote",
            json={
                "intent_function": "swap",
                "chain_id": _CHAIN,
                "params": {
                    "input_token": _USDC,
                    "output_token": _DONALDPUMP,
                    "input_amount": "1000000000",
                },
            },
        )

    @staticmethod
    def _round_store(opened_epoch):
        cur = None if opened_epoch is None else SimpleNamespace(opened_epoch=opened_epoch)
        return SimpleNamespace(get_current_round=lambda: cur)

    def _set_solver_and_sim(self):
        sim = _FakeSimRunner()
        orders_module.set_block_loop(SimpleNamespace(
            solver=_FakeSolver(quote_zero=False), _simulation_runner=sim,
        ))
        return sim

    def test_quote_rejected_for_retired_deployment(self):
        """RETIRED is epoch-independent → 400 regardless of the current round."""
        from minotaur_subnet.shared.types import AppStatus
        orders_module.set_app_store(_RetireStore(AppStatus.RETIRED))
        sim = self._set_solver_and_sim()

        resp = self._post_quote()
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("retired", resp.json()["detail"].lower())
        # Gate must fire BEFORE the expensive plan simulation.
        self.assertEqual(sim.calls, [])

    def test_quote_rejected_for_retiring_past_cutover(self):
        """RETIRING drops once the round's opened_epoch reaches the cutover."""
        from unittest.mock import patch
        from minotaur_subnet.shared.types import AppStatus
        orders_module.set_app_store(
            _RetireStore(AppStatus.RETIRING, retire_effective_epoch=1000))
        sim = self._set_solver_and_sim()

        with patch(
            "minotaur_subnet.api.routes.submissions.get_round_store",
            return_value=self._round_store(1000),  # opened_epoch == cutover
        ):
            resp = self._post_quote()
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("retired", resp.json()["detail"].lower())
        self.assertEqual(sim.calls, [])

    def test_quote_allowed_for_retiring_before_cutover(self):
        """Before the cutover round, a RETIRING app still quotes (not yet effective)."""
        from unittest.mock import patch
        from minotaur_subnet.shared.types import AppStatus
        orders_module.set_app_store(
            _RetireStore(AppStatus.RETIRING, retire_effective_epoch=1000))
        self._set_solver_and_sim()

        with patch(
            "minotaur_subnet.api.routes.submissions.get_round_store",
            return_value=self._round_store(999),  # one epoch before cutover
        ):
            resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["estimated_output"], _DELIVERED)

    def test_quote_allowed_for_retiring_when_no_open_round(self):
        """No open round → epoch unresolvable → RETIRING treated as not-yet-retired
        (conservative), so the quote still serves rather than dropping on a
        transient no-round window."""
        from unittest.mock import patch
        from minotaur_subnet.shared.types import AppStatus
        orders_module.set_app_store(
            _RetireStore(AppStatus.RETIRING, retire_effective_epoch=1000))
        self._set_solver_and_sim()

        with patch(
            "minotaur_subnet.api.routes.submissions.get_round_store",
            return_value=self._round_store(None),  # no current round
        ):
            resp = self._post_quote()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["estimated_output"], _DELIVERED)


class TestQuotePlanCacheAndSingleFlight(unittest.TestCase):
    """Unit tests for the plan-cache primitives: single-flight dedup, the
    short negative TTL, and oldest-entry (not clear-all) eviction."""

    def setUp(self):
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()

    def tearDown(self):
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()

    def test_single_flight_collapses_concurrent_identical_computes(self):
        """N concurrent callers for the same key run the producer ONCE, and all
        receive that one result — the thundering-herd fix."""
        import asyncio

        calls = {"n": 0}

        async def scenario():
            gate = asyncio.Event()

            async def producer():
                calls["n"] += 1
                await gate.wait()  # hold the "sim" open so all callers overlap
                result = {"delivered": 42, "gas_units": 7, "route": "r", "metadata": {}}
                orders_module._quote_plan_cache_put("K", result)
                return result

            waiters = [
                asyncio.ensure_future(
                    orders_module._quote_plan_get_or_compute("K", producer)
                )
                for _ in range(4)
            ]
            await asyncio.sleep(0)  # let all four register/await the shared task
            gate.set()
            return await asyncio.gather(*waiters)

        results = asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)  # producer ran exactly once for 4 callers
        self.assertTrue(all(r["delivered"] == 42 for r in results))
        # Registry cleaned up after completion.
        self.assertNotIn("K", orders_module._QUOTE_PLAN_INFLIGHT)

    def test_second_caller_after_completion_hits_cache_not_producer(self):
        """Once the producer has run and cached, a later call for the same key
        returns from cache without re-running the producer."""
        import asyncio

        calls = {"n": 0}

        async def scenario():
            async def producer():
                calls["n"] += 1
                result = {"delivered": 5, "gas_units": 0, "route": "", "metadata": {}}
                orders_module._quote_plan_cache_put("K", result)
                return result

            first = await orders_module._quote_plan_get_or_compute("K", producer)
            second = await orders_module._quote_plan_get_or_compute("K", producer)
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first["delivered"], 5)
        self.assertEqual(second["delivered"], 5)

    def test_negative_entry_expires_on_short_ttl_while_positive_survives(self):
        """A degraded (``_negative``) result must clear on the short NEG_TTL so a
        transient failure can't pin a zero quote for the full window; a positive
        result at the same age survives."""
        o = orders_module
        o._quote_plan_cache_put("pos", {"delivered": 1})
        o._quote_plan_cache_put("neg", {"delivered": 0, "_negative": True})
        # Backdate both by an age between NEG_TTL (3s) and the full TTL (12s).
        age = (o._QUOTE_PLAN_NEG_TTL + o._QUOTE_PLAN_CACHE_TTL) / 2.0
        for k in ("pos", "neg"):
            ts, val = o._QUOTE_PLAN_CACHE[k]
            o._QUOTE_PLAN_CACHE[k] = (ts - age, val)
        self.assertIsNotNone(o._quote_plan_cache_get("pos"))  # positive still valid
        self.assertIsNone(o._quote_plan_cache_get("neg"))     # negative expired

    def test_eviction_drops_oldest_entry_not_whole_cache(self):
        """Overflowing the cap must evict only the OLDEST-inserted entry, not
        wipe every hot entry (the old clear-all behaviour)."""
        o = orders_module
        prev_max = o._QUOTE_PLAN_CACHE_MAX
        o._QUOTE_PLAN_CACHE_MAX = 3
        try:
            for k in ("a", "b", "c"):
                o._quote_plan_cache_put(k, {"delivered": 1})
            o._quote_plan_cache_put("d", {"delivered": 1})  # over cap → evict "a"
            self.assertEqual(set(o._QUOTE_PLAN_CACHE), {"b", "c", "d"})
        finally:
            o._QUOTE_PLAN_CACHE_MAX = prev_max


class TestQuoteForkPin(unittest.TestCase):
    """The quote fork-pin: a per-chain pin cache with a short TTL, and the
    simulator's pin_only no-op that reuses the fork when already at the block."""

    def setUp(self):
        orders_module._QUOTE_FORK_PIN.clear()

    def tearDown(self):
        orders_module._QUOTE_FORK_PIN.clear()

    def test_pin_get_put_and_ttl_expiry(self):
        o = orders_module
        self.assertIsNone(o._quote_fork_pin_get(1))
        o._quote_fork_pin_put(1, 21_000_000)
        self.assertEqual(o._quote_fork_pin_get(1), 21_000_000)
        # Backdate past the TTL → expires.
        ts, blk = o._QUOTE_FORK_PIN[1]
        o._QUOTE_FORK_PIN[1] = (ts - (o._QUOTE_FORK_PIN_TTL + 1.0), blk)
        self.assertIsNone(o._quote_fork_pin_get(1))
        self.assertNotIn(1, o._QUOTE_FORK_PIN)  # expired entry evicted on read

    def test_reset_fork_for_sim_skips_refork_when_pinned_at_block(self):
        """pin_only + fork already at the block → NO re-fork (reuse warm fork).
        This is the "run a quote without a re-fork" fast path; isolation is still
        the snapshot/revert bracket, not this call."""
        from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

        sim = AnvilSimulator.__new__(AnvilSimulator)  # bypass __init__ (no anvil)
        sim.w3 = SimpleNamespace(eth=SimpleNamespace(block_number=21_000_500))
        reset_calls: list = []
        sim._reset_fork = lambda block_number=None: reset_calls.append(block_number)

        # Already at the pinned block → skip.
        sim._reset_fork_for_sim(21_000_500, pin_only=True)
        self.assertEqual(reset_calls, [])

        # Block mismatch → re-fork to the pinned block (once).
        sim._reset_fork_for_sim(21_000_999, pin_only=True)
        self.assertEqual(reset_calls, [21_000_999])

    def test_reset_fork_for_sim_always_reforks_without_pin_only(self):
        """Scoring / order-processing path (pin_only=False) ALWAYS re-forks —
        byte-for-byte unchanged even when the fork is already at the block."""
        from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

        sim = AnvilSimulator.__new__(AnvilSimulator)
        sim.w3 = SimpleNamespace(eth=SimpleNamespace(block_number=21_000_500))
        reset_calls: list = []
        sim._reset_fork = lambda block_number=None: reset_calls.append(block_number)

        sim._reset_fork_for_sim(21_000_500, pin_only=False)  # same block, no pin
        sim._reset_fork_for_sim(None, pin_only=False)        # head re-fork
        self.assertEqual(reset_calls, [21_000_500, None])


def _dedicated_quote_runner(chain_ids, connected=True):
    """A fake DEDICATED quote runner: a spying _FakeSimRunner whose .simulator
    exposes a per-chain simulators map (each with is_connected()), matching the
    real MultiChainSimulator shape the get_quote runner-pick gate probes."""
    r = _FakeSimRunner()
    r.simulator = SimpleNamespace(
        current_fork_block=lambda chain_id=None: 21_000_000,
        simulators={
            cid: SimpleNamespace(is_connected=lambda c=connected: c) for cid in chain_ids
        },
    )
    return r


class TestDedicatedQuoteRunner(unittest.TestCase):
    """get_quote prefers the OPT-IN dedicated quote runner ONLY when it has a
    connected per-chain sim for the request chain, else falls back to the shared
    (order) runner — never changing the order path's runner object."""

    def setUp(self):
        self._prev_rl = os.environ.get("QUOTE_RATE_LIMIT_PER_MINUTE")
        os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = "0"
        orders_module.set_js_engine(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        orders_module.set_quote_sim_runner(None)
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_block_loop(None)
        orders_module.set_app_store(None)
        orders_module.set_quote_sim_runner(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
        orders_module._QUOTE_PLAN_INFLIGHT.clear()
        orders_module._QUOTE_FORK_PIN.clear()
        if self._prev_rl is None:
            os.environ.pop("QUOTE_RATE_LIMIT_PER_MINUTE", None)
        else:
            os.environ["QUOTE_RATE_LIMIT_PER_MINUTE"] = self._prev_rl

    def _wire(self, shared, dedicated):
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(
            SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=shared)
        )
        orders_module.set_quote_sim_runner(dedicated)

    def _post(self):
        return self.client.post(
            "/v1/apps/testapp/quote",
            json={"intent_function": "swap", "chain_id": _CHAIN,
                  "params": {"input_token": _USDC, "output_token": _DONALDPUMP,
                             "input_amount": "1000000000"}},
        )

    def test_no_dedicated_runner_uses_shared(self):
        shared = _FakeSimRunner()
        self._wire(shared, None)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(shared.calls), 1)  # opt-out: shared runner did the sim

    def test_dedicated_used_for_matching_connected_chain(self):
        shared = _FakeSimRunner()
        dedicated = _dedicated_quote_runner([_CHAIN], connected=True)  # _CHAIN == 8453
        self._wire(shared, dedicated)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(dedicated.calls), 1)   # dedicated ran the quote sim
        self.assertEqual(len(shared.calls), 0)      # order runner untouched

    def test_dedicated_missing_chain_falls_back(self):
        shared = _FakeSimRunner()
        dedicated = _dedicated_quote_runner([1], connected=True)  # only chain 1; quote is 8453
        self._wire(shared, dedicated)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(shared.calls), 1)      # fell back (direct per-chain lookup)
        self.assertEqual(len(dedicated.calls), 0)

    def test_dedicated_disconnected_falls_back(self):
        shared = _FakeSimRunner()
        dedicated = _dedicated_quote_runner([_CHAIN], connected=False)  # right chain, down
        self._wire(shared, dedicated)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(shared.calls), 1)      # is_connected() False → shared
        self.assertEqual(len(dedicated.calls), 0)

    def test_order_runner_object_identity_unchanged(self):
        shared = _FakeSimRunner()
        dedicated = _dedicated_quote_runner([_CHAIN], connected=True)
        bl = SimpleNamespace(solver=_FakeSolver(quote_zero=False), _simulation_runner=shared)
        orders_module.set_app_store(_FakeStore())
        orders_module.set_block_loop(bl)
        orders_module.set_quote_sim_runner(dedicated)
        self.assertEqual(self._post().status_code, 200)
        self.assertIs(bl._simulation_runner, shared)  # order path never rebound


class TestQuoteSimRegistry(unittest.TestCase):
    """The quote_sim_rpc resolver is the opt-in gate: empty env → no dedicated fork."""

    def test_quote_sim_rpc_resolver(self):
        from minotaur_subnet.chains import registry
        prev = os.environ.pop("BASE_QUOTE_SIM_RPC_URL", None)
        try:
            self.assertEqual(registry.quote_sim_rpc(8453), "")  # unset → no dedicated fork
            os.environ["BASE_QUOTE_SIM_RPC_URL"] = "http://anvil-base-quote:8546"
            self.assertEqual(registry.quote_sim_rpc(8453), "http://anvil-base-quote:8546")
            # NO fallback to the shared order anvil envs (the aliasing footgun).
            self.assertEqual(registry.quote_sim_rpc(31337), "")
        finally:
            if prev is None:
                os.environ.pop("BASE_QUOTE_SIM_RPC_URL", None)
            else:
                os.environ["BASE_QUOTE_SIM_RPC_URL"] = prev


if __name__ == "__main__":
    unittest.main()
