"""Tests for ``consensus_chain_rpc_url``.

Locks in the per-chain RPC selection that drives consensus on-chain
reads. The function must prefer ``*_UPSTREAM_RPC_URL`` env vars over
Anvil-fork URLs, because Anvil forks freeze at the fork point and miss
on-chain updateValidators / setQuorumBps writes — the root cause of the
2026-05-26 registration-visibility incident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.protocol_config import consensus_chain_rpc_url


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every env var the helper consults."""
    for name in (
        "BASE_UPSTREAM_RPC_URL",
        "BITTENSOR_EVM_UPSTREAM_RPC_URL",
        "BITTENSOR_EVM_RPC_URL",
        "ETH_UPSTREAM_RPC_URL",
        "ANVIL_RPC_URL",
        "BASE_RPC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ── chain 8453 (Base) ────────────────────────────────────────────────


def test_base_prefers_upstream(clean_env):
    clean_env.setenv("BASE_UPSTREAM_RPC_URL", "https://base-mainnet.example.com/v2/key")
    clean_env.setenv("BASE_RPC_URL", "http://anvil-base:8546")
    assert consensus_chain_rpc_url(8453) == "https://base-mainnet.example.com/v2/key"


def test_base_falls_through_to_anvil_when_no_upstream(clean_env):
    """Local testnet path: no upstream configured, Anvil IS live chain."""
    clean_env.setenv("BASE_RPC_URL", "http://anvil-base:8546")
    assert consensus_chain_rpc_url(8453) == "http://anvil-base:8546"


def test_base_empty_upstream_skipped(clean_env):
    """Whitespace-only BASE_UPSTREAM_RPC_URL falls through (real misconfig case)."""
    clean_env.setenv("BASE_UPSTREAM_RPC_URL", "   ")
    clean_env.setenv("BASE_RPC_URL", "http://anvil-base:8546")
    assert consensus_chain_rpc_url(8453) == "http://anvil-base:8546"


# ── chain 964 (BT EVM) ──────────────────────────────────────────────


def test_btevm_prefers_explicit_upstream(clean_env):
    clean_env.setenv("BITTENSOR_EVM_UPSTREAM_RPC_URL", "https://btevm.example.com")
    clean_env.setenv("BITTENSOR_EVM_RPC_URL", "http://anvil-btevm:8547")
    assert consensus_chain_rpc_url(964) == "https://btevm.example.com"


def test_btevm_falls_back_to_btevm_rpc(clean_env):
    """BITTENSOR_EVM_RPC_URL is the legacy single-name var; honor it."""
    clean_env.setenv("BITTENSOR_EVM_RPC_URL", "http://anvil-btevm:8547")
    assert consensus_chain_rpc_url(964) == "http://anvil-btevm:8547"


def test_btevm_hardcoded_public_default_when_unset(clean_env):
    """BT EVM has a known-good public endpoint as the last-resort fallback."""
    assert consensus_chain_rpc_url(964) == "https://lite.chain.opentensor.ai"


# ── chain 1 (Ethereum mainnet) ───────────────────────────────────────


def test_eth_prefers_upstream(clean_env):
    clean_env.setenv("ETH_UPSTREAM_RPC_URL", "https://eth-mainnet.example.com/v2/key")
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil-eth:8545")
    assert consensus_chain_rpc_url(1) == "https://eth-mainnet.example.com/v2/key"


def test_eth_falls_back_to_anvil(clean_env):
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil-eth:8545")
    assert consensus_chain_rpc_url(1) == "http://anvil-eth:8545"


# ── chain 31337 / unknown (local Anvil) ─────────────────────────────


def test_local_testnet_uses_anvil_url(clean_env):
    """31337 has no upstream — Anvil URL IS the live chain there."""
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil:8545")
    assert consensus_chain_rpc_url(31337) == "http://anvil:8545"


def test_unknown_chain_falls_back_to_anvil(clean_env):
    """Future chains we haven't taught the helper about default safely."""
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil:8545")
    assert consensus_chain_rpc_url(42161) == "http://anvil:8545"


def test_no_env_falls_back_to_localhost(clean_env):
    """Final fallback — used by tests + dev environments with no compose."""
    assert consensus_chain_rpc_url(31337) == "http://localhost:8545"


# ── upstream preference: validates the fix for the registration bug ──


def test_base_upstream_wins_over_anvil_when_both_set(clean_env):
    """The whole point of this helper: when prod has BOTH the Anvil
    fork URL AND the live upstream URL, we MUST pick the upstream so
    on-chain writes are visible without a fork recycle."""
    clean_env.setenv("BASE_UPSTREAM_RPC_URL", "https://base-mainnet.example.com/key")
    clean_env.setenv("BASE_RPC_URL", "http://anvil-base:8546")
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil:8545")
    result = consensus_chain_rpc_url(8453)
    # Critical: not the Anvil URL
    assert "anvil" not in result
    assert result == "https://base-mainnet.example.com/key"


def test_btevm_upstream_wins_over_anvil_when_both_set(clean_env):
    clean_env.setenv("BITTENSOR_EVM_UPSTREAM_RPC_URL", "https://btevm.example.com")
    clean_env.setenv("BITTENSOR_EVM_RPC_URL", "http://anvil-btevm:8547")
    result = consensus_chain_rpc_url(964)
    assert "anvil" not in result


def test_eth_upstream_wins_over_anvil_when_both_set(clean_env):
    clean_env.setenv("ETH_UPSTREAM_RPC_URL", "https://eth.example.com")
    clean_env.setenv("ANVIL_RPC_URL", "http://anvil:8545")
    result = consensus_chain_rpc_url(1)
    assert "anvil" not in result


# ── call-site regression guards ──────────────────────────────────────


def test_validator_main_uses_consensus_chain_rpc_url():
    """validator/main.py must call consensus_chain_rpc_url, not pick
    ANVIL_RPC_URL / BASE_RPC_URL directly. Regression guard for the
    2026-05-26 incident."""
    src = (_REPO_ROOT / "minotaur_subnet" / "validator" / "main.py").read_text()
    assert "consensus_chain_rpc_url" in src, (
        "validator/main.py must call consensus_chain_rpc_url(chain_id) "
        "to read consensus state from upstream, not the Anvil fork"
    )
    # Ensure the old bug pattern isn't still in there alongside the fix
    assert 'anvil_rpc = (\n            os.environ.get("ANVIL_RPC_URL")' not in src, (
        "Old Anvil-first selection pattern leaked back into validator/main.py"
    )


def test_api_startup_uses_consensus_chain_rpc_url():
    """api/startup.py order-consensus must call consensus_chain_rpc_url."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    assert "consensus_chain_rpc_url" in src
    # Specifically, the order_rpc_url assignment must use the helper
    assert "order_rpc_url = consensus_chain_rpc_url(" in src
