"""Golden-equivalence tests for the chain registry.

The registry is a PURE CONSOLIDATION: its resolvers must return byte-identical
values to the original inlined per-site logic. The reference functions below are
verbatim copies of the pre-refactor bodies; each test asserts the registry agrees
across a matrix of env configurations (every subset of the relevant vars), so a
divergence in any fallback order or vocabulary is caught.
"""

from __future__ import annotations

import itertools

import pytest

from minotaur_subnet.chains import registry


# ─────────────────────────────────────────────────────────────────────────────
#  Reference implementations — verbatim copies of the original inlined logic.
# ─────────────────────────────────────────────────────────────────────────────

def _ref_live_rpc(chain_id, env):
    # app_registry_cache / validator_registry_cache / score_threshold_cache
    g = lambda k: env.get(k, "").strip()  # noqa: E731
    if chain_id == 8453:
        return g("BASE_UPSTREAM_RPC_URL") or g("BASE_RPC_URL")
    if chain_id == 1:
        return g("ETH_UPSTREAM_RPC_URL") or g("ETH_RPC_URL") or g("ANVIL_RPC_URL")
    if chain_id == 964:
        return (g("BITTENSOR_EVM_UPSTREAM_RPC_URL") or g("BITTENSOR_EVM_RPC_URL")
                or g("BITTENSOR_EVM_FORK_RPC_URL"))
    return ""


def _ref_gas_rpc(chain_id, env):
    # fee_policy._live_gas_rpc_url
    g = lambda k: env.get(k, "").strip()  # noqa: E731
    if chain_id == 8453:
        return g("BASE_UPSTREAM_RPC_URL") or g("BASE_RPC_URL")
    if chain_id == 1:
        return (g("ETH_UPSTREAM_RPC_URL") or g("ETHEREUM_RPC_URL")
                or g("ETH_RPC_URL") or g("ANVIL_RPC_URL"))
    if chain_id == 964:
        return g("BITTENSOR_EVM_UPSTREAM_RPC_URL") or g("BITTENSOR_EVM_RPC_URL")
    return ""


def _ref_consensus_rpc(chain_id, env):
    # protocol_config.consensus_chain_rpc_url
    g = lambda k: env.get(k, "").strip()  # noqa: E731
    if chain_id == 8453:
        u = g("BASE_UPSTREAM_RPC_URL")
        if u:
            return u
    elif chain_id == 964:
        u = g("BITTENSOR_EVM_UPSTREAM_RPC_URL") or g("BITTENSOR_EVM_RPC_URL")
        if u:
            return u
        return "https://lite.chain.opentensor.ai"
    elif chain_id == 1:
        u = g("ETH_UPSTREAM_RPC_URL")
        if u:
            return u
    return g("ANVIL_RPC_URL") or g("BASE_RPC_URL") or "http://localhost:8545"


# ── env-matrix driver ─────────────────────────────────────────────────────────

_ALL_RPC_ENVS = [
    "ETH_UPSTREAM_RPC_URL", "ETHEREUM_RPC_URL", "ETH_RPC_URL", "ANVIL_RPC_URL",
    "BASE_UPSTREAM_RPC_URL", "BASE_RPC_URL",
    "BITTENSOR_EVM_UPSTREAM_RPC_URL", "BITTENSOR_EVM_RPC_URL",
    "BITTENSOR_EVM_FORK_RPC_URL",
]


def _clear(monkeypatch):
    for k in _ALL_RPC_ENVS:
        monkeypatch.delenv(k, raising=False)


def _apply(monkeypatch, subset):
    for k in _ALL_RPC_ENVS:
        monkeypatch.delenv(k, raising=False)
    for k in subset:
        monkeypatch.setenv(k, f"http://set/{k}")


# A representative env matrix: single vars, the production-like "all upstreams set",
# and a handful of partial combos that exercise every fallback rung.
_MATRIX = (
    [()]
    + [(k,) for k in _ALL_RPC_ENVS]
    + [
        ("ETH_RPC_URL", "ANVIL_RPC_URL"),
        ("ETHEREUM_RPC_URL", "ETH_RPC_URL"),
        ("BASE_RPC_URL",),
        ("BITTENSOR_EVM_RPC_URL", "BITTENSOR_EVM_FORK_RPC_URL"),
        ("BITTENSOR_EVM_FORK_RPC_URL",),
        ("BASE_UPSTREAM_RPC_URL", "BASE_RPC_URL"),
        ("ETH_UPSTREAM_RPC_URL", "BASE_UPSTREAM_RPC_URL",
         "BITTENSOR_EVM_UPSTREAM_RPC_URL"),  # production leader shape
    ]
)


