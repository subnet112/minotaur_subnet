"""Cost-aware gating for the miner agent loop.

Keeps Claude token spend and CPU bounded by skipping the expensive parts
of a cycle when they're unlikely to pay off. The gate runs at the top of
every cycle and returns ``(should_run, reason)``. When ``should_run`` is
False the loop logs the reason (for CloudWatch ``MinerCycleSkipped{Reason}``)
and sleeps until the next cycle.

Four rules, each returning a distinct reason code:

  1. CHAMPION_UNCHALLENGED — this miner is the current champion, and no new
     submission has scored higher than ours since our last submission. No
     point burning Claude tokens to improve code that's already winning.

  2. TOP_RANKED_UNCHALLENGED — not champion, but ranked 1st by recent score
     and no fresh challenger. We're at the top of the waiting-room; pointless
     to iterate until someone else catches up.

  3. PLATEAU — last K cycles produced no score gain above MIN_DELTA.
     Enter a PLATEAU_COOLDOWN_SECONDS quiet window before trying again.

  4. TOKEN_BUDGET — daily Claude token budget exhausted (resets UTC midnight).

State persists to ``<state_dir>/cost_gate_state.json`` so daemon restarts
don't forget the plateau / budget counters.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_STATE_FILE = "cost_gate_state.json"

# Env-var defaults. Set per-miner via compose.
#
# The plateau/stagnation deltas are deltas on the COUNTS-BASED PROGRESS RATIO
# the loop now feeds in — ``better/compared`` (fraction of orders the miner's
# latest submission beats the champion on), a [0,1] number. They were 0..1
# JS-score deltas before the relative-cutover; because the progress ratio is
# also [0,1] the same thresholds carry over unchanged (a 0.5% / 1% move in the
# fraction of orders won). See loop._current_progress_signal.
DEFAULT_PLATEAU_K = 5
DEFAULT_PLATEAU_MIN_DELTA = 0.005          # +0.5% of orders-won required to reset
DEFAULT_PLATEAU_COOLDOWN_SECONDS = 4 * 3600  # 4h
DEFAULT_TOKEN_BUDGET_PER_DAY = 100_000

# Circuit-breaker: if our best-score-ever doesn't move for this many
# seconds, force a long cooldown regardless of plateau-K state. Backstop
# in case the K-cycle plateau detector misses (e.g. score oscillating
# just under min_delta keeps resetting cycles_without_improvement). 3
# days is long enough that genuine multi-day exploration cycles aren't
# disrupted, short enough that a wedged miner stops bleeding budget.
DEFAULT_NO_PROGRESS_BREAKER_SECONDS = 3 * 24 * 3600  # 3 days
DEFAULT_NO_PROGRESS_COOLDOWN_SECONDS = 24 * 3600     # 24h cooldown when triggered

# Stagnation detector: if the relative-progress ratio (better/compared) has
# stayed within +/- this delta across the last K benchmarked submissions, the
# miner is churning — Claude is iterating but not winning more orders. Trigger
# a long cooldown so we don't bleed budget on a stuck hypothesis. (Pre-cutover
# this tracked the pre-sim 0..1 score mean; that's now a saturated validity
# sentinel, so the loop feeds the counts ratio instead — same [0,1] delta.)
DEFAULT_STAGNATION_WINDOW = 5
DEFAULT_STAGNATION_DELTA = 0.01
DEFAULT_STAGNATION_COOLDOWN_SECONDS = 12 * 3600  # 12h


@dataclass
class CostGateState:
    cycles_without_improvement: int = 0
    last_observed_score: float = 0.0
    plateau_entered_at: float | None = None   # unix seconds
    token_budget_date: str = ""                # YYYY-MM-DD UTC
    token_budget_used: int = 0
    last_submission_at: float = 0.0            # unix seconds
    last_submission_score: float = 0.0
    # Circuit-breaker tracking
    best_score_ever: float = 0.0
    best_score_ever_at: float = 0.0           # unix seconds; 0 = never seen
    no_progress_breaker_at: float | None = None  # set when breaker triggers
    # Stagnation tracking: rolling window of pre-sim mean scores from
    # actual Claude runs (transient-failure runs excluded by the loop
    # before record_pre_sim is called). When the window's range falls
    # below stagnation_delta for K runs, trip a long cooldown.
    recent_pre_sim_scores: list[float] = field(default_factory=list)
    stagnation_cooldown_at: float | None = None

    @classmethod
    def load(cls, path: Path) -> "CostGateState":
        try:
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


@dataclass(frozen=True)
class GateDecision:
    should_run: bool
    reason: str = ""
    detail: str = ""


class CostGate:
    """Decides each cycle whether the expensive LLM path should run.

    The agent loop calls ``should_run_this_cycle()`` right after fetching
    the current champion + per-app scores, then calls ``record_cycle()``
    after finishing (or deciding to skip) with the observed best score
    across this miner's own strategies.
    """

    def __init__(
        self,
        *,
        miner_id: str,
        state_dir: str | Path,
        plateau_k: int = DEFAULT_PLATEAU_K,
        plateau_min_delta: float = DEFAULT_PLATEAU_MIN_DELTA,
        plateau_cooldown_seconds: float = DEFAULT_PLATEAU_COOLDOWN_SECONDS,
        token_budget_per_day: int = DEFAULT_TOKEN_BUDGET_PER_DAY,
        no_progress_breaker_seconds: float = DEFAULT_NO_PROGRESS_BREAKER_SECONDS,
        no_progress_cooldown_seconds: float = DEFAULT_NO_PROGRESS_COOLDOWN_SECONDS,
        stagnation_window: int = DEFAULT_STAGNATION_WINDOW,
        stagnation_delta: float = DEFAULT_STAGNATION_DELTA,
        stagnation_cooldown_seconds: float = DEFAULT_STAGNATION_COOLDOWN_SECONDS,
    ) -> None:
        self.miner_id = miner_id
        self.state_path = Path(state_dir) / DEFAULT_STATE_FILE
        self.plateau_k = plateau_k
        self.plateau_min_delta = plateau_min_delta
        self.plateau_cooldown_seconds = plateau_cooldown_seconds
        self.token_budget_per_day = token_budget_per_day
        self.no_progress_breaker_seconds = no_progress_breaker_seconds
        self.no_progress_cooldown_seconds = no_progress_cooldown_seconds
        self.stagnation_window = stagnation_window
        self.stagnation_delta = stagnation_delta
        self.stagnation_cooldown_seconds = stagnation_cooldown_seconds
        self.state = CostGateState.load(self.state_path)

    # ── Token accounting ────────────────────────────────────────────────

    def _today(self) -> str:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    def _roll_token_day_if_needed(self) -> None:
        today = self._today()
        if self.state.token_budget_date != today:
            self.state.token_budget_date = today
            self.state.token_budget_used = 0

    def record_pre_sim_score(self, mean_score: float) -> None:
        """Append a relative-progress ratio to the stagnation window.

        Post relative-cutover this receives the ``better/compared`` ratio
        (fraction of orders beating the champion) once per benchmarked
        submission — NOT the old pre-sim 0..1 score mean, which is now a
        saturated validity sentinel. A flat ratio across K submissions ⇒ Claude
        is churning, not winning more orders. The arg name is kept for API
        stability. (Skip transient-failure cycles — they tell us nothing.)
        """
        self.state.recent_pre_sim_scores.append(float(mean_score))
        # Cap window
        if len(self.state.recent_pre_sim_scores) > self.stagnation_window:
            self.state.recent_pre_sim_scores = (
                self.state.recent_pre_sim_scores[-self.stagnation_window:]
            )
        self.state.save(self.state_path)

    def record_token_usage(self, tokens: int) -> None:
        """Charge ``tokens`` against today's budget. Persists to disk."""
        if tokens <= 0:
            return
        self._roll_token_day_if_needed()
        self.state.token_budget_used += int(tokens)
        self.state.save(self.state_path)

    # ── Plateau tracking ────────────────────────────────────────────────

    def record_cycle(self, *, best_score: float, submitted: bool) -> None:
        """Update plateau + submission state after a cycle finishes.

        ``best_score`` is this miner's progress signal this cycle — post-cutover
        the counts-based ``better/compared`` ratio (best across apps), a [0,1]
        number, NOT a JS quality score. Arg name kept for API stability.
        ``submitted`` is True if the cycle actually POSTed a submission.
        """
        delta = best_score - self.state.last_observed_score
        if delta >= self.plateau_min_delta:
            # Real improvement — reset plateau counter
            self.state.cycles_without_improvement = 0
            self.state.plateau_entered_at = None
        else:
            self.state.cycles_without_improvement += 1
            if (
                self.state.cycles_without_improvement >= self.plateau_k
                and self.state.plateau_entered_at is None
            ):
                self.state.plateau_entered_at = _now()
                logger.info(
                    "[cost-gate %s] PLATEAU entered at %s (K=%d without Δ>=%.3f)",
                    self.miner_id,
                    self.state.plateau_entered_at,
                    self.plateau_k,
                    self.plateau_min_delta,
                )
        self.state.last_observed_score = max(
            self.state.last_observed_score, best_score,
        )
        # Track absolute best-score-ever for the no-progress breaker.
        # Any improvement (however tiny) resets the breaker timer; the
        # breaker fires only when the best stays flat for N days, NOT
        # the same condition as plateau-K which uses min_delta.
        if best_score > self.state.best_score_ever:
            self.state.best_score_ever = best_score
            self.state.best_score_ever_at = _now()
            self.state.no_progress_breaker_at = None  # reset breaker
        elif self.state.best_score_ever_at == 0.0:
            # First cycle ever — anchor the timer to now even if score is 0
            self.state.best_score_ever_at = _now()
        if submitted:
            self.state.last_submission_at = _now()
            self.state.last_submission_score = best_score
        self.state.save(self.state_path)

    # ── The gate ────────────────────────────────────────────────────────

    def should_run_this_cycle(
        self,
        *,
        champion: dict[str, Any] | None,
        my_best_score: float,
        top_rival_score: float,
        new_submissions_since_ours: int,
    ) -> GateDecision:
        """Decide whether to run the LLM this cycle.

        Args:
          champion: dict from ``GET /v1/solver/champion`` or None.
          my_best_score: our progress signal — post-cutover the counts-based
                         ``better/compared`` ratio (fraction of orders beating
                         the champion), [0,1]. 0 when we have no relative signal.
          top_rival_score: best rival progress; inert post-cutover (no per-rival
                           relative ratio is served, so the loop passes 0) — the
                           "rival is ahead" case is detected via
                           new_submissions_since_ours instead.
          new_submissions_since_ours: count of submissions from any miner
                                      created after our latest submission.
        """
        self._roll_token_day_if_needed()

        # Rule 4: hard token-budget limit
        if self.state.token_budget_used >= self.token_budget_per_day:
            return GateDecision(
                should_run=False,
                reason="TOKEN_BUDGET",
                detail=(
                    f"{self.state.token_budget_used}/{self.token_budget_per_day} "
                    f"tokens used today ({self.state.token_budget_date})"
                ),
            )

        # Stagnation: K full Claude runs in a row produced pre-sim means
        # within stagnation_delta of each other. That's strong evidence
        # Claude is churning, not exploring. Trigger a long cooldown.
        scores = self.state.recent_pre_sim_scores
        if (
            self.stagnation_cooldown_seconds > 0
            and len(scores) >= self.stagnation_window
            and (max(scores) - min(scores)) < self.stagnation_delta
        ):
            if self.state.stagnation_cooldown_at is None:
                self.state.stagnation_cooldown_at = _now()
                self.state.save(self.state_path)
                logger.warning(
                    "[cost-gate %s] STAGNATION tripped: last %d pre-sim "
                    "means in [%.4f, %.4f] (delta < %.3f). %.0fh cooldown.",
                    self.miner_id, len(scores), min(scores), max(scores),
                    self.stagnation_delta,
                    self.stagnation_cooldown_seconds / 3600,
                )
            elapsed = _now() - self.state.stagnation_cooldown_at
            if elapsed < self.stagnation_cooldown_seconds:
                return GateDecision(
                    should_run=False,
                    reason="STAGNATION",
                    detail=(
                        f"last {len(scores)} runs flat in "
                        f"[{min(scores):.4f}, {max(scores):.4f}]; cooldown "
                        f"{(self.stagnation_cooldown_seconds - elapsed)/3600:.1f}h left"
                    ),
                )
            # Cooldown elapsed: clear and re-anchor by resetting the window
            # (so we don't immediately re-trip on the same flat scores).
            self.state.stagnation_cooldown_at = None
            self.state.recent_pre_sim_scores = []
            self.state.save(self.state_path)

        # Circuit-breaker: best-ever score hasn't moved for N days. Backstop
        # for the K-cycle plateau detector when scores oscillate just under
        # min_delta and keep resetting cycles_without_improvement.
        if self.state.best_score_ever_at > 0.0:
            stagnation = _now() - self.state.best_score_ever_at
            if stagnation >= self.no_progress_breaker_seconds:
                if self.state.no_progress_breaker_at is None:
                    self.state.no_progress_breaker_at = _now()
                    self.state.save(self.state_path)
                    logger.warning(
                        "[cost-gate %s] NO_PROGRESS breaker tripped: best=%.4f "
                        "unchanged for %.0fh — entering %.0fh cooldown",
                        self.miner_id,
                        self.state.best_score_ever,
                        stagnation / 3600,
                        self.no_progress_cooldown_seconds / 3600,
                    )
                cooldown_elapsed = _now() - self.state.no_progress_breaker_at
                if cooldown_elapsed < self.no_progress_cooldown_seconds:
                    return GateDecision(
                        should_run=False,
                        reason="NO_PROGRESS",
                        detail=(
                            f"best score {self.state.best_score_ever:.4f} "
                            f"unchanged for {stagnation/3600:.0f}h; cooldown "
                            f"{(self.no_progress_cooldown_seconds - cooldown_elapsed)/3600:.1f}h left"
                        ),
                    )
                # Cooldown elapsed — clear breaker and re-anchor the timer so
                # we don't immediately re-trip on the next cycle. Give the
                # next breaker window a fresh N days starting now.
                self.state.no_progress_breaker_at = None
                self.state.best_score_ever_at = _now()
                self.state.save(self.state_path)

        # Rule 3: plateau cooldown
        if self.state.plateau_entered_at is not None:
            elapsed = _now() - self.state.plateau_entered_at
            if elapsed < self.plateau_cooldown_seconds:
                return GateDecision(
                    should_run=False,
                    reason="PLATEAU",
                    detail=(
                        f"no improvement in last {self.plateau_k} cycles; "
                        f"cooldown {self.plateau_cooldown_seconds - elapsed:.0f}s left"
                    ),
                )
            # cooldown elapsed — exit plateau and try again
            self.state.plateau_entered_at = None
            self.state.cycles_without_improvement = 0
            self.state.save(self.state_path)

        # Rule 1: we are champion, no rival has caught up
        champion_miner = (champion or {}).get("miner_id") or (champion or {}).get("hotkey")
        if champion_miner and champion_miner == self.miner_id:
            if top_rival_score < my_best_score and new_submissions_since_ours == 0:
                return GateDecision(
                    should_run=False,
                    reason="CHAMPION_UNCHALLENGED",
                    detail=(
                        f"I am champion ({champion_miner}); top rival "
                        f"{top_rival_score:.4f} < me {my_best_score:.4f}"
                    ),
                )

        # Rule 2: not champion but top-ranked with no fresh challenger
        if (
            my_best_score >= top_rival_score
            and new_submissions_since_ours == 0
            and my_best_score > 0
        ):
            return GateDecision(
                should_run=False,
                reason="TOP_RANKED_UNCHALLENGED",
                detail=(
                    f"my score {my_best_score:.4f} >= top rival "
                    f"{top_rival_score:.4f}; no new submissions since ours"
                ),
            )

        return GateDecision(should_run=True)


def _now() -> float:
    """Wallclock in unix seconds; wrapped so tests can patch."""
    import time
    return time.time()
