"""Chain discovery service functions."""

from __future__ import annotations

from typing import Any, Iterable

from . import _state as _state_mod


def build_public_chain_info(chains: Iterable[Any]) -> list[dict[str, Any]]:
    """Project relayer ChainDeployment entries into the public /v1/chains shape.

    Only chains with a non-empty ``validator_registry_address`` are exposed —
    that's our marker for "we've stood up the consensus stack here." Chains
    without it (e.g. the simulation-only Anvil fork the api uses internally
    via ANVIL_RPC_URL) are filtered out so they don't leak into the public
    chain list and look like a user-routable destination.
    """
    return [
        {
            "chain_id": c.chain_id,
            "name": c.name,
            "rpc_available": bool(c.rpc_url),
            "registry_address": c.validator_registry_address,
        }
        for c in chains
        if c.validator_registry_address
    ]


def list_chains() -> dict[str, Any]:
    """Return all chains the platform can deploy to and simulate on."""
    return {"chains": _state_mod._chain_info, "total": len(_state_mod._chain_info)}
