"""Round-anchored fork-pin derivation — canonical, independently verifiable.

Every validator derives the SAME per-chain benchmark fork block from the round's
subtensor-epoch anchor *timestamp*, so champion-consensus determinism stops
depending on an out-of-band ``BENCHMARK_EPOCH_BLOCK`` env value that operators
must set identically by hand. The pin is a property of the chain (the last block
at/before the anchor time), so two honest nodes compute the identical integer
without trusting a leader-asserted number.

See ``docs/round-anchored-fork-pin-spec.md`` (Option b).

**P0 scope — PURE and INERT.** This module reads no env, opens no RPC
connection, and is not yet wired into the benchmark/consensus path. Callers
inject block accessors (a live web3 adapter in production, a synthetic map in
tests), which keeps the derivation trivially testable and import-light.
"""

from __future__ import annotations

import os
from typing import Callable

# Accessor signatures injected by the caller.
HeadFn = Callable[[int], int]               # chain_id -> current head block number
BlockTimestampFn = Callable[[int, int], int]  # (chain_id, block) -> unix timestamp
LoFn = Callable[[int], int]                 # chain_id -> earliest searchable block

# Explicit disable values for the fleet-uniform gate. Anything else (including
# unset, empty, or garbage) is treated as ENABLED so a typo can never silently
# fall a single validator back to live-head and split it off the fleet.
_GATE_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def round_anchored_pin_enabled() -> bool:
    """Fleet-uniform gate for round-anchored fork pinning. **DEFAULT ON.**

    Round-anchored pinning is consensus-relevant config: it folds into
    ``benchmark_pack_hash`` and the on-chain veto, so it must be uniform CODE
    propagated via ``:stable``/redeploy, NOT a per-validator env that 3rd-party
    validators would never set (the same reason ``CHAMPION_MINER_WEIGHT_FRACTION``
    and ``EPOCH_SECONDS`` are hardcoded). Hence the default is ON in code.

    Emergency override only: set ``ROUND_ANCHORED_PIN`` to one of
    ``{0, false, no, off}`` (case-insensitive) to disable fleet-wide via compose
    without a code revert. Unset / any other value = enabled. This is the single
    place the default lives, so every read site stays in lock-step.
    """
    raw = os.environ.get("ROUND_ANCHORED_PIN")
    if raw is None:
        return True
    return raw.strip().lower() not in _GATE_OFF_VALUES


def epoch_anchor_ts(epoch: int, epoch_seconds: int) -> int:
    """Deterministic anchor timestamp for a (time-based) solver-round epoch.

    Mirrors ``SolverRoundEpochClock`` time mode, where epoch ``E`` spans
    ``[E*epoch_seconds, (E+1)*epoch_seconds)`` — so the epoch boundary timestamp
    is ``E * epoch_seconds``. Every validator computes it identically from the
    shared ``epoch_seconds`` config (already fleet-consistent, since round ids
    derive from it), with no chain read. This is the anchor fed to
    :func:`derive_fork_pins`.
    """
    if epoch_seconds <= 0:
        raise ValueError("epoch_seconds must be > 0")
    return int(epoch) * int(epoch_seconds)


class ForkPinUnavailable(Exception):
    """The pin cannot yet be derived deterministically for a chain.

    Raised when the chain has not confirmed far enough past the anchor (so the
    anchor is not yet *bracketed* by confirmed blocks), or when no block at/before
    the anchor exists. Callers must defer the round / fall back — never guess. A
    guess is exactly the silent divergence this module removes.
    """


def find_pin_block(
    anchor_ts: int,
    *,
    head: int,
    block_timestamp: Callable[[int], int],
    confirmations: int = 0,
    lo: int = 0,
) -> int:
    """Highest block ``b`` with ``block_timestamp(b) <= anchor_ts``, confirmed.

    Deterministic: identical ``(anchor_ts, chain history, confirmations)`` yields
    the identical ``b`` on every node, because ``b`` is a property of the chain
    (the last block before the anchor time), not of when or where it is computed.

    Determinism guard (the bracketing rule): only blocks at or below
    ``head - confirmations`` are eligible, and a confirmed block strictly *after*
    the anchor must exist (``block_timestamp(confirmed_tip) > anchor_ts``). If the
    confirmed tip is still at/before the anchor, the anchor is effectively in the
    future relative to confirmed state and two nodes at different wall-clocks
    would disagree — so we raise :class:`ForkPinUnavailable` (defer) rather than
    return a moving target.

    Assumes block timestamps are non-decreasing in block number (true on
    Ethereum/Base and required for the binary search).
    """
    if confirmations < 0:
        raise ValueError("confirmations must be >= 0")

    confirmed_tip = head - confirmations
    if confirmed_tip < lo:
        raise ForkPinUnavailable(
            f"chain too short: confirmed tip {confirmed_tip} < lo {lo}"
        )

    # Bracketing: a confirmed block strictly after the anchor must exist, else the
    # pin would track the still-moving tip.
    if block_timestamp(confirmed_tip) <= anchor_ts:
        raise ForkPinUnavailable(
            f"anchor {anchor_ts} not yet confirmed-bracketed: tip {confirmed_tip} "
            f"ts={block_timestamp(confirmed_tip)} <= anchor"
        )

    # Nothing qualifies if even the earliest searchable block is after the anchor.
    if block_timestamp(lo) > anchor_ts:
        raise ForkPinUnavailable(
            f"anchor {anchor_ts} precedes lo block {lo} ts={block_timestamp(lo)}"
        )

    # Binary search the highest b in [lo, confirmed_tip] with ts(b) <= anchor_ts.
    best = lo
    low, high = lo, confirmed_tip
    while low <= high:
        mid = (low + high) // 2
        if block_timestamp(mid) <= anchor_ts:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def derive_fork_pins(
    anchor_ts: int,
    chains: list[int],
    *,
    head_of: HeadFn,
    block_timestamp_of: BlockTimestampFn,
    confirmations: int = 0,
    lo_of: LoFn | None = None,
) -> dict[int, int]:
    """Per-chain canonical fork pins for a round, keyed by ``chain_id``.

    Raises :class:`ForkPinUnavailable` if *any* chain cannot be pinned
    deterministically — a partially-pinned round would let validators diverge, so
    the whole round defers rather than pin some chains and not others.
    """
    pins: dict[int, int] = {}
    for chain_id in chains:
        lo = lo_of(chain_id) if lo_of is not None else 0
        try:
            pins[chain_id] = find_pin_block(
                anchor_ts,
                head=head_of(chain_id),
                block_timestamp=lambda b, _c=chain_id: block_timestamp_of(_c, b),
                confirmations=confirmations,
                lo=lo,
            )
        except ForkPinUnavailable as exc:
            raise ForkPinUnavailable(f"chain {chain_id}: {exc}") from exc
    return pins


def serialize_fork_pins(pins: dict[int, int]) -> str:
    """Deterministic string of the pins, for folding into ``benchmark_pack_hash``.

    Sorted by ``chain_id`` so every node produces byte-identical output regardless
    of dict insertion order, e.g. ``"8453:46904887|964:5012345"``.
    """
    return "|".join(f"{chain_id}:{pins[chain_id]}" for chain_id in sorted(pins))
