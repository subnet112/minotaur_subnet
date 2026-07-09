"""Canonical multi-chain registry — one source of truth for per-chain config.

Historically the supported EVM chains (Ethereum 1, Base 8453, Bittensor-EVM 964,
local Anvil 31337) were hardcoded in dozens of places: five copy-pasted
``if chain_id == 8453/1/964`` RPC ladders, three separate ``chain_id -> (name,
rpc_env)`` tables, the proxy slug map, per-chain fee/gas tables, and hand-unrolled
``rpc_urls``/``sim_rpc_urls``/``upstream_rpc_urls`` builders in both the api and the
validator. Adding a chain meant editing all of them.

This module collapses that into ONE table (:data:`CHAINS`). Adding a chain is now:
add a :class:`ChainSpec` row here and supply its env vars. Nothing else hardcodes
the chain set.

Two kinds of field, kept deliberately separate:

* **Consensus-static** (``slug``, ``is_anchor``, ``lookback_epochs``, ``is_poa``):
  fleet-uniform CODE constants. ``is_anchor``/``lookback_epochs`` fold into
  ``benchmark_pack_hash`` and MUST be identical fleet-wide — exactly the discipline
  ``ROUND_ANCHORED_PIN`` / ``EPOCH_SECONDS`` follow. They are values here, never env.

* **Env-resolved** (the ``*_rpc_envs`` ladders, registry-address templates): each is
  an ORDERED tuple of environment-variable NAMES. The resolvers below return the
  first name that is set. The names + order reproduce the pre-refactor behavior
  EXACTLY (see ``tests/unit/test_chain_registry.py``, which pins registry resolution
  against the original inlined logic), so this is a pure consolidation: no env
  rename, no behavior change, no pack-hash change.

Import-cheap on purpose (stdlib only): consensus and harness code imports it freely
without pulling in web3. ``blockchain/chains.get_web3`` and ``relayer/chain_config``
become thin readers of this table.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ChainSpec:
    """Everything the runtime needs to know about one EVM chain.

    ``*_rpc_envs`` are ordered tuples of env-var NAMES; the resolvers return the
    first that is set (empty string when none is). This mirrors the original
    per-site ``os.environ.get(A) or os.environ.get(B)`` ladders verbatim.
    """

    chain_id: int
    name: str
    # Proxy route slug + read-proxy ``UPSTREAMS`` key + metrics key. NOT unique
    # (local Anvil 31337 shares "eth" with mainnet, as the legacy CHAIN_NAMES did).
    slug: str
    # Canonical single live-RPC env (blockchain.chains.get_web3 / relayer chain_config).
    rpc_env: str
    is_poa: bool = False
    # Block-explorer base URL (blockchain.chains.get_explorer_url / get_tx_url).
    explorer: str = ""

    # ── env-resolved RPC ladders (ordered env-var names) ──────────────────────
    # Strict live-chain reads (registry / validator-set / score caches): the
    # operator's *_UPSTREAM_RPC_URL, then plain RPC. Empty tuple -> "" (unknown).
    live_rpc_envs: tuple[str, ...] = ()
    # Gas-price read (fee_policy) — like live but chain-specific extras.
    gas_rpc_envs: tuple[str, ...] = ()
    # Consensus reads (protocol_config): UPSTREAM-only, with public/local fallback.
    consensus_rpc_envs: tuple[str, ...] = ()
    consensus_public_fallback: str | None = None
    # Simulator fork target (the api/validator sim_rpc_urls builder).
    sim_rpc_envs: tuple[str, ...] = ()
    # Fork SOURCE the anvil forks from (upstream_rpc_urls builder + pin derivation).
    upstream_rpc_env: str | None = None
    # Benchmark-sandbox anvil (orchestrator build_rpc_url_map).
    benchmark_rpc_envs: tuple[str, ...] = ()
    # Plain-RPC ladder for health / contract-presence probes (contract_checks,
    # metrics) — just needs *a* working RPC, upstream not required.
    check_rpc_envs: tuple[str, ...] = ()
    # Upstream the read-proxy sidecar dials for this chain (read_proxy_manager
    # UPSTREAMS): plain RPC preferred, then the fork upstream. Falls back to
    # ``consensus_public_fallback`` (e.g. the BT-EVM lite endpoint) when set.
    proxy_upstream_envs: tuple[str, ...] = ()
    # Live-RPC used to build the faucet + boot-solver rpc_urls (startup/validator).
    boot_rpc_env: str | None = None

    # ── consensus-static (CODE constants; fold into pack hash) ────────────────
    is_anchor: bool = False
    lookback_epochs: int = 1

    # ── per-chain economics (fallbacks; overridable via PROTOCOL_FEE_FLOOR_WEI_<id>) ──
    fee_floor_wei: int = 0
    fallback_gas_price_wei: int = 1_000_000_000

    # ── membership flags ──────────────────────────────────────────────────────
    # Local testnet / the MultiChainSimulator default fallback chain.
    is_local: bool = False
    # Always present in the solver's chain_ids set even without a configured RPC
    # (legacy: the base set was seeded [1, 31337] unconditionally). Ethereum +
    # local Anvil; other chains join only when their boot RPC env is set.
    always_in_chain_set: bool = False
    # Wired into the running stack today (vs dormant registry-only entries like
    # Arbitrum/Optimism that have metadata but no sim/benchmark plumbing).
    wired: bool = True


# ─────────────────────────────────────────────────────────────────────────────
#  THE TABLE — add a chain by adding a row.
# ─────────────────────────────────────────────────────────────────────────────

_SPECS: tuple[ChainSpec, ...] = (
    ChainSpec(
        chain_id=1,
        name="Ethereum",
        slug="eth",
        rpc_env="ETHEREUM_RPC_URL",
        is_poa=False,
        explorer="https://etherscan.io",
        live_rpc_envs=("ETH_UPSTREAM_RPC_URL", "ETH_RPC_URL", "ANVIL_RPC_URL"),
        gas_rpc_envs=(
            "ETH_UPSTREAM_RPC_URL", "ETHEREUM_RPC_URL", "ETH_RPC_URL", "ANVIL_RPC_URL",
        ),
        consensus_rpc_envs=("ETH_UPSTREAM_RPC_URL",),
        consensus_public_fallback=None,
        sim_rpc_envs=("ETH_SIM_RPC_URL", "ANVIL_RPC_URL"),
        upstream_rpc_env="ETH_UPSTREAM_RPC_URL",
        benchmark_rpc_envs=("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
        check_rpc_envs=("ETH_RPC_URL", "ANVIL_RPC_URL"),
        proxy_upstream_envs=("ETH_RPC_URL", "ETH_UPSTREAM_RPC_URL"),
        boot_rpc_env="ANVIL_RPC_URL",
        is_anchor=False,
        lookback_epochs=3,   # ~12s blocks: 3 epochs clears 12-conf by round open
        fee_floor_wei=33_000_000_000_000,
        fallback_gas_price_wei=25_000_000_000,
        always_in_chain_set=True,
    ),
    ChainSpec(
        chain_id=8453,
        name="Base",
        slug="base",
        rpc_env="BASE_RPC_URL",
        is_poa=True,
        explorer="https://basescan.org",
        live_rpc_envs=("BASE_UPSTREAM_RPC_URL", "BASE_RPC_URL"),
        gas_rpc_envs=("BASE_UPSTREAM_RPC_URL", "BASE_RPC_URL"),
        consensus_rpc_envs=("BASE_UPSTREAM_RPC_URL",),
        consensus_public_fallback=None,
        sim_rpc_envs=("BASE_SIM_RPC_URL", "BASE_RPC_URL"),
        upstream_rpc_env="BASE_UPSTREAM_RPC_URL",
        benchmark_rpc_envs=(
            "BENCHMARK_ANVIL_RPC_BASE", "BASE_SIM_RPC_URL", "BASE_RPC_URL",
        ),
        check_rpc_envs=("BASE_RPC_URL",),
        proxy_upstream_envs=("BASE_RPC_URL", "BASE_UPSTREAM_RPC_URL"),
        boot_rpc_env="BASE_RPC_URL",
        is_anchor=True,          # the primary benchmark anchor (was ROUND_ANCHOR_CHAINS)
        lookback_epochs=1,
        fee_floor_wei=33_000_000_000_000,
        fallback_gas_price_wei=20_000_000,
    ),
    ChainSpec(
        chain_id=964,
        name="Bittensor EVM",
        slug="btevm",
        rpc_env="BITTENSOR_EVM_RPC_URL",
        is_poa=False,
        explorer="https://evm.taostats.io",
        live_rpc_envs=(
            "BITTENSOR_EVM_UPSTREAM_RPC_URL",
            "BITTENSOR_EVM_RPC_URL",
            "BITTENSOR_EVM_FORK_RPC_URL",
        ),
        gas_rpc_envs=("BITTENSOR_EVM_UPSTREAM_RPC_URL", "BITTENSOR_EVM_RPC_URL"),
        consensus_rpc_envs=(
            "BITTENSOR_EVM_UPSTREAM_RPC_URL", "BITTENSOR_EVM_RPC_URL",
        ),
        consensus_public_fallback="https://lite.chain.opentensor.ai",
        sim_rpc_envs=("BITTENSOR_EVM_RPC_URL",),
        upstream_rpc_env="BITTENSOR_EVM_UPSTREAM_RPC_URL",
        benchmark_rpc_envs=(
            "BENCHMARK_ANVIL_RPC_BTEVM",
            "BITTENSOR_EVM_SIM_RPC_URL",
            "BITTENSOR_EVM_RPC_URL",
        ),
        check_rpc_envs=("BITTENSOR_EVM_RPC_URL", "BITTENSOR_EVM_FORK_RPC_URL"),
        proxy_upstream_envs=("BITTENSOR_EVM_RPC_URL", "BITTENSOR_EVM_UPSTREAM_RPC_URL"),
        boot_rpc_env="BITTENSOR_EVM_RPC_URL",
        is_anchor=False,
        lookback_epochs=1,
        fee_floor_wei=330_000_000_000_000,
        fallback_gas_price_wei=25_000_000_000,
    ),
    ChainSpec(
        chain_id=31337,
        name="Anvil",
        slug="eth",   # legacy CHAIN_NAMES routed 31337 through the "eth" proxy slug
        rpc_env="ANVIL_RPC_URL",
        is_poa=False,
        explorer="http://localhost:8545",
        live_rpc_envs=(),   # the live-read caches only knew 8453/1/964 -> "" here
        gas_rpc_envs=(),
        consensus_rpc_envs=(),
        consensus_public_fallback=None,
        sim_rpc_envs=("ANVIL_RPC_URL",),
        upstream_rpc_env=None,   # local testnet forks from nothing
        benchmark_rpc_envs=("BENCHMARK_ANVIL_RPC_ETH", "ANVIL_RPC_URL"),
        boot_rpc_env="ANVIL_RPC_URL",
        is_anchor=False,
        lookback_epochs=1,
        fee_floor_wei=0,
        fallback_gas_price_wei=1_000_000_000,
        is_local=True,
        always_in_chain_set=True,
    ),
    # ── Dormant: metadata present (explorer/name/gas), not wired into sim/benchmark. ──
    ChainSpec(
        chain_id=42161, name="Arbitrum", slug="arbitrum", rpc_env="ARBITRUM_RPC_URL",
        is_poa=True, explorer="https://arbiscan.io", fallback_gas_price_wei=10_000_000, wired=False,
    ),
    ChainSpec(
        chain_id=10, name="Optimism", slug="optimism", rpc_env="OPTIMISM_RPC_URL",
        is_poa=True, explorer="https://optimistic.etherscan.io", fallback_gas_price_wei=10_000_000, wired=False,
    ),
)

CHAINS: dict[int, ChainSpec] = {s.chain_id: s for s in _SPECS}

# Generic (chain-agnostic) address fallbacks, kept for parity with the pre-refactor
# resolvers that accepted a non-suffixed env alongside the per-chain form.
VALIDATOR_REGISTRY_FALLBACK_ENV = "VALIDATOR_REGISTRY_ADDRESS"
CHAMPION_REGISTRY_FALLBACK_ENV = "CHAMPION_CONSENSUS_CONTRACT_ADDRESS"


# ─────────────────────────────────────────────────────────────────────────────
#  Lookups
# ─────────────────────────────────────────────────────────────────────────────

def spec(chain_id: int) -> ChainSpec | None:
    """The :class:`ChainSpec` for *chain_id*, or ``None`` if unregistered."""
    return CHAINS.get(int(chain_id))


def is_supported(chain_id: int) -> bool:
    return int(chain_id) in CHAINS


def all_chain_ids() -> tuple[int, ...]:
    """Every registered chain id (including dormant/unwired)."""
    return tuple(CHAINS)


def wired_chain_ids() -> tuple[int, ...]:
    """Chains actually plumbed into the running stack (sim/benchmark/proxy)."""
    return tuple(cid for cid, s in CHAINS.items() if s.wired)


def chain_name(chain_id: int) -> str:
    s = spec(chain_id)
    return s.name if s is not None else "Unknown"


def slug(chain_id: int) -> str | None:
    """Proxy route / UPSTREAMS / metrics slug (the legacy ``CHAIN_NAMES`` value)."""
    s = spec(chain_id)
    return s.slug if s is not None else None


def default_chain_id() -> int:
    """The local/testnet fallback chain (MultiChainSimulator ``default_chain_id``)."""
    for cid, s in CHAINS.items():
        if s.is_local:
            return cid
    return 31337


def anchor_chains() -> tuple[int, ...]:
    """The benchmark anchor chain-set (was ``ROUND_ANCHOR_CHAINS``).

    Consensus-static: folds into ``benchmark_pack_hash``, so it is derived from
    the ``is_anchor`` CODE constants here — never env — and must be fleet-uniform.
    """
    return tuple(cid for cid, s in CHAINS.items() if s.is_anchor)


def lookback_epochs(chain_id: int, default: int = 1) -> int:
    """Per-chain round-anchor confirmation-margin lookback (consensus-static)."""
    s = spec(chain_id)
    return s.lookback_epochs if s is not None else default


# ─────────────────────────────────────────────────────────────────────────────
#  Env resolution
# ─────────────────────────────────────────────────────────────────────────────

def _first_env(names: tuple[str, ...]) -> str:
    """First set (non-empty, stripped) env var among *names*, else ``""``."""
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def live_rpc(chain_id: int) -> str:
    """LIVE-chain RPC for registry / validator-set / score reads.

    Replaces the byte-identical ``_chain_rpc_env`` in ``app_registry_cache`` /
    ``validator_registry_cache`` / ``score_threshold_cache``. Returns ``""`` when
    no live RPC is configured (callers fail open with a WARN, as before).
    """
    s = spec(chain_id)
    return _first_env(s.live_rpc_envs) if s is not None else ""


def gas_rpc(chain_id: int) -> str:
    """LIVE RPC for the gas-price read (``fee_policy._live_gas_rpc_url``)."""
    s = spec(chain_id)
    return _first_env(s.gas_rpc_envs) if s is not None else ""


def consensus_rpc(chain_id: int) -> str:
    """RPC for consensus chain reads (``protocol_config.consensus_chain_rpc_url``).

    UPSTREAM-preferred, then a chain-specific public fallback, then the local
    Anvil URL — the local node IS the live chain for testnet/dev.
    """
    s = spec(chain_id)
    if s is not None:
        u = _first_env(s.consensus_rpc_envs)
        if u:
            return u
        if s.consensus_public_fallback:
            return s.consensus_public_fallback
    return (
        os.environ.get("ANVIL_RPC_URL", "").strip()
        or os.environ.get("BASE_RPC_URL", "").strip()
        or "http://localhost:8545"
    )


def sim_rpc(chain_id: int) -> str:
    """Simulator fork-target RPC for *chain_id* (the ``sim_rpc_urls`` value)."""
    s = spec(chain_id)
    return _first_env(s.sim_rpc_envs) if s is not None else ""


def upstream_rpc(chain_id: int) -> str:
    """The fork SOURCE upstream RPC for *chain_id* (``upstream_rpc_urls`` value)."""
    s = spec(chain_id)
    if s is None or s.upstream_rpc_env is None:
        return ""
    return os.environ.get(s.upstream_rpc_env, "").strip()


def benchmark_rpc(chain_id: int) -> str:
    """Benchmark-sandbox anvil RPC (``orchestrator.build_rpc_url_map``)."""
    s = spec(chain_id)
    return _first_env(s.benchmark_rpc_envs) if s is not None else ""


def check_rpc(chain_id: int) -> str:
    """Plain-RPC for health / contract-presence probes (contract_checks, metrics)."""
    s = spec(chain_id)
    return _first_env(s.check_rpc_envs) if s is not None else ""


def proxy_upstream(chain_id: int) -> str:
    """Upstream the read-proxy sidecar dials for *chain_id* (its ``UPSTREAMS`` value).

    Plain RPC preferred, then the fork upstream, then the chain's public fallback
    (e.g. the BT-EVM lite endpoint). ``""`` when nothing is configured.
    """
    s = spec(chain_id)
    if s is None:
        return ""
    url = _first_env(s.proxy_upstream_envs)
    if url:
        return url
    return s.consensus_public_fallback or ""


def boot_rpc(chain_id: int) -> str:
    """Live RPC used to seed the faucet + boot-solver ``rpc_urls`` maps."""
    s = spec(chain_id)
    if s is None or s.boot_rpc_env is None:
        return ""
    return os.environ.get(s.boot_rpc_env, "").strip()


# ── per-chain contract-address env resolution (templated ``PREFIX_<chain_id>``) ──

def validator_registry_env(chain_id: int) -> str:
    return f"VALIDATOR_REGISTRY_{int(chain_id)}"


def app_registry_env(chain_id: int) -> str:
    return f"APP_REGISTRY_{int(chain_id)}"


def champion_registry_env(chain_id: int) -> str:
    return f"CHAMPION_REGISTRY_{int(chain_id)}"


def relayer_wallet_env(chain_id: int) -> str:
    return f"RELAYER_WALLET_{int(chain_id)}"


def validator_registry_address(chain_id: int, *, allow_generic: bool = False) -> str:
    """Resolve the ValidatorRegistry address for *chain_id* from env.

    ``allow_generic`` also accepts the chain-agnostic ``VALIDATOR_REGISTRY_ADDRESS``
    (checked FIRST, matching the call sites that preferred the generic override).
    """
    if allow_generic:
        generic = os.environ.get(VALIDATOR_REGISTRY_FALLBACK_ENV, "").strip()
        if generic:
            return generic
    return os.environ.get(validator_registry_env(chain_id), "").strip()


def app_registry_address(chain_id: int) -> str:
    return os.environ.get(app_registry_env(chain_id), "").strip()


def champion_registry_address(chain_id: int, *, allow_generic: bool = False) -> str:
    if allow_generic:
        generic = os.environ.get(CHAMPION_REGISTRY_FALLBACK_ENV, "").strip()
        if generic:
            return generic
    return os.environ.get(champion_registry_env(chain_id), "").strip()


# ── economics ────────────────────────────────────────────────────────────────

def fee_floor_wei(chain_id: int, default: int = 0) -> int:
    s = spec(chain_id)
    return s.fee_floor_wei if s is not None else default


def fallback_gas_price_wei(chain_id: int, default: int = 1_000_000_000) -> int:
    s = spec(chain_id)
    return s.fallback_gas_price_wei if s is not None else default
