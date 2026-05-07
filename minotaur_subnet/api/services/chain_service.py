"""Chain discovery service functions."""

from __future__ import annotations

from typing import Any

from . import _state as _state_mod


def list_chains() -> dict[str, Any]:
    """Return all chains the platform can deploy to and simulate on."""
    return {"chains": _state_mod._chain_info, "total": len(_state_mod._chain_info)}
