"""The JS sandbox gets its per-chain RPC URLs from the chain registry.

``runner.js`` builds its ``RPC_URLS`` map from two legacy env names plus
``RPC_URL_<chain_id>`` overrides applied afterwards:

    if (ANVIL_RPC_URL) { RPC_URLS[1] = …; RPC_URLS[31337] = …; }
    if (BASE_RPC_URL)  { RPC_URLS[8453] = …; }
    for (RPC_URL_<n> in env) RPC_URLS[n] = …

Three defects followed from that, all fixed by deriving the map from the
registry's SIM role instead:

1. Chain 964 had NO entry at all, so ``ethCall(964, …)`` threw
   ``No RPC URL configured for chain 964`` — the yield app's on-chain
   verification could never run. Leaving this to a per-operator env var would
   have fixed only the nodes we operate.
2. ``ANVIL_RPC_URL`` is the *Base* anvil on prod, so chain 1 was served Base
   state — the split-brain class the registry docstring warns about.
3. ``BASE_RPC_URL`` is the *live* archive, so chain 8453 read an unpinned head
   rather than the round-pinned fork, making scoring non-deterministic across
   validators.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.engine.sandbox import _sandbox_child_env  # noqa: E402

# Mirrors the leader's real values (`docker exec production-api-1 printenv`),
# including the trap: ANVIL_RPC_URL points at the BASE anvil.
_PROD_LIKE = {
    "ETH_SIM_RPC_URL": "http://anvil-eth:8545",
    "BASE_SIM_RPC_URL": "http://anvil-base:8546",
    "BITTENSOR_CHOPSTICKS_SIM_RPC_URL": "http://chopsticks-btevm:8545",
    "ANVIL_RPC_URL": "http://anvil-base:8546",
    "BASE_RPC_URL": "https://rpc-base.example/?authorization=live-credential",
}


@pytest.fixture
def prod_like_env(monkeypatch):
    for k in list(_PROD_LIKE) + ["RPC_URL_1", "RPC_URL_964", "RPC_URL_8453"]:
        monkeypatch.delenv(k, raising=False)
    for k, v in _PROD_LIKE.items():
        monkeypatch.setenv(k, v)
    return _sandbox_child_env()


def test_chain_964_is_wired(prod_like_env):
    """The defect that started this: 964 had no URL on any node."""
    assert prod_like_env["RPC_URL_964"] == "http://chopsticks-btevm:8545"


def test_chain_1_is_not_served_the_base_anvil(prod_like_env):
    """ANVIL_RPC_URL is the Base anvil on prod; chain 1 must not inherit it."""
    assert prod_like_env["RPC_URL_1"] == "http://anvil-eth:8545"
    assert prod_like_env["RPC_URL_1"] != prod_like_env["RPC_URL_8453"]


def test_chain_8453_uses_the_pinned_fork_not_the_live_archive(prod_like_env):
    """Reading live head instead of the pinned fork breaks determinism."""
    assert prod_like_env["RPC_URL_8453"] == "http://anvil-base:8546"
    assert "authorization" not in prod_like_env["RPC_URL_8453"]


def test_explicit_override_still_wins(monkeypatch):
    """The RPC_URL_<chain_id> escape hatch must keep precedence."""
    for k, v in _PROD_LIKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RPC_URL_964", "http://operator-choice:9999")
    assert _sandbox_child_env()["RPC_URL_964"] == "http://operator-choice:9999"


def test_unconfigured_chain_is_absent_rather_than_empty(monkeypatch):
    """A chain with no sim RPC gets no key, so runner.js fails loudly.

    An empty-string URL would be worse than a missing one: the guest would
    attempt a request against "" instead of getting the explicit
    "No RPC URL configured for chain N".
    """
    for k in _PROD_LIKE:
        monkeypatch.delenv(k, raising=False)
    env = _sandbox_child_env()
    assert not any(v == "" for k, v in env.items() if k.startswith("RPC_URL_"))


def test_no_secret_reaches_the_child(monkeypatch):
    """The registry layer must not widen the allowlist."""
    for k, v in _PROD_LIKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RELAYER_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("VALIDATOR_PRIVATE_KEY", "0xcafe")
    monkeypatch.setenv("ADMIN_API_KEY", "topsecret")
    env = _sandbox_child_env()
    assert "RELAYER_PRIVATE_KEY" not in env
    assert "VALIDATOR_PRIVATE_KEY" not in env
    assert "ADMIN_API_KEY" not in env
    assert not any("deadbeef" in v or "topsecret" in v for v in env.values())
