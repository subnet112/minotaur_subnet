"""Tests for AggregatorClient payload building and utilities."""
import pytest

from neurons.aggregator_client import AggregatorClient


def test_canonical_weights_payload_format():
    client = AggregatorClient(base_url="http://localhost:4000")

    payload = client._build_canonical_weights_payload(
        validator_id="validator-123",
        epoch_key="epoch-42",
        timestamp="2025-01-01T00:00:00Z",
        block_number=12345,
        weights={"miner-a": 0.6, "miner-b": 0.4},
        stats={
            "totalSimulations": 100,
            "validMiners": 2,
            "totalMiners": 5,
            "burnPercentage": 0.1,
        },
    )

    lines = payload.split("\n")

    assert lines[0] == "validator-weights"
    assert lines[1] == "validator-123"
    assert lines[2] == "epoch-42"
    assert lines[3] == "2025-01-01T00:00:00Z"
    assert lines[4] == "12345"
    # Weights line: sorted keys:sorted values
    assert "miner-a,miner-b" in lines[5]
    assert lines[6] == "100"
    assert lines[7] == "2"
    assert lines[8] == "5"
    assert lines[9] == "0.1"


def test_canonical_weights_payload_empty_weights():
    client = AggregatorClient(base_url="http://localhost:4000")

    payload = client._build_canonical_weights_payload(
        validator_id="validator",
        epoch_key="epoch",
        timestamp="2025-01-01T00:00:00Z",
        block_number=None,
        weights={},
        stats={
            "totalSimulations": 0,
            "validMiners": 0,
            "totalMiners": 0,
            "burnPercentage": 0.0,
        },
    )

    lines = payload.split("\n")

    # Empty weights should produce ":"
    assert lines[5] == ":"
    # No block number should be empty line
    assert lines[4] == ""


def test_canonical_weights_payload_deterministic_ordering():
    client = AggregatorClient(base_url="http://localhost:4000")

    weights = {"zebra": 0.1, "alpha": 0.3, "middle": 0.6}
    stats = {"totalSimulations": 0, "validMiners": 0, "totalMiners": 0, "burnPercentage": 0.0}

    payload1 = client._build_canonical_weights_payload(
        validator_id="v", epoch_key="e", timestamp="t", block_number=1,
        weights=weights, stats=stats,
    )
    payload2 = client._build_canonical_weights_payload(
        validator_id="v", epoch_key="e", timestamp="t", block_number=1,
        weights=weights, stats=stats,
    )

    # Should be deterministic
    assert payload1 == payload2

    # Keys should be sorted alphabetically
    lines = payload1.split("\n")
    assert "alpha,middle,zebra" in lines[5]


def test_canonical_weights_payload_decimal_formatting():
    client = AggregatorClient(base_url="http://localhost:4000")

    payload = client._build_canonical_weights_payload(
        validator_id="v",
        epoch_key="e",
        timestamp="t",
        block_number=1,
        weights={"a": 0.333333333333, "b": 0.666666666666},
        stats={"totalSimulations": 0, "validMiners": 0, "totalMiners": 0, "burnPercentage": 0.0},
    )

    lines = payload.split("\n")
    # Should not have trailing zeros
    assert "0.333333333333" in lines[5] or "0.33333333" in lines[5]


def test_aggregator_client_initialization():
    client = AggregatorClient(
        base_url="http://localhost:4000",
        api_key="test-key",
        timeout=30,
        verify_ssl=False,
        max_retries=5,
        backoff_seconds=1.0,
        page_limit=100,
    )

    assert client.base_url == "http://localhost:4000"
    assert client.api_key == "test-key"
    assert client.timeout == 30
    assert client.verify_ssl is False
    assert client.max_retries == 5
    assert client.backoff_seconds == 1.0
    assert client.page_limit == 100


def test_aggregator_client_strips_trailing_slash():
    client = AggregatorClient(base_url="http://localhost:4000/")

    assert client.base_url == "http://localhost:4000"


def test_aggregator_client_page_limit_bounds():
    # Page limit should be bounded between 1 and 1000
    client = AggregatorClient(base_url="http://localhost", page_limit=0)
    assert client.page_limit >= 1

    client = AggregatorClient(base_url="http://localhost", page_limit=9999)
    assert client.page_limit <= 1000

