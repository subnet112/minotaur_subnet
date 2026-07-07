"""Chain discovery service functions."""

from __future__ import annotations

import os
from typing import Any, Iterable

from . import _state as _state_mod


def _app_registry_address(c: Any) -> str:
    """AppRegistry address for a chain — chain config first, then the
    ``APP_REGISTRY_{chain_id}`` env fallback (the SAME resolution order the
    deployer and app_admin use, so /v1/chains can never disagree with what
    the platform actually enforces)."""
    return (
        (getattr(c, "app_registry_address", "") or "")
        or os.environ.get(f"APP_REGISTRY_{c.chain_id}", "")
    ).strip()


def build_public_chain_info(chains: Iterable[Any]) -> list[dict[str, Any]]:
    """Project relayer ChainDeployment entries into the public /v1/chains shape.

    Only chains with a non-empty ``validator_registry_address`` are exposed —
    that's our marker for "we've stood up the consensus stack here." Chains
    without it (e.g. the simulation-only Anvil fork the api uses internally
    via ANVIL_RPC_URL) are filtered out so they don't leak into the public
    chain list and look like a user-routable destination.

    Field notes for consumers:
      - ``registry_address``      → the ValidatorRegistry on that chain
        (kept under its historical name for backward compatibility).
      - ``app_registry_address``  → the AppRegistry on that chain; "" when no
        on-chain app gate is configured there. Additive (frontend renders
        registry panels from it — treat "" as "not configured").
    """
    return [
        {
            "chain_id": c.chain_id,
            "name": c.name,
            "rpc_available": bool(c.rpc_url),
            "registry_address": c.validator_registry_address,
            "app_registry_address": _app_registry_address(c),
        }
        for c in chains
        if c.validator_registry_address
    ]


def list_chains() -> dict[str, Any]:
    """Return all chains the platform can deploy to and simulate on."""
    return {"chains": _state_mod._chain_info, "total": len(_state_mod._chain_info)}
