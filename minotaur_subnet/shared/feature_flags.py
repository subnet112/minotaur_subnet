"""Process-wide feature flags read from environment.

Keep this module dependency-free so it can be imported from any layer
without creating cycles.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def cross_chain_enabled() -> bool:
    """Multi-leg / bridge execution is dev-track, not in beta.

    When this is off:
      - The /orders API rejects orders whose input_chain_id differs from
        output_chain_id (or any other multi-chain marker).
      - The blockloop refuses to process a plan whose metadata flags it
        as cross-chain, even if a solver tried to return one.

    Flip on only on dedicated staging/dev environments until the
    cross-chain path clears its Phase 5 exit criteria.
    """
    return _env_bool("CROSS_CHAIN_ENABLED", default=False)


CROSS_CHAIN_DISABLED_MESSAGE = (
    "Cross-chain / multi-leg orders are not enabled in this environment. "
    "Beta scope is single-chain Base (chain 8453). "
    "Set CROSS_CHAIN_ENABLED=1 on a staging target to exercise the dev-track path."
)
