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

from minotaur_subnet.chains import registry

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


# Fleet-uniform benchmark-pin config. These fold into the round-anchored pin and
# thus ``benchmark_pack_hash``, so — like the gate above and CHAMPION_MINER_WEIGHT_
# FRACTION / EPOCH_SECONDS — they MUST be uniform CODE, never per-validator envs a
# 3rd party could override to split the fleet (a different value -> a different pin
# -> PACK_HASH_MISMATCH -> drops out of quorum). They are constants, not env reads.
#
# Base is the primary anchor chain. Additional chains join the anchor set ONLY
# via the BENCHMARK_ALL_DEPLOYMENT_CHAINS gate below (per-deployment chains),
# now that the fork pin is a per-chain map end-to-end (run_benchmark
# ``fork_blocks``, build_pin_blocks, per-scenario simulate) — the old scalar
# hazard (a Base block number applied to a non-Base fork) is structurally gone.
#
# The anchor set is the ``is_anchor`` flag in the chain registry (a CODE constant
# there — never env — so it stays fleet-uniform). Kept as a module constant here
# for the existing readers (startup, benchmark_worker, veto_wire).
ROUND_ANCHOR_CHAINS: tuple[int, ...] = registry.anchor_chains()
# Finality margin for the bracketing guard in find_pin_block (head - confirmations).
ROUND_ANCHOR_CONFIRMATIONS: int = 12
# Anchor the round's fork-pin this many epochs BEFORE the round's (close) epoch.
# The pin needs a confirmed block (head - ROUND_ANCHOR_CONFIRMATIONS, ~24s on Base)
# strictly AFTER the anchor; if the anchor sat at the close epoch (== the close
# wall-clock), that finality margin is never satisfiable at close and the pin defers
# forever (benchmark never runs → round aborts benchmarked=0). One epoch back
# (= epoch_seconds, 60s) buries the anchor comfortably past the confirmation depth by
# close, while staying a pure deterministic function of the epoch (no chain read).
#
# This DEFAULT is tuned for FAST chains (Base, ~2s blocks): 1 epoch (60s) back is
# ~30 blocks, comfortably past the 12-confirmation margin even at round OPEN.
ROUND_ANCHOR_LOOKBACK_EPOCHS: int = 1

# PER-CHAIN lookback override for SLOW chains. find_pin_block requires a confirmed
# block (head - ROUND_ANCHOR_CONFIRMATIONS) STRICTLY AFTER the anchor, and that
# must hold at round OPEN — when only `lookback` epochs of buffer exist yet. A
# chain with T-second blocks buries 12 confirmations ~12*T seconds deep, so the
# anchor must sit ≥ that far back or the round DEFERS FOREVER (froze the leader
# 2026-07-08 with Ethereum: 12*~12s = ~144s > the 60s one-epoch anchor — see
# issue #632). INVARIANT when adding a chain or changing epoch length:
#   lookback_epochs * EPOCH_SECONDS  >  ROUND_ANCHOR_CONFIRMATIONS * chain_block_secs
# At the production EPOCH_SECONDS=60, Ethereum (12*12s=144s) needs >=3 epochs
# (180s, ~36s jitter margin). A shorter epoch or a slower chain needs a larger
# value here or it re-freezes (fails loud / defers, never mis-scores). The DEFAULT
# (Base) anchor — and therefore the Base-only benchmark_pack_hash — is byte-identical
# to before; only chains whose registry ``lookback_epochs`` differs from the default
# change, and only when BENCHMARK_ALL_DEPLOYMENT_CHAINS is armed. The per-chain value
# lives in the chain registry as a CODE constant (never a per-validator env, same
# discipline as the rest of this module): Ethereum is 3 there (12s blocks: 3 epochs =
# 180s clears the 12-conf ~144s margin), every other chain is the default 1.


def round_anchor_lookback_epochs(chain_id: int) -> int:
    """Per-chain confirmation-margin lookback in epochs. Fast chains use the
    default 1; slow chains anchor deeper so ``find_pin_block`` can confirm-bracket
    them at round open. Sourced from the chain registry (fleet-uniform CODE
    constant) — every validator resolves it identically."""
    return registry.lookback_epochs(
        int(chain_id), ROUND_ANCHOR_LOOKBACK_EPOCHS,
    )


