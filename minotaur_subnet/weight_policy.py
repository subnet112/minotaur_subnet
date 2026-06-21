"""Shared subnet weight emission policy helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_HOTKEY = "__genesis__"
# Epoch the genesis/bootstrap submission is keyed under in the submission store.
GENESIS_EPOCH = 0


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


def resolve_subnet_owner_hotkey(subtensor: Any = None, netuid: int | None = None) -> str:
    """Resolve the subnet owner hotkey. The CHAIN is authoritative (public on-chain
    data, identical for every validator); the env (SUBNET_OWNER_HOTKEY / OWNER_HOTKEY)
    is only a fallback for environments where the chain isn't queryable (e.g. a local
    testnet without the storage set)."""
    if subtensor is not None and netuid is not None:
        try:
            chain_owner = lookup_subnet_owner_from_chain(subtensor, netuid)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Subnet owner chain lookup failed (%s); falling back to env", exc)
            chain_owner = ""
        if chain_owner:
            return chain_owner
    return get_subnet_owner_hotkey()


def is_real_miner_hotkey(hotkey: str | None) -> bool:
    """Return whether a hotkey belongs to a real miner-backed champion."""
    value = (hotkey or "").strip()
    return bool(value) and value != GENESIS_HOTKEY


# Share of emission weight a real miner champion receives; the remainder burns
# to the subnet owner. 0.05 means a 95% burn — a conservative ramp so a freshly-
# adopted (or bad) champion earns only a bounded share until proven.
#
# This is a HARDCODED CONSTANT, deliberately NOT an env knob: the weight split is
# consensus-relevant and must be IDENTICAL across every validator, or different
# operators (esp. third parties) would emit divergent weight vectors. Baking it
# into the image makes a change propagate uniformly via redeploy/Watchtower;
# changing it means a code edit + release, by design.
CHAMPION_MINER_WEIGHT_FRACTION = 0.05


def apply_champion_burn_ramp(
    miner_weights: dict[str, float],
    *,
    owner_hotkey: str | None = None,
) -> dict[str, float]:
    """Scale a (normalized) miner weight distribution so the miners collectively
    receive ``CHAMPION_MINER_WEIGHT_FRACTION`` (0.05) of emission and the
    remainder (0.95) burns to the subnet owner — the conservative champion ramp.

    The relative split *among* miners is preserved; only their aggregate share is
    capped. Used by BOTH champion-weight paths (the daemon burn-fallback builder
    below and the leader's ranked path in ``epoch/manager.py``) so the 95% burn
    applies however the distribution was built.

    Returns ``miner_weights`` unchanged only when there are no miners or the owner
    can't be resolved (nothing to burn to). The owner is *dropped* from the miner
    set before ramping — it is the burn target, never a competing miner — and the
    remaining miners are re-normalized so they still collectively receive exactly
    ``CHAMPION_MINER_WEIGHT_FRACTION`` (a stray owner submission must not skip the
    burn for the whole set).
    """
    if not miner_weights:
        return miner_weights
    owner = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()
    if not owner:
        # Fail LOUD: with no resolvable owner we cannot burn, so the miners would
        # receive the FULL emission instead of the intended 0.05 — the exact thing
        # this ramp exists to prevent. Surface it so an operator sets the owner.
        logger.warning(
            "Champion burn ramp SKIPPED: no subnet owner resolved (set "
            "SUBNET_OWNER_HOTKEY / OWNER_HOTKEY on the leader, or wire a chain "
            "fallback) — %d miner(s) would receive the FULL emission share "
            "instead of %.0f%%.",
            len(miner_weights), CHAMPION_MINER_WEIGHT_FRACTION * 100,
        )
        return miner_weights
    # Drop the owner from the miner set BEFORE ramping. The owner is the burn
    # target, not a candidate miner; leaving it in made the old `owner in
    # miner_weights` guard skip the ramp entirely, so the whole set kept summing
    # to 1.0 (no 0.95 burn) — one stray owner submission disabling the burn for
    # everyone. Re-normalize the remaining miners to preserve their relative split.
    miners = {hk: w for hk, w in miner_weights.items() if hk != owner}
    if not miners:
        # The owner was the only "miner" (champion IS the owner) -> 100% to owner.
        return {owner: 1.0}
    total = sum(miners.values())
    if total <= 0:
        return {owner: 1.0}
    ramped = {
        hk: (w / total) * CHAMPION_MINER_WEIGHT_FRACTION for hk, w in miners.items()
    }
    ramped[owner] = 1.0 - CHAMPION_MINER_WEIGHT_FRACTION
    return ramped


def build_bootstrap_or_champion_weights(
    champion_hotkey: str | None,
    *,
    owner_hotkey: str | None = None,
) -> dict[str, float]:
    """Bootstrap / ramp weight emission (single-champion / daemon burn-fallback path).

    - No real miner champion (``None`` / genesis): 100% burn to the subnet owner.
    - Real miner champion: the conservative ramp via ``apply_champion_burn_ramp``
      — the champion gets ``CHAMPION_MINER_WEIGHT_FRACTION`` (0.05) and 0.95 burns
      to the owner (falling back to 100%-to-champion only if the owner can't be
      resolved or the champion IS the owner).
    """
    owner = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()

    if is_real_miner_hotkey(champion_hotkey):
        assert champion_hotkey is not None
        return apply_champion_burn_ramp(
            {champion_hotkey.strip(): 1.0}, owner_hotkey=owner_hotkey,
        )

    if owner:
        return {owner: 1.0}
    return {}
