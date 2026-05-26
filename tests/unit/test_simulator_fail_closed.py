"""Regression tests for PR-3 — simulator fail-closed + scoreThreshold fix.

Audit findings closed:
* C3 — silent fallback to leader-supplied simulation when local Anvil down.
* H7 — hardcoded score_bps=5000 in ConsensusManager.sign_approval / verify.
"""
from __future__ import annotations

import os
import pytest

from minotaur_subnet.consensus import score_threshold_cache
from minotaur_subnet.consensus.dissent import RejectionCode


def test_simulator_unavailable_in_rejection_codes() -> None:
    """C3: RejectionCode enum must expose SIMULATOR_UNAVAILABLE."""
    assert RejectionCode.SIMULATOR_UNAVAILABLE.value == "SIMULATOR_UNAVAILABLE"
    # Must be distinct from SIMULATION_FAILED — the two describe different
    # failure modes (sim reverted vs sim couldn't run).
    assert (
        RejectionCode.SIMULATOR_UNAVAILABLE.value
        != RejectionCode.SIMULATION_FAILED.value
    )


def test_score_threshold_cache_falls_back_when_rpc_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H7: When chain has no RPC env, fall back to the legacy 5000 default."""
    # Wipe any RPC env vars for chain 8453
    for var in ("BASE_RPC_URL", "BASE_SIM_RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    score_threshold_cache.invalidate()
    threshold = score_threshold_cache.score_threshold_for(
        "0x" + "ab" * 20, 8453, fallback_bps=5000,
    )
    assert threshold == 5000


def test_score_threshold_cache_falls_back_on_bad_address() -> None:
    """H7: Empty/invalid contract address returns fallback without RPC call."""
    score_threshold_cache.invalidate()
    assert score_threshold_cache.score_threshold_for("", 8453) == 5000
    assert score_threshold_cache.score_threshold_for("not-an-address", 8453) == 5000


def test_score_threshold_cache_does_not_cache_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H7: Soft-failures must NOT poison the cache. A subsequent call after
    the RPC comes back must re-attempt the read.

    (Verified by checking the cache dict directly — score_threshold_for
    returns fallback on failure but doesn't write to _CACHE.)
    """
    for var in ("BASE_RPC_URL", "BASE_SIM_RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    score_threshold_cache.invalidate()
    contract = "0x" + "cd" * 20
    score_threshold_cache.score_threshold_for(contract, 8453)
    # Cache must be empty for this key after a fallback
    assert (8453, contract.lower()) not in score_threshold_cache._CACHE


def test_score_threshold_invalidate_single_key() -> None:
    """Operator escape hatch: invalidate one entry without dumping the cache."""
    score_threshold_cache.invalidate()
    score_threshold_cache._CACHE[(8453, "0xaa")] = 6000
    score_threshold_cache._CACHE[(964, "0xbb")] = 7000
    score_threshold_cache.invalidate("0xaa", 8453)
    assert (8453, "0xaa") not in score_threshold_cache._CACHE
    assert (964, "0xbb") in score_threshold_cache._CACHE
