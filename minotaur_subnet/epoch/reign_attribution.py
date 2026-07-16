"""Throne-time accrual for time-weighted emission (Phase 0, observe-only).

Today emission is a POINT SAMPLE: once per tempo the validator commits weight to
whoever holds the champion throne at the single ~4-minute commit instant, and
every other miner who reigned during that tempo earns nothing (the "short-reign
lottery" — a champion dethroned before the commit window gets zero). This module
computes the alternative: split the miner emission fraction across EVERY hotkey
that held the throne during a tempo, proportional to how many solver-round epochs
each held it.

ACCRUAL, NOT RECONSTRUCTION
---------------------------
We do NOT reconstruct a window of past rounds at settlement time. Instead we
SAMPLE the current champion frequently and accumulate epochs per hotkey:

    each tick:   credit the CURRENT champion for the epochs elapsed since the
                 last sample
    per tempo:   emit proportional to the accumulator, then reset

This needs only "who is champion right now" — never a reconstructed reign
ledger, a history backfill, or a per-round hotkey record.

WHY SAMPLING IS ROBUST HERE (the load-bearing property)
-------------------------------------------------------
"Who is champion now" is the fastest-converging piece of fleet state: it is
pushed instantly and pull-reconciled within ~5 min (champion_reconcile). And a
champion cannot activate until ~22 epochs (~22 min) after the previous round
closed (SOLVER_ROUND_ACTIVATION_DELAY_EPOCHS), so every reign lasts ~22+ min —
comfortably longer than the ~5-min convergence lag. A follower therefore always
observes each champion as "current" for most of its reign and can NEVER entirely
miss one. Divergence between validators is bounded to the convergence lag per
transition (usually ~0, since the push is instant), and it is bounded and
self-correcting — unlike reconstructing history, where a single missed
broadcast leaves a permanent, unhealable full-reign gap.

DETERMINISM CONTRACT
--------------------
The emitted weight vector is consensus-relevant: two validators emitting
different vectors for the same tempo get the minority clipped by Yuma with no
on-chain detector (the silent-divergence class of
``weight_policy.CHAMPION_MINER_WEIGHT_FRACTION``). Accrual is done in INTEGER
epoch counts, sampling the SAME champion source that drives emission, so nodes
that agree on the current champion accumulate identically. Downtime gaps are
fail-closed to the owner (never blindly credited to whoever is champion on
recovery).

This is still OBSERVE-ONLY (default OFF), and it MUST stay that way until three
Phase-1 gaps are closed — the first is the promotion blocker:
  1. Settle + reset on a SHARED chain-tempo integer, not per-node wall-clock.
     As wired, both the tempo bucket (now_epoch // TEMPO_EPOCHS) and the settle
     instant follow each node's own clock + emit-loop phase, so two validators
     reading the accumulator seconds apart across a boundary can settle a full
     tempo vs a freshly-reset (empty) one — an UNBOUNDED divergence that fires
     even with a stable, unanimously-agreed champion. Drive both off the chain
     weight-tempo index and settle only at that boundary.
  2. Persist the accumulator across restarts (an in-memory reset mid-tempo drops
     the pre-restart reign share).
  3. Bound the convergence-lag term (a follower slow to adopt a new champion
     right-endpoint-credits the handover epochs to the outgoing one).
All three are harmless while the vector is only LOGGED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minotaur_subnet.weight_policy import (
    apply_champion_burn_ramp,
    is_real_miner_hotkey,
)

# Bittensor tempo in solver-round epochs. The on-chain weight tempo is 360
# blocks (~72 min at 12s blocks); solver-round epochs are 60s buckets
# (clock.EPOCH_SECONDS), so one tempo spans ~72 epochs. Used to bucket the
# accrual window; the real (Phase 1) path resets on the chain tempo index.
TEMPO_EPOCHS = 72

# A hotkey must accrue at least this many epochs in a tempo to earn any share;
# sub-floor accruals fold into the owner residual. 0 disables the floor. A floor
# blunts dust and the cheapest dilution-griefing (grab the throne for a single
# epoch purely to skim a rival's share); tune once the mechanism emits for real.
DEFAULT_MIN_REIGN_EPOCHS = 0

# The coordinator samples every ~5s while an epoch is 60s, so consecutive samples
# are normally 0–1 epochs apart. A span wider than this means sampling STOPPED
# (a coordinator stall — e.g. screening-build CPU starvation, documented to
# silence the leader loop for tens of seconds — or a paused/restarted loop); the
# excess folds into the owner residual instead of blindly crediting the whole
# gap to whoever is champion when sampling resumes. Must be well below
# TEMPO_EPOCHS or it is a no-op (a span that large already triggers the tempo
# reset). Small, so a real stall fails closed to the owner; >1 to tolerate tick
# jitter across an epoch boundary.
MAX_SAMPLE_GAP_EPOCHS = 2


@dataclass(frozen=True)
class ThroneTimeAttribution:
    """Integer throne-time attribution for one tempo.

    Every accrued epoch is credited to at most one miner hotkey; everything else
    (no-champion spans and downtime gaps, plus any below-floor hotkeys) is
    ``unattributed`` and routes to the owner. ``window_epochs`` is the total
    accrued this tempo.
    """

    per_hotkey_epochs: dict[str, int] = field(default_factory=dict)
    unattributed_epochs: int = 0

    @property
    def window_epochs(self) -> int:
        return sum(self.per_hotkey_epochs.values()) + self.unattributed_epochs


class ThroneTimeAccumulator:
    """Accrues throne-time by sampling the current champion.

    Call :meth:`sample` frequently (each coordinator tick) with the current
    champion and epoch; call :meth:`settle` at emit to read the accrued
    distribution. The accumulator auto-resets when the tempo index advances, so a
    fresh tempo starts empty. In-memory only in Phase 0 (a restart loses the
    partial tempo — an observation gap, not a correctness bug); persistence is
    Phase 1.
    """

    def __init__(self) -> None:
        self._tempo_index: int | None = None
        self._cursor_epoch: int | None = None
        self._epochs: dict[str, int] = {}
        self._unattributed: int = 0

    def sample(
        self,
        *,
        now_epoch: int,
        tempo_index: int,
        champion_hotkey: str | None,
        max_gap_epochs: int | None = None,
    ) -> None:
        """Credit the span ``[cursor, now_epoch)`` to ``champion_hotkey``.

        Resets when ``tempo_index`` changes (anchoring the cursor at ``now_epoch``
        — we never retro-credit epochs from before we began observing this
        tempo). A span wider than ``max_gap_epochs`` means sampling stopped
        (downtime / a stalled loop); only the cap is credited to the current
        champion and the excess folds into the owner residual, so a long gap is
        never blindly attributed to whoever happens to be champion on recovery.
        """
        now_epoch = int(now_epoch)
        tempo_index = int(tempo_index)

        if self._tempo_index != tempo_index:
            self._tempo_index = tempo_index
            self._epochs = {}
            self._unattributed = 0
            self._cursor_epoch = now_epoch
            return
        if self._cursor_epoch is None:
            self._cursor_epoch = now_epoch
            return

        span = now_epoch - self._cursor_epoch
        if span <= 0:
            # Same epoch as the last sample (the common case, since ticks are
            # finer than epochs) or a backwards clock — nothing to credit.
            return

        credited = span if max_gap_epochs is None else min(span, max(0, int(max_gap_epochs)))
        gap = span - credited
        if is_real_miner_hotkey(champion_hotkey):
            assert champion_hotkey is not None
            hk = champion_hotkey.strip()
            self._epochs[hk] = self._epochs.get(hk, 0) + credited
        else:
            self._unattributed += credited
        self._unattributed += gap
        self._cursor_epoch = now_epoch

    def settle(self, *, min_reign_epochs: int = DEFAULT_MIN_REIGN_EPOCHS) -> ThroneTimeAttribution:
        """Snapshot the current tempo's accrual (does not reset — the tempo
        rollover in :meth:`sample` handles that). Applies the minimum-reign
        floor: sub-floor hotkeys are dropped and their epochs fold into the owner
        residual."""
        floor = max(0, int(min_reign_epochs))
        if floor > 0:
            kept = {hk: e for hk, e in self._epochs.items() if e >= floor}
        else:
            kept = dict(self._epochs)
        dropped = sum(self._epochs.values()) - sum(kept.values())
        return ThroneTimeAttribution(
            per_hotkey_epochs=kept,
            unattributed_epochs=self._unattributed + dropped,
        )

    def debug_state(self) -> dict[str, object]:
        """Compact state for /health or logs."""
        return {
            "tempo_index": self._tempo_index,
            "cursor_epoch": self._cursor_epoch,
            "hotkeys": len(self._epochs),
            "accrued_epochs": sum(self._epochs.values()),
            "unattributed_epochs": self._unattributed,
        }


def build_time_weighted_mapping(
    attribution: ThroneTimeAttribution,
    *,
    owner_hotkey: str | None,
    miner_fraction: float,
) -> dict[str, float]:
    """Convert an integer throne-time attribution into a hotkey→weight mapping.

    The miners collectively receive ``miner_fraction`` scaled by the fraction of
    the tempo that had a (surviving) champion — so un-throned / downtime epochs
    correctly shrink the miner pool toward the owner rather than being silently
    redistributed:

        effective_miner_fraction = miner_fraction * attributed / window_epochs

    The per-miner split is proportional to epochs held, delegated to
    ``apply_champion_burn_ramp`` (the same tested ramp the single-champion path
    uses). Miners are fed in hotkey-sorted order so the float normalization is
    order-stable across nodes. Degenerate cases (empty window, no miner, zero
    attributed share) route 100% to the owner.
    """
    attributed = sum(attribution.per_hotkey_epochs.values())
    if attribution.window_epochs <= 0 or attributed <= 0:
        owner = (owner_hotkey or "").strip()
        return {owner: 1.0} if owner else {}

    effective_fraction = miner_fraction * attributed / attribution.window_epochs
    miner_weights = {
        hk: float(epochs)
        for hk, epochs in sorted(attribution.per_hotkey_epochs.items())
    }
    return apply_champion_burn_ramp(
        miner_weights,
        owner_hotkey=owner_hotkey,
        miner_fraction=effective_fraction,
    )
