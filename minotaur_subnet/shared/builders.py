"""Shared builder helpers for common types.

Centralises the construction of IntentState and Interaction objects that
is otherwise duplicated across api/routes/apps.py, api/services.py,
blockloop/loop.py, and validator/main.py.
"""

from __future__ import annotations

from typing import Any

from minotaur_subnet.shared.types import IntentState, Interaction


def build_intent_state(
    contract_address: str = "",
    chain_id: int = 1,
    nonce: int = 0,
    owner: str = "",
    params: dict[str, Any] | None = None,
    intent_function: str | None = "execute",
) -> IntentState:
    """Build an IntentState with explicit runtime params and control metadata.

    Args:
        contract_address: Deployed contract address.
        chain_id: Target chain ID.
        nonce: Replay protection nonce.
        owner: Owner / submitter address; defaults to zero address.
        params: App/runtime parameters stored in ``raw_params``.
        intent_function: If not None, injected into ``control["_intent_function"]``.
            Pass ``None`` to skip injection (e.g. for dry-run scoring where
            the intent function is not relevant).

    Returns:
        A fully-constructed IntentState.
    """
    raw_params = dict(params or {})
    control: dict[str, Any] = {}
    if intent_function is not None:
        control["_intent_function"] = intent_function
    return IntentState(
        contract_address=contract_address,
        chain_id=chain_id,
        nonce=nonce,
        owner=owner or ("0x" + "00" * 20),
        raw_params=raw_params,
        control=control,
    )


def parse_interactions(
    interactions_raw: list[dict[str, Any]],
    default_chain_id: int = 1,
) -> list[Interaction]:
    """Parse interaction dicts handling both camelCase and snake_case keys.

    Accepts dicts with either ``call_data`` or ``callData`` (prefers
    ``call_data``), and ``chain_id`` or ``chainId`` (prefers ``chain_id``).

    Args:
        interactions_raw: List of interaction dictionaries.
        default_chain_id: Fallback chain ID when the dict has neither key.

    Returns:
        List of typed Interaction objects.
    """
    return [
        Interaction(
            target=ix.get("target", "0x" + "00" * 20),
            value=ix.get("value", "0"),
            call_data=ix.get("call_data", ix.get("callData", "0x")),
            chain_id=ix.get("chain_id", ix.get("chainId", default_chain_id)),
        )
        for ix in interactions_raw
    ]
