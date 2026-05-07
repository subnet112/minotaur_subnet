"""Unit tests for the quoting endpoint (POST /apps/{app_id}/quote).

The quote endpoint calls solver.quote() for fast, pure-math quoting from
snapshot pool state. No simulation, no JS scoring, no order created.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
    IntentState,
    QuoteResult,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "test_store.json")


@pytest.fixture
def swap_app_def():
    return AppIntentDefinition(
        app_id="swap_app",
        name="Swap App",
        version="1.0.0",
        intent_type="swap",
        js_code="module.exports = { config: {name: 'swap'}, score: () => ({score: 0.85, valid: true}) }",
        config=AppIntentConfig(supported_chains=[1]),
    )


@pytest.fixture
def active_store(temp_store, swap_app_def):
    temp_store.save_app(swap_app_def)
    temp_store.save_deployment(DeploymentResult(
        app_id="swap_app",
        status=AppStatus.ACTIVE,
        contract_address="0x" + "ab" * 20,
        chain_id=1,
    ))
    return temp_store


def _make_quote_result(estimated: str = "980000000") -> QuoteResult:
    """Build a mock QuoteResult."""
    return QuoteResult(
        estimated_output=estimated,
        route_summary="USDC -> WETH via 0.30% pool",
        gas_estimate=150_000,
        metadata={"pool": "0xpool1", "fee": 3000, "protocol": "UniswapV3"},
    )


def _make_mock_solver(quote_result: QuoteResult | None = None) -> MagicMock:
    """Build a mock solver with quote() support."""
    solver = MagicMock()
    if quote_result is None:
        quote_result = _make_quote_result()
    solver.quote.return_value = quote_result
    return solver


# ── Core quoting logic tests ──────────────────────────────────────────────────


class TestQuoteLogic:
    """Test the core quoting logic."""

    def test_slippage_calculation(self):
        """suggested_min_output = estimated_output * (1 - slippage_bps/10000)."""
        estimated = 1_000_000_000
        slippage_bps = 50  # 0.5%
        expected_min = int(estimated * (10000 - slippage_bps) // 10000)
        assert expected_min == 995_000_000

    def test_slippage_zero(self):
        """With 0 bps slippage, suggested_min_output == estimated_output."""
        estimated = 500_000_000
        slippage_bps = 0
        result = int(estimated * (10000 - slippage_bps) // 10000)
        assert result == estimated

    def test_slippage_clamped_at_10000(self):
        """Slippage > 10000 bps should be clamped to 10000 (100%)."""
        slippage_bps = max(0, min(99999, 10000))
        assert slippage_bps == 10000

    def test_quote_result_fields(self):
        """QuoteResult has all expected fields."""
        qr = _make_quote_result()
        assert qr.estimated_output == "980000000"
        assert qr.gas_estimate == 150_000
        assert "pool" in qr.metadata
        assert qr.route_summary != ""

    def test_quote_result_computed_params_default(self):
        """QuoteResult.computed_params defaults to empty dict."""
        qr = QuoteResult(estimated_output="100")
        assert qr.computed_params == {}
        assert qr.route_summary == ""
        assert qr.gas_estimate == 0


# ── Validator handler tests ────────────────────────────────────────────────────


def _make_validator(store, port: int):
    """Build an AppIntentsValidator with a mocked JS engine (no Node.js needed)."""
    from unittest.mock import MagicMock, patch
    from minotaur_subnet.validator.main import AppIntentsValidator

    mock_engine = MagicMock()
    mock_engine.list_loaded_intents.return_value = []
    mock_engine.timeout_ms = 10000

    with patch("minotaur_subnet.validator.main.JsExecutionEngine", return_value=mock_engine):
        validator = AppIntentsValidator(store=store, port=port)

    # Replace engine with mock after construction too
    validator.engine = mock_engine
    return validator


class TestValidatorQuoteHandler:
    """Test the aiohttp _handle_quote handler in AppIntentsValidator."""

    @pytest.mark.asyncio
    async def test_quote_returns_expected_fields(self, active_store):
        """A valid quote request via solver.quote() returns all expected fields."""
        validator = _make_validator(active_store, 19200)
        solver = _make_mock_solver()
        validator.block_loop.set_solver(solver)

        from aiohttp.test_utils import TestClient, TestServer
        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/apps/swap_app/quote",
                json={
                    "params": {
                        "input_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "output_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                        "input_amount": "1000000000",
                    },
                    "chain_id": 1,
                    "slippage_bps": 50,
                },
            )
            assert resp.status == 200
            data = await resp.json()

            assert data["app_id"] == "swap_app"
            assert data["estimated_output"] == "980000000"
            assert data["valid_for_seconds"] == 30
            assert data["chain_id"] == 1
            assert data["slippage_bps"] == 50
            assert data["gas_estimate"] == 150_000
            assert data["route_summary"] == "USDC -> WETH via 0.30% pool"
            assert "suggested_min_output" in data
            assert "computed_params" in data
            assert isinstance(data["computed_params"], dict)

            # No score or score_breakdown — quoting is not scoring
            assert "score" not in data
            assert "score_breakdown" not in data

    @pytest.mark.asyncio
    async def test_quote_unknown_app_returns_404(self, active_store):
        """Quoting an unknown app_id should return 404."""
        from aiohttp.test_utils import TestClient, TestServer

        validator = _make_validator(active_store, 19201)
        validator.block_loop.set_solver(_make_mock_solver())

        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/apps/nonexistent_app/quote",
                json={"params": {"input_token": "0xA", "output_token": "0xB", "input_amount": "100"}},
            )
            assert resp.status == 404
            data = await resp.json()
            assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_quote_no_solver_returns_503(self, active_store):
        """Without GENESIS_SOLVER_IMAGE, no solver is available -> 503."""
        from aiohttp.test_utils import TestClient, TestServer

        validator = _make_validator(active_store, 19202)
        # No solver — BaselineSwapSolver removed from SDK, no Docker image configured

        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/apps/swap_app/quote",
                json={"params": {"input_token": "0xA", "output_token": "0xB", "input_amount": "100"}},
            )
            assert resp.status == 503
            data = await resp.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_quote_solver_not_implemented_returns_501(self, active_store):
        """When solver.quote() raises NotImplementedError, return 501."""
        from aiohttp.test_utils import TestClient, TestServer

        validator = _make_validator(active_store, 19206)
        solver = MagicMock()
        solver.quote.side_effect = NotImplementedError("no quoting")
        validator.block_loop.set_solver(solver)

        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/apps/swap_app/quote",
                json={"params": {"input_token": "0xA", "output_token": "0xB", "input_amount": "100"}},
            )
            assert resp.status == 501
            data = await resp.json()
            assert "quoting" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_quote_does_not_create_order(self, active_store):
        """Calling quote should NOT add any order to the OrderBook."""
        from aiohttp.test_utils import TestClient, TestServer

        validator = _make_validator(active_store, 19204)
        validator.block_loop.set_solver(_make_mock_solver())

        # stats() returns {status_name: count} — empty dict means no orders
        assert sum(validator.orderbook.stats().values()) == 0

        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/apps/swap_app/quote",
                json={
                    "params": {
                        "input_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "output_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                        "input_amount": "1000000000",
                    },
                },
            )

        # OrderBook must remain empty — quoting is read-only
        assert sum(validator.orderbook.stats().values()) == 0

    @pytest.mark.asyncio
    async def test_quote_suggested_min_output_respects_slippage(self, active_store):
        """suggested_min_output is correctly reduced by slippage_bps."""
        from aiohttp.test_utils import TestClient, TestServer

        validator = _make_validator(active_store, 19205)
        validator.block_loop.set_solver(_make_mock_solver(_make_quote_result("1000000000")))

        app = validator._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/apps/swap_app/quote",
                json={
                    "params": {
                        "input_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "output_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                        "input_amount": "1000000000",
                    },
                    "slippage_bps": 200,  # 2%
                },
            )
            assert resp.status == 200
            data = await resp.json()

            estimated = int(data["estimated_output"])
            suggested = int(data["suggested_min_output"])
            assert estimated == 1_000_000_000
            assert suggested == 980_000_000  # 1B * (10000-200)/10000
            assert suggested <= estimated
