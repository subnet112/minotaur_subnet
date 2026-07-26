"""Registry-driven builders for the runtime per-chain RPC maps.

The api (``api/startup.py``) and the validator (``validator/main.py``) used to
hand-unroll the same ``rpc_urls`` / ``sim_rpc_urls`` / ``upstream_rpc_urls`` /
``chain_ids`` dicts per literal chain, and had drifted apart (the validator
omitted Bittensor-EVM and never read ``ETH_SIM_RPC_URL``). These builders derive
the maps from the chain registry so BOTH entry points share one definition and a
new chain needs only a registry row.

Each builder returns ``{chain_id: url}`` including a chain only when its RPC is
configured (env set) — an absent chain has no RPC, exactly as before.
"""
from __future__ import annotations

from minotaur_subnet.chains import registry


def boot_rpc_urls() -> dict[int, str]:
    """Live RPCs for the boot solver + faucet (``registry.boot_rpc`` per chain)."""
    out: dict[int, str] = {}
    for cid in registry.wired_chain_ids():
        url = registry.boot_rpc(cid)
        if url:
            out[cid] = url
    return out


def runtime_chain_ids() -> list[int]:
    """The solver's chain-id set: the always-present base chains (Ethereum + local
    Anvil) plus any other wired chain whose boot RPC is configured.

    Preserves the legacy ordering (base set first, then conditionally-added
    chains in registry order).
    """
    ids = [cid for cid in registry.all_chain_ids()
           if (s := registry.spec(cid)) is not None and s.always_in_chain_set]
    for cid in registry.wired_chain_ids():
        s = registry.spec(cid)
        if s is not None and not s.always_in_chain_set and registry.boot_rpc(cid):
            ids.append(cid)
    return ids


def sim_rpc_urls() -> dict[int, str]:
    """Simulator fork-target RPCs (``registry.sim_rpc`` per chain)."""
    out: dict[int, str] = {}
    for cid in registry.wired_chain_ids():
        url = registry.sim_rpc(cid)
        if url:
            out[cid] = url
    return out


def quote_sim_rpc_urls() -> dict[int, str]:
    """Dedicated /quote simulator fork-targets (``registry.quote_sim_rpc`` per
    chain). Returns ``{}`` when no chain has a quote fork configured — this empty
    map is the opt-in gate: no dedicated quote simulator is built and quotes use
    the shared order simulator (behaviour byte-unchanged)."""
    out: dict[int, str] = {}
    for cid in registry.wired_chain_ids():
        url = registry.quote_sim_rpc(cid)
        if url:
            out[cid] = url
    return out


def upstream_rpc_urls() -> dict[int, str]:
    """Fork-source upstream RPCs (``registry.upstream_rpc`` per chain)."""
    out: dict[int, str] = {}
    for cid in registry.wired_chain_ids():
        url = registry.upstream_rpc(cid)
        if url:
            out[cid] = url
    return out