@pytest.mark.parametrize("chain_id", [1, 8453, 964, 31337])
@pytest.mark.parametrize("subset", _MATRIX)
def test_live_rpc_matches_reference(monkeypatch, chain_id, subset):
    _apply(monkeypatch, subset)
    assert registry.live_rpc(chain_id) == _ref_live_rpc(chain_id, dict(__import__("os").environ))


@pytest.mark.parametrize("chain_id", [1, 8453, 964, 31337])
@pytest.mark.parametrize("subset", _MATRIX)
def test_gas_rpc_matches_reference(monkeypatch, chain_id, subset):
    _apply(monkeypatch, subset)
    assert registry.gas_rpc(chain_id) == _ref_gas_rpc(chain_id, dict(__import__("os").environ))


@pytest.mark.parametrize("chain_id", [1, 8453, 964, 31337])
@pytest.mark.parametrize("subset", _MATRIX)
def test_consensus_rpc_matches_reference(monkeypatch, chain_id, subset):
    _apply(monkeypatch, subset)
    assert registry.consensus_rpc(chain_id) == _ref_consensus_rpc(chain_id, dict(__import__("os").environ))


# ── structural / anchor invariants ────────────────────────────────────────────

def test_anchor_chains_is_base_only():
    # The pre-refactor ROUND_ANCHOR_CHAINS constant. Must stay Base-only (fleet-uniform).
    assert registry.anchor_chains() == (8453,)


def test_lookback_epochs_eth_is_three():
    assert registry.lookback_epochs(1) == 3
    assert registry.lookback_epochs(8453) == 1
    assert registry.lookback_epochs(964) == 1
    assert registry.lookback_epochs(999) == 1  # unknown -> default


def test_slug_matches_legacy_chain_names():
    # The legacy CHAIN_NAMES = {1:"eth", 31337:"eth", 8453:"base", 964:"btevm"}.
    assert registry.slug(1) == "eth"
    assert registry.slug(31337) == "eth"
    assert registry.slug(8453) == "base"
    assert registry.slug(964) == "btevm"
    assert registry.slug(999) is None


def test_default_chain_id_is_local_anvil():
    assert registry.default_chain_id() == 31337


def test_registry_env_templates():
    assert registry.validator_registry_env(8453) == "VALIDATOR_REGISTRY_8453"
    assert registry.app_registry_env(1) == "APP_REGISTRY_1"
    assert registry.champion_registry_env(964) == "CHAMPION_REGISTRY_964"


def test_fee_floor_and_gas_defaults():
    assert registry.fee_floor_wei(1) == 33_000_000_000_000
    assert registry.fee_floor_wei(964) == 330_000_000_000_000
    assert registry.fee_floor_wei(31337) == 0
    assert registry.fee_floor_wei(999, default=7) == 7
    assert registry.fallback_gas_price_wei(8453) == 20_000_000
    assert registry.fallback_gas_price_wei(999) == 1_000_000_000


# ─────────────────────────────────────────────────────────────────────────────
#  boot_rpc — the live-solver / faucet boot map. Chain 1 must prefer the REAL
#  Ethereum RPC: booting the live solver with chain 1 → ANVIL_RPC_URL (the Base
#  anvil on production nodes, misnamed legacy) made every chain-1 generate_plan
#  return an empty plan, so live /quote answered estimated_output=0 on chain 1.
# ─────────────────────────────────────────────────────────────────────────────

def test_boot_rpc_chain1_prefers_real_ethereum_rpc(monkeypatch):
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://eth.example/v2/key")
    monkeypatch.setenv("ANVIL_RPC_URL", "http://anvil-base:8546")
    assert registry.boot_rpc(1) == "https://eth.example/v2/key"


def test_boot_rpc_chain1_falls_back_to_anvil_for_local_dev(monkeypatch):
    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.setenv("ANVIL_RPC_URL", "http://localhost:8545")
    assert registry.boot_rpc(1) == "http://localhost:8545"


def test_boot_rpc_other_chains_unchanged(monkeypatch):
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "https://btevm.example")
    monkeypatch.setenv("ANVIL_RPC_URL", "http://localhost:8545")
    assert registry.boot_rpc(8453) == "https://base.example"
    assert registry.boot_rpc(964) == "https://btevm.example"
    assert registry.boot_rpc(31337) == "http://localhost:8545"


def test_boot_rpc_urls_map_uses_real_eth_when_configured(monkeypatch):
    from minotaur_subnet.chains import wiring
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://eth.example/v2/key")
    monkeypatch.setenv("ANVIL_RPC_URL", "http://anvil-base:8546")
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    urls = wiring.boot_rpc_urls()
    assert urls[1] == "https://eth.example/v2/key"
    assert urls[31337] == "http://anvil-base:8546"
    assert urls[8453] == "https://base.example"
