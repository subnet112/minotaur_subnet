"""Tests for OnchainWeightsEmitter weight processing utilities."""
import numpy as np
import pytest

from neurons.onchain_emitter import (
    _normalize_max_weight,
    process_weights_for_netuid,
    _Node,
)


class FakeSubstrate:
    def __init__(self, min_allowed_weights: int = 8, max_weight_limit: float = 0.1):
        self._min_allowed_weights = min_allowed_weights
        self._max_weight_limit = max_weight_limit

    def query(self, module: str, storage: str, params=None):
        if storage == "MinAllowedWeights":
            return FakeValue(self._min_allowed_weights)
        if storage == "MaxWeightsLimit":
            # MaxWeightsLimit is stored as u16 ratio of U16_MAX
            return FakeValue(int(self._max_weight_limit * 65535))
        return None


class FakeValue:
    def __init__(self, value):
        self.value = value


def test_normalize_max_weight_respects_limit():
    weights = np.array([0.5, 0.3, 0.2], dtype=np.float32)
    normalized = _normalize_max_weight(weights, limit=0.4)

    assert normalized.max() <= 0.4 + 1e-6
    assert abs(normalized.sum() - 1.0) < 1e-6


def test_normalize_max_weight_no_change_needed():
    weights = np.array([0.25, 0.25, 0.25, 0.25], dtype=np.float32)
    normalized = _normalize_max_weight(weights, limit=0.5)

    assert abs(normalized.sum() - 1.0) < 1e-6
    np.testing.assert_allclose(normalized, weights, rtol=1e-6)


def test_normalize_max_weight_handles_zeros():
    weights = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    normalized = _normalize_max_weight(weights, limit=0.5)

    # With all zeros, should distribute equally
    assert abs(normalized.sum() - 1.0) < 1e-6


def test_normalize_max_weight_single_element():
    weights = np.array([1.0], dtype=np.float32)
    normalized = _normalize_max_weight(weights, limit=0.1)

    # Single element should get 100% regardless of limit
    assert abs(normalized.sum() - 1.0) < 1e-6


def test_process_weights_filters_zero_weights():
    nodes = [
        _Node(node_id=0, hotkey="hk0"),
        _Node(node_id=1, hotkey="hk1"),
        _Node(node_id=2, hotkey="hk2"),
    ]
    uids = np.array([0, 1, 2], dtype=np.int64)
    weights = np.array([0.5, 0.0, 0.5], dtype=np.float32)

    substrate = FakeSubstrate(min_allowed_weights=2)

    node_ids, node_weights = process_weights_for_netuid(
        uids=uids,
        weights=weights,
        netuid=1,
        substrate=substrate,
        nodes=nodes,
    )

    # Should include non-zero weights
    assert len(node_ids) >= 2
    assert abs(sum(node_weights) - 1.0) < 1e-6


def test_process_weights_pads_to_min_allowed():
    nodes = [
        _Node(node_id=i, hotkey=f"hk{i}")
        for i in range(10)
    ]
    uids = np.array(list(range(10)), dtype=np.int64)
    # Only 2 non-zero weights, but min_allowed is 8
    weights = np.zeros(10, dtype=np.float32)
    weights[0] = 0.6
    weights[1] = 0.4

    substrate = FakeSubstrate(min_allowed_weights=8)

    node_ids, node_weights = process_weights_for_netuid(
        uids=uids,
        weights=weights,
        netuid=1,
        substrate=substrate,
        nodes=nodes,
    )

    # Should pad to meet minimum
    assert len(node_ids) >= 8
    assert abs(sum(node_weights) - 1.0) < 1e-6


def test_process_weights_handles_empty_nodes():
    nodes = []
    uids = np.array([], dtype=np.int64)
    weights = np.array([], dtype=np.float32)

    substrate = FakeSubstrate(min_allowed_weights=8)

    node_ids, node_weights = process_weights_for_netuid(
        uids=uids,
        weights=weights,
        netuid=1,
        substrate=substrate,
        nodes=nodes,
    )

    assert node_ids == []
    assert node_weights == []


def test_process_weights_respects_max_weight_limit():
    nodes = [
        _Node(node_id=i, hotkey=f"hk{i}")
        for i in range(5)
    ]
    uids = np.array([0, 1, 2, 3, 4], dtype=np.int64)
    # One node has very high weight
    weights = np.array([0.9, 0.025, 0.025, 0.025, 0.025], dtype=np.float32)

    substrate = FakeSubstrate(min_allowed_weights=5, max_weight_limit=0.3)

    node_ids, node_weights = process_weights_for_netuid(
        uids=uids,
        weights=weights,
        netuid=1,
        substrate=substrate,
        nodes=nodes,
    )

    # Max weight should be clamped
    assert max(node_weights) <= 0.3 + 1e-6
    assert abs(sum(node_weights) - 1.0) < 1e-6

