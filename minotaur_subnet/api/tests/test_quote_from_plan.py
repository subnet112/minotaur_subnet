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
    """Returns a SimulationResult whose token_transfers deliver the output."""

    def __init__(self) -> None:
        self.simulator = object()  # truthy → _has_sim is True

    async def simulate(self, plan, order, contract_address, intent_order, is_cross_chain, deployed):
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
        return SimpleNamespace(app_id=app_id, name="DexAggregator")

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


if __name__ == "__main__":
    unittest.main()
