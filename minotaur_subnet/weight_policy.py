"""Shared subnet weight emission policy helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_HOTKEY = "__genesis__"


def get_subnet_owner_hotkey() -> str:
    """Return the configured subnet-owner hotkey used for burn routing.

    Reads from env (SUBNET_OWNER_HOTKEY / OWNER_HOTKEY). Returns "" if neither
    is set. Callers should prefer ``lookup_subnet_owner_from_chain`` which
    falls through to the on-chain authoritative value.
    """
    return (
        os.environ.get("SUBNET_OWNER_HOTKEY", "").strip()
        or os.environ.get("OWNER_HOTKEY", "").strip()
    )


def lookup_subnet_owner_from_chain(subtensor: Any, netuid: int) -> str:
    """Query the subtensor ``SubnetOwnerHotkey`` storage and return its SS58
    string, or "" on any failure.

    Subnet owners are public on-chain data, so this is the authoritative
    source — no operator config required. The previous design required
    every operator to set ``SUBNET_OWNER_HOTKEY`` in their env or their
    daemon would silently emit empty weights every epoch. Third-party
    operators using our canonical compose didn't have that env passed
    through, and their daemons looked healthy from outside but never
    actually emitted on-chain.
    """
    try:
        result = subtensor.query_subtensor("SubnetOwnerHotkey", params=[netuid])
        value = getattr(result, "value", None)
        if value is None:
            value = str(result) if result is not None else ""
        value = str(value).strip()
        return value
    except Exception as exc:
        logger.warning(
            "Failed to look up subnet %d owner hotkey from chain: %s", netuid, exc
        )
        return ""


def is_real_miner_hotkey(hotkey: str | None) -> bool:
    """Return whether a hotkey belongs to a real miner-backed champion."""
    value = (hotkey or "").strip()
    return bool(value) and value != GENESIS_HOTKEY


def build_bootstrap_or_champion_weights(
    champion_hotkey: str | None,
    *,
    owner_hotkey: str | None = None,
) -> dict[str, float]:
    """Return 100% burn-to-owner before miner champion, else 100% to champion."""
    if is_real_miner_hotkey(champion_hotkey):
        assert champion_hotkey is not None
        return {champion_hotkey.strip(): 1.0}

    owner = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()
    if owner:
        return {owner: 1.0}
    return {}
