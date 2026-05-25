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


# TestValidatorQuoteHandler removed 2026-05-25 — the underlying
# ``POST /apps/{id}/quote`` route on the validator daemon was deleted in
# PR #28 (validator-surface cleanup). The api at port 8080 carries the
# equivalent live route (``POST /v1/apps/{id}/quote``) and is covered by
# the integration tests in ``tests/testnet/test_local_testnet.py``.

