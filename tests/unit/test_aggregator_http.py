"""Tests for AggregatorClient HTTP interactions using mocks."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from neurons.aggregator_client import AggregatorClient, AggregatorClientError


class MockResponse:
    """Mock aiohttp response."""

    def __init__(self, json_data=None, status=200, text=""):
        self._json_data = json_data
        self.status = status
        self._text = text

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """Mock aiohttp ClientSession."""

    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        return MockResponse(json_data={}, status=200)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_fetch_pending_orders_success():
    """Test successful fetch of pending orders."""
    orders = [
        {"orderId": "order-1", "minerId": "miner-a"},
        {"orderId": "order-2", "minerId": "miner-b"},
    ]

    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(json_data=orders)])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", api_key="test-key")
            result = await client.fetch_pending_orders("validator-1")

            assert len(result) == 2
            assert result[0]["orderId"] == "order-1"

    asyncio.run(run_test())


def test_fetch_pending_orders_empty():
    """Test fetch when no pending orders."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(json_data=[])])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000")
            result = await client.fetch_pending_orders("validator-1")

            assert result == []

    asyncio.run(run_test())


def test_fetch_pending_orders_error_returns_empty():
    """Test fetch returns empty list on error."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(status=500, text="Internal error")])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", max_retries=0)
            result = await client.fetch_pending_orders("validator-1")

            assert result == []

    asyncio.run(run_test())


def test_submit_validation_success():
    """Test successful validation submission."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(json_data={"success": True})])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", api_key="test-key")
            result = await client.submit_validation(
                order_id="order-123",
                validator_id="validator-1",
                success=True,
                notes="Simulation passed"
            )

            assert result is True
            assert mock_session.requests[0]["method"] == "POST"

    asyncio.run(run_test())


def test_submit_validation_failure():
    """Test validation submission failure."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(status=400, text="Bad request")])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", max_retries=0)
            result = await client.submit_validation(
                order_id="order-123",
                validator_id="validator-1",
                success=False
            )

            assert result is False

    asyncio.run(run_test())


def test_fetch_health_success():
    """Test successful health check."""
    health_data = {"status": "healthy", "storage": {"healthy": True}}

    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(json_data=health_data)])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000")
            result = await client.fetch_health()

            assert result == health_data
            assert result["status"] == "healthy"

    asyncio.run(run_test())


def test_fetch_health_unhealthy():
    """Test health check when aggregator is unhealthy."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(status=503, text="Service unavailable")])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", max_retries=0)
            result = await client.fetch_health()

            assert result is None

    asyncio.run(run_test())


def test_api_key_header_included():
    """Test that API key is included in request headers."""
    async def run_test():
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = MockSession([MockResponse(json_data=[])])
            mock_session_cls.return_value = mock_session

            client = AggregatorClient(base_url="http://localhost:4000", api_key="secret-key")
            await client.fetch_pending_orders("validator-1")

            headers = mock_session.requests[0].get("headers", {})
            assert headers.get("X-API-Key") == "secret-key"

    asyncio.run(run_test())


def test_retry_configuration():
    """Test that retry parameters are properly configured."""
    client = AggregatorClient(
        base_url="http://localhost:4000",
        max_retries=5,
        backoff_seconds=2.0
    )

    assert client.max_retries == 5
    assert client.backoff_seconds == 2.0


def test_max_retries_minimum():
    """Test that max_retries has a minimum of 0."""
    client = AggregatorClient(base_url="http://localhost:4000", max_retries=-1)

    assert client.max_retries == 0


def test_timeout_configuration():
    """Test that timeout is properly configured."""
    client = AggregatorClient(base_url="http://localhost:4000", timeout=30)

    assert client.timeout == 30


def test_base_url_normalization():
    """Test that base URL trailing slash is stripped."""
    client = AggregatorClient(base_url="http://localhost:4000/")

    assert client.base_url == "http://localhost:4000"


def test_ssl_verification_disabled():
    """Test that SSL verification can be disabled."""
    client = AggregatorClient(base_url="https://localhost:4000", verify_ssl=False)

    assert client.verify_ssl is False
