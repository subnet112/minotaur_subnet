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


# FLOOR share of emission weight real miner champions collectively receive; the
# remainder burns to the subnet owner. 0.05 means a 95% burn at the floor — a
# conservative ramp so a freshly-adopted (or bad) champion earns only a bounded
# share until proven. The aggregate miner share scales ABOVE this floor with
# trailing-24h order volume (see ``champion_miner_weight_fraction``).
#
# These are HARDCODED CONSTANTS, deliberately NOT env knobs: the weight split is
# consensus-relevant and must be IDENTICAL across every validator, or different
# operators (esp. third parties) would emit divergent weight vectors. Baking them
# into the image makes a change propagate uniformly via redeploy/Watchtower;
# changing them means a code edit + release, by design.
CHAMPION_MINER_WEIGHT_FLOOR = 0.05
# Backwards-compatible alias: the floor IS the default miner fraction (volume 0).
CHAMPION_MINER_WEIGHT_FRACTION = CHAMPION_MINER_WEIGHT_FLOOR

# Number of orders processed within the trailing 24h at which miners receive the
# FULL emission (fraction 1.0). Between 0 and this the miner share ramps linearly
# from the floor to 1.0. Like the floor, consensus-relevant and baked in.
ORDERS_FOR_FULL_EMISSION = 1000


def champion_miner_weight_fraction(orders_24h: int | float) -> float:
    """Aggregate miner emission share given trailing-24h order volume.

    Linear ramp: ``CHAMPION_MINER_WEIGHT_FLOOR`` (0.05) at 0 orders rising to
    ``1.0`` (100% to miners, nothing burned) at ``ORDERS_FOR_FULL_EMISSION``
    (1000) orders, then clamped at 1.0 beyond that. The floor is the minimum so a
    quiet subnet still routes the conservative 5% to its champion. Negative or
    non-numeric inputs degrade to the floor.
    """
    try:
        orders = max(0.0, float(orders_24h))
    except (TypeError, ValueError):
        return CHAMPION_MINER_WEIGHT_FLOOR
    progress = (
        min(1.0, orders / ORDERS_FOR_FULL_EMISSION)
        if ORDERS_FOR_FULL_EMISSION > 0
        else 1.0
    )
    fraction = CHAMPION_MINER_WEIGHT_FLOOR + (1.0 - CHAMPION_MINER_WEIGHT_FLOOR) * progress
    return min(1.0, max(CHAMPION_MINER_WEIGHT_FLOOR, fraction))


def apply_champion_burn_ramp(
    miner_weights: dict[str, float],
    *,
    owner_hotkey: str | None = None,
    miner_fraction: float | None = None,
) -> dict[str, float]:
    """Scale a (normalized) miner weight distribution so the miners collectively
    receive ``miner_fraction`` of emission and the remainder burns to the subnet
    owner — the conservative champion ramp.

    ``miner_fraction`` defaults to ``CHAMPION_MINER_WEIGHT_FLOOR`` (0.05, a 95%
    burn). Callers scaling emission by order volume pass the value from
    ``champion_miner_weight_fraction``; passing 1.0 routes the full emission to
    miners with nothing burned. The function is idempotent in the fraction: an
    already-ramped ``{champion: 0.05, owner: 0.95}`` mapping re-ramps cleanly to
    a new split, so it can be safely re-applied at emission time.

    The relative split *among* miners is preserved; only their aggregate share is
    capped. Used by BOTH champion-weight paths (the daemon burn-fallback builder
    below and the leader's ranked path in ``epoch/manager.py``) so the burn
    applies however the distribution was built.

    Returns ``miner_weights`` unchanged only when there are no miners or the owner
    can't be resolved (nothing to burn to). The owner is *dropped* from the miner
    set before ramping — it is the burn target, never a competing miner — and the
    remaining miners are re-normalized so they still collectively receive exactly
    ``miner_fraction`` (a stray owner submission must not skip the burn for the
    whole set).
    """
    if not miner_weights:
        return miner_weights
    fraction = CHAMPION_MINER_WEIGHT_FLOOR if miner_fraction is None else miner_fraction
    fraction = min(1.0, max(0.0, float(fraction)))
    owner = (owner_hotkey or "").strip() or get_subnet_owner_hotkey()
    if not owner:
        # Fail LOUD: with no resolvable owner we cannot burn, so the miners would
        # receive the FULL emission instead of the intended share — the exact thing
        # this ramp exists to prevent. Surface it so an operator sets the owner.
        logger.warning(
            "Champion burn ramp SKIPPED: no subnet owner resolved (set "
            "SUBNET_OWNER_HOTKEY / OWNER_HOTKEY on the leader, or wire a chain "
            "fallback) — %d miner(s) would receive the FULL emission share "
            "instead of %.0f%%.",
            len(miner_weights), fraction * 100,
        )
        return miner_weights
    # Drop the owner from the miner set BEFORE ramping. The owner is the burn
    # target, not a candidate miner; leaving it in made the old `owner in
    # miner_weights` guard skip the ramp entirely, so the whole set kept summing
    # to 1.0 (no burn) — one stray owner submission disabling the burn for
    # everyone. Re-normalize the remaining miners to preserve their relative split.
    miners = {hk: w for hk, w in miner_weights.items() if hk != owner}
    if not miners:
        # The owner was the only "miner" (champion IS the owner) -> 100% to owner.
        return {owner: 1.0}
    total = sum(miners.values())
    if total <= 0:
        return {owner: 1.0}
    ramped = {hk: (w / total) * fraction for hk, w in miners.items()}
    ramped[owner] = 1.0 - fraction
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
