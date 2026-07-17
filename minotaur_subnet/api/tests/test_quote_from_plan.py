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

    def __init__(self, result: SimulationResult | None = None) -> None:
        self.simulator = object()  # truthy → _has_sim is True
        self.calls: list[SimpleNamespace] = []
        self._result = result

    async def simulate(self, plan, order, contract_address, intent_order, is_cross_chain, deployed):
        self.calls.append(SimpleNamespace(
            plan=plan, order=order, contract_address=contract_address,
            intent_order=intent_order, is_cross_chain=is_cross_chain, deployed=deployed,
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
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        orders_module.set_block_loop(None)
        orders_module.set_app_store(None)
        orders_module._QUOTE_PLAN_CACHE.clear()
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


if __name__ == "__main__":
    unittest.main()
