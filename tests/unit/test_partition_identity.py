"""Partition identity-space resolution for the Stage-2 coverage partition.

Regression guard: the partition matches this node against the validator set, so
the self id MUST be in the set's space (EVM address from the consensus manager),
NOT the SS58 hotkey used for the diverse-subset seed. If the spaces mismatch,
`me in set` is always False and the partition silently degrades to the
(overlapping) diverse draw — coverage never actually spreads across validators.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


def _worker(*, identity, set_provider=None, self_id_provider=None):
    return BenchmarkWorker(
        submission_store=MagicMock(),
        validator_identity=identity,
        validator_set_provider=set_provider,
        validator_self_id_provider=self_id_provider,
    )


@pytest.fixture
def quorum_on(monkeypatch):
    monkeypatch.setenv("CHALLENGER_QUORUM_MODE", "1")


def test_partition_active_when_self_id_in_set(quorum_on):
    vset = ["0xCCC", "0xAAA", "0xBBB"]  # unsorted on purpose
    w = _worker(
        identity="5HotkeySS58",  # wrong space; must NOT be used for matching
        set_provider=lambda: vset,
        self_id_provider=lambda: "0xBBB",
    )
    seed, idx, count = w._resolve_stage2_partition()
    assert seed is None
    assert count == 3
    # index is into the SORTED set (deterministic across validators)
    assert idx == sorted(vset).index("0xBBB")


def test_falls_back_to_diverse_when_self_id_not_in_set(quorum_on):
    # The bug this guards: an SS58 hotkey self-id never matches an EVM set.
    w = _worker(
        identity="5HotkeySS58",
        set_provider=lambda: ["0xAAA", "0xBBB"],
        self_id_provider=lambda: "5HotkeySS58",  # hotkey, not in the EVM set
    )
    seed, idx, count = w._resolve_stage2_partition()
    assert seed == "5HotkeySS58"  # diverse-subset fallback (still stable)
    assert idx is None and count is None


def test_diverse_fallback_when_no_self_provider(quorum_on):
    w = _worker(
        identity="5Hotkey",
        set_provider=lambda: ["0xAAA"],
        self_id_provider=None,
    )
    seed, idx, count = w._resolve_stage2_partition()
    assert seed == "5Hotkey" and idx is None and count is None


def test_shared_draw_when_quorum_mode_off(monkeypatch):
    monkeypatch.delenv("CHALLENGER_QUORUM_MODE", raising=False)
    w = _worker(
        identity="5Hotkey",
        set_provider=lambda: ["0xAAA"],
        self_id_provider=lambda: "0xAAA",
    )
    seed, idx, count = w._resolve_stage2_partition()
    # quorum mode off -> shared round-only draw, no partition, no diverse seed
    assert seed is None and idx is None and count is None


def test_resilient_to_provider_exceptions(quorum_on):
    def boom():
        raise RuntimeError("provider down")

    w = _worker(identity="5Hotkey", set_provider=boom, self_id_provider=boom)
    seed, idx, count = w._resolve_stage2_partition()
    # both providers raise -> empty set / no self -> diverse fallback, no crash
    assert seed == "5Hotkey" and idx is None and count is None


def test_all_validators_agree_on_indices(quorum_on):
    # Every validator resolves the SAME sorted set -> distinct, total indices.
    vset = ["0xBBB", "0xAAA", "0xCCC"]
    indices = []
    for me in vset:
        w = _worker(
            identity=f"hotkey-{me}",
            set_provider=lambda: list(vset),
            self_id_provider=(lambda m=me: m),
        )
        _, idx, count = w._resolve_stage2_partition()
        assert count == 3
        indices.append(idx)
    assert sorted(indices) == [0, 1, 2]  # disjoint, cover [0, V)
