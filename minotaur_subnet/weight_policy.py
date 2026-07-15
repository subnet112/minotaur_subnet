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


# FIXED share of emission weight the real miner champion receives; the remainder
# routes to the subnet owner. This is a FLAT split — there is NO order-volume
# scaling: once a champion is adopted it earns this constant share of the miner
# emission pool regardless of order throughput.
#
# The number is written HERE ONCE and nowhere else. Every docstring, doc and test
# refers to it symbolically. This is not style: the 0.05 -> 0.10 move (3cf7895)
# wrote the new number into the code but left four prose sites claiming the old
# one, and they stayed wrong for weeks. Do not re-introduce a literal.
#
# "Burn" is the historical name and it is a MISNOMER worth knowing: the remainder
# is not destroyed. It routes to the subnet owner's registered UID as ordinary
# incentive (see validator/weights_emitter.py, which resolves the owner share via
# hotkey_to_uid), so this constant does not set a supply-sink rate — it sets how
# the miner emission pool is DIVIDED between the champion and the owner.
#
# This is a HARDCODED CONSTANT, deliberately NOT an env knob: the weight split is
# consensus-relevant and must be IDENTICAL across every validator, or different
# operators (esp. third parties) would emit divergent weight vectors. Baking it
# into the image makes a change propagate uniformly via redeploy/Watchtower;
# changing it means a code edit + release, by design.
#
# CHANGING IT IS A FLEET EVENT, and a quiet one — this value is NOT folded into
# benchmark_pack_hash and is NOT carried on the champion certificate, so a fleet
# running mixed values still forms champion consensus, still certifies, and still
# reaches quorum, all while committing divergent weight vectors. Nothing in this
# repo detects that. The only symptom is Yuma clipping the minority side's vtrust
# (and roughly halving its dividends), visible on the issue-59 dashboard's Trust
# column. There is no env kill switch by design, so the only rollback is another
# code change + release. Merge and promote back-to-back; never let a change to
# this value sit on develop, where it reaches the leader alone within the hour.
CHAMPION_MINER_WEIGHT_FRACTION = 0.75


def apply_champion_burn_ramp(
    miner_weights: dict[str, float],
    *,
    owner_hotkey: str | None = None,
    miner_fraction: float | None = None,
) -> dict[str, float]:
    """Scale a (normalized) miner weight distribution so the miners collectively
    receive ``miner_fraction`` of emission and the remainder routes to the subnet
    owner — the conservative champion ramp.

    ``miner_fraction`` defaults to ``CHAMPION_MINER_WEIGHT_FRACTION``; passing 1.0
    routes the full emission to miners with nothing held back. The function is
    idempotent in the fraction: an already-split ``{champion: f, owner: 1-f}``
    mapping re-applies cleanly to the same split, so it can be safely re-applied at
    emission time. (It is in fact a projection, not merely idempotent — no branch
    here compares the fraction against any threshold, so this holds for every f.)

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
    fraction = CHAMPION_MINER_WEIGHT_FRACTION if miner_fraction is None else miner_fraction
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
    """Bootstrap / champion weight emission (single-champion / daemon burn-fallback path).

    - No real miner champion (``None`` / genesis): 100% to the subnet owner.
    - Real miner champion: the fixed split via ``apply_champion_burn_ramp`` — the
      champion gets ``CHAMPION_MINER_WEIGHT_FRACTION`` and the owner gets the
      remainder (falling back to 100%-to-champion only if the owner can't be
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
