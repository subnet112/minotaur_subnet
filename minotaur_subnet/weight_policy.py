"""Shared subnet weight emission policy helpers."""

from __future__ import annotations

import os

GENESIS_HOTKEY = "__genesis__"


def get_subnet_owner_hotkey() -> str:
    """Return the configured subnet-owner hotkey used for burn routing."""
    return (
        os.environ.get("SUBNET_OWNER_HOTKEY", "").strip()
        or os.environ.get("OWNER_HOTKEY", "").strip()
    )


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