def benchmark_all_deployment_chains_enabled() -> bool:
    """Gate for benchmarking EVERY operational deployment chain of an app
    (not just the app's primary deployment). **DEFAULT OFF.**

    CONSENSUS-RELEVANT: turning this on adds each deployment chain's scenarios
    to the flat benchmark set (they join the relative adoption rule) and folds
    that chain's round-anchored pin into ``benchmark_pack_hash``. Like
    BENCHMARK_STATIC_QUOTE / ROUND_ANCHORED_PIN it must be flipped
    FLEET-UNIFORMLY: a split value surfaces as PACK_HASH_MISMATCH (fail-loud),
    never a silent mis-score. Ships OFF so it can soak on the lead first.

    Operational prerequisites on every node that arms it: the extra chains must
    be routed through the block-pin proxy (``SOLVER_READ_PROXY_CHAINS``), have a
    live upstream RPC for pin derivation (``*_UPSTREAM_RPC_URL``), and a
    dedicated sim fork (e.g. ``ETH_SIM_RPC_URL`` → the eth anvil) — otherwise
    the benchmark fails loud (RealSimulationUnavailable / ForkPinUnavailable)
    rather than scoring degraded.
    """
    return os.environ.get(
        "BENCHMARK_ALL_DEPLOYMENT_CHAINS", "",
    ).strip().lower() in ("1", "true", "yes", "on")


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


def round_anchor_ts(epoch: int, epoch_seconds: int) -> int:
    """Fork-pin anchor timestamp for a round — ``epoch_anchor_ts`` with the
    confirmation-margin lookback applied.

    Anchors ``ROUND_ANCHOR_LOOKBACK_EPOCHS`` epoch(s) before the round's (close)
    epoch, so the anchor is already buried past ``ROUND_ANCHOR_CONFIRMATIONS`` by the
    time the round closes — otherwise ``find_pin_block`` can never confirm-bracket an
    anchor that sits at the close wall-clock, and the round defers indefinitely. Still
    a pure function of ``(epoch, epoch_seconds)``: fleet-deterministic, no chain read.
    Every pin-derivation / pack-hash site MUST use this (not the raw
    ``epoch_anchor_ts``) so leader and followers compute the identical pin.

    Uses the DEFAULT (fast-chain) lookback. For the per-chain anchor a slow chain
    needs, use :func:`round_anchor_ts_for_chain`; this stays the Base/default
    value so existing scalar callers are byte-identical.
    """
    return epoch_anchor_ts(int(epoch) - ROUND_ANCHOR_LOOKBACK_EPOCHS, epoch_seconds)


def round_anchor_ts_for_chain(chain_id: int, epoch: int, epoch_seconds: int) -> int:
    """:func:`round_anchor_ts` with THIS chain's per-chain lookback applied (see
    :func:`round_anchor_lookback_epochs`). Equals ``round_anchor_ts`` exactly for
    any chain using the default lookback (e.g. Base), so the default pin is
    unchanged; a slow chain (e.g. Ethereum) anchors proportionally deeper so it
    confirm-brackets at round open. Pure/deterministic — no chain read."""
    return epoch_anchor_ts(
        int(epoch) - round_anchor_lookback_epochs(chain_id), epoch_seconds,
    )


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
    anchor_ts_of: Callable[[int], int] | None = None,
) -> dict[int, int]:
    """Per-chain canonical fork pins for a round, keyed by ``chain_id``.

    Raises :class:`ForkPinUnavailable` if *any* chain cannot be pinned
    deterministically — a partially-pinned round would let validators diverge, so
    the whole round defers rather than pin some chains and not others.

    ``anchor_ts_of`` (optional): a PER-CHAIN anchor timestamp
    (``chain_id -> anchor_ts``). When given it supersedes the scalar ``anchor_ts``
    so a slow chain can anchor deeper (see
    :func:`round_anchor_ts_for_chain` / issue #632); when None every chain uses
    the scalar ``anchor_ts`` — byte-identical to the pre-#632 behaviour, so a
    single-chain (Base-only) round is unchanged.
    """
    pins: dict[int, int] = {}
    for chain_id in chains:
        lo = lo_of(chain_id) if lo_of is not None else 0
        chain_anchor = anchor_ts_of(chain_id) if anchor_ts_of is not None else anchor_ts
        try:
            pins[chain_id] = find_pin_block(
                chain_anchor,
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
