/**
 * Rebalance App Intent - JavaScript Scoring Layer
 *
 * Runs on validators inside a sandboxed environment. Solvers never see this
 * code; they only receive the final numeric score (black-box optimisation).
 *
 * This is an AUTO-TRIGGERED intent: the subnet continuously monitors
 * portfolio drift and triggers rebalancing when any token's allocation
 * deviates beyond the configured threshold.
 *
 * Scoring breakdown (0 - 1.0):
 *   - Allocation accuracy:     up to 0.35  (post-execution closeness to targets)
 *   - Minimal trading:         up to 0.25  (only trade what's necessary)
 *   - Gas efficiency:          up to 0.20
 *   - Timing quality:          up to 0.20  (trigger near threshold, not too early/late)
 *
 * State fields (from state.raw_params or state.typed_context):
 *   target_allocations   - {"ETH": 0.6, "USDC": 0.4}  (weights sum to 1.0)
 *   current_allocations  - {"ETH": 0.7, "USDC": 0.3}  (actual current weights)
 *   threshold_pct        - 0.05  (5% drift triggers rebalance)
 *   total_value_usd      - current portfolio value in USD
 *
 * Configuration injected at deploy time via {{PARAM}} placeholders:
 *   {{SUPPORTED_CHAINS}} - JSON array of supported chain IDs
 */

// ─── helpers ────────────────────────────────────────────────────────────────

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function linearScore(value, worst, best) {
  if (best === worst) return value >= best ? 1.0 : 0.0;
  return clamp((value - worst) / (best - worst), 0.0, 1.0);
}

function runtimeParams(state) {
  return state.typed_context || state.raw_params || state.rawParams || {};
}

/**
 * Compute the maximum absolute drift between current and target allocations.
 */
function maxDrift(currentAllocations, targetAllocations) {
  let maxD = 0;
  for (const token of Object.keys(targetAllocations)) {
    const current = currentAllocations[token] || 0;
    const target = targetAllocations[token] || 0;
    maxD = Math.max(maxD, Math.abs(current - target));
  }
  // Also check tokens in current but not in target (should be 0)
  for (const token of Object.keys(currentAllocations)) {
    if (!(token in targetAllocations)) {
      maxD = Math.max(maxD, Math.abs(currentAllocations[token]));
    }
  }
  return maxD;
}

/**
 * Compute the sum of absolute drifts (total rebalance needed).
 */
function totalDrift(currentAllocations, targetAllocations) {
  let total = 0;
  const allTokens = new Set([
    ...Object.keys(targetAllocations),
    ...Object.keys(currentAllocations),
  ]);
  for (const token of allTokens) {
    const current = currentAllocations[token] || 0;
    const target = targetAllocations[token] || 0;
    total += Math.abs(current - target);
  }
  return total;
}

/**
 * Estimate post-execution allocations from simulation token transfers.
 * Returns an allocation map (token_symbol -> weight) if possible,
 * or null if we cannot determine post-execution state.
 */
function estimatePostAllocations(simulation, state) {
  if (
    !simulation.outputs ||
    !simulation.outputs.tokenTransfers ||
    simulation.outputs.tokenTransfers.length === 0
  ) {
    return null;
  }

  // Start with current allocations and apply transfer deltas
  const params = runtimeParams(state);
  const targetAllocations = params.target_allocations || {};
  const currentAllocations = params.current_allocations || {};
  const totalValueUsd = params.total_value_usd || 0;

  if (totalValueUsd <= 0) return null;

  // We cannot fully determine post-execution allocations from transfers
  // alone without price data, so we use the simulation's reported
  // final state if available.
  if (simulation.outputs.postAllocations) {
    return simulation.outputs.postAllocations;
  }

  return null;
}

// ─── validate ───────────────────────────────────────────────────────────────

async function validate(plan, state, context) {
  // 1. Structural checks
  if (!plan.interactions || plan.interactions.length === 0) {
    return { valid: false, reason: "No interactions in plan" };
  }
  if (plan.deadline < context.timestamp) {
    return { valid: false, reason: "Plan deadline has passed" };
  }

  // 2. Verify rebalance state is present
  const params = runtimeParams(state);
  const targetAllocations = params.target_allocations;
  const currentAllocations = params.current_allocations;
  const thresholdPct = params.threshold_pct;

  if (!targetAllocations || typeof targetAllocations !== "object") {
    return { valid: false, reason: "Missing target_allocations in state" };
  }
  if (!currentAllocations || typeof currentAllocations !== "object") {
    return { valid: false, reason: "Missing current_allocations in state" };
  }
  if (thresholdPct === undefined || thresholdPct === null) {
    return { valid: false, reason: "Missing threshold_pct in state" };
  }

  // 3. Verify drift actually exceeds threshold (plan should not be submitted otherwise)
  const drift = maxDrift(currentAllocations, targetAllocations);
  if (drift < thresholdPct * 0.5) {
    // Allow some tolerance (half threshold) to avoid rejecting plans
    // submitted right at the boundary
    return {
      valid: false,
      reason: `Max drift ${(drift * 100).toFixed(2)}% is well below threshold ${(thresholdPct * 100).toFixed(2)}%`,
    };
  }

  // 4. Simulate
  const simulation = await context.simulator.simulate(plan);
  if (simulation.reverts) {
    return { valid: false, reason: `Simulation reverted: ${simulation.error}` };
  }

  // 5. Warnings
  const warnings = [];
  if (plan.interactions.length > 10) {
    warnings.push(
      `High interaction count: ${plan.interactions.length} (may indicate unnecessary trades)`
    );
  }
  if (simulation.gasUsed > 1000000) {
    warnings.push(`High gas usage: ${simulation.gasUsed}`);
  }

  return { valid: true, warnings: warnings.length > 0 ? warnings : undefined };
}

// ─── score ───────────────────────────────────────────────────────────────────

async function score(plan, state, context) {
  const simulation = await context.simulator.simulate(plan);
  if (simulation.reverts) {
    return { score: 0, breakdown: { reverted: -1 } };
  }

  const params = runtimeParams(state);
  const targetAllocations = params.target_allocations || {};
  const currentAllocations = params.current_allocations || {};
  const thresholdPct = params.threshold_pct || 0.05;
  const totalValueUsd = params.total_value_usd || 0;

  let finalScore = 0;
  const breakdown = {};

  // ── Component 1: Allocation accuracy (up to 0.35) ──────────────────────
  // How close are post-execution allocations to targets?
  const postAllocations = estimatePostAllocations(simulation, state);

  if (postAllocations) {
    // Calculate remaining drift after rebalance
    const preDrift = totalDrift(currentAllocations, targetAllocations);
    const postDrift = totalDrift(postAllocations, targetAllocations);

    if (preDrift > 0) {
      // Score based on how much drift was reduced
      // Perfect rebalance: postDrift = 0, driftReduction = 1.0
      const driftReduction = clamp(1.0 - postDrift / preDrift, 0, 1);
      const accuracyScore = driftReduction * 0.35;
      finalScore += accuracyScore;
      breakdown.allocationAccuracy = accuracyScore;
    } else {
      // No drift to fix - full marks (edge case)
      finalScore += 0.35;
      breakdown.allocationAccuracy = 0.35;
    }
  } else {
    // Cannot determine post-execution allocations from simulation
    // Give moderate credit if the plan at least doesn't revert
    finalScore += 0.15;
    breakdown.allocationAccuracy = 0.15;
  }

  // ── Component 2: Minimal trading (up to 0.25) ──────────────────────────
  // Penalize unnecessary trades. A perfect rebalance touches only the
  // tokens that are drifted and trades only the amount needed.
  const numSwaps = plan.interactions.filter(
    (i) => i.call_data && i.call_data.length > 10
  ).length;
  // Approvals are paired with swaps, so actual swap count = interactions / 2
  const estimatedSwaps = Math.ceil(numSwaps / 2);

  // Count how many tokens actually need rebalancing
  const tokensNeedingRebalance = Object.keys(targetAllocations).filter(
    (token) => {
      const current = currentAllocations[token] || 0;
      const target = targetAllocations[token] || 0;
      return Math.abs(current - target) > thresholdPct * 0.25;
    }
  ).length;

  // Minimum possible swaps: ceil(tokensNeedingRebalance / 2)
  // (each swap moves value between two tokens)
  const minSwaps = Math.max(1, Math.ceil(tokensNeedingRebalance / 2));

  if (estimatedSwaps <= minSwaps) {
    finalScore += 0.25;
    breakdown.minimalTrading = 0.25;
  } else if (estimatedSwaps <= minSwaps * 2) {
    // Slightly more swaps than necessary - partial credit
    const efficiency = minSwaps / estimatedSwaps;
    const tradingScore = efficiency * 0.25;
    finalScore += tradingScore;
    breakdown.minimalTrading = tradingScore;
  } else {
    // Way too many swaps - likely unnecessary round-trips
    const tradingScore = clamp((minSwaps / estimatedSwaps) * 0.15, 0, 0.10);
    finalScore += tradingScore;
    breakdown.minimalTrading = tradingScore;
  }

  // ── Component 3: Gas efficiency (up to 0.20) ──────────────────────────
  const gasUsed = simulation.gasUsed || 0;
  // Baseline gas per swap: ~250k for approve + exactInputSingle
  const expectedGas = estimatedSwaps * 250000;
  const gasBest = estimatedSwaps * 120000;

  if (gasUsed > 0 && expectedGas > 0) {
    const gasScore =
      linearScore(expectedGas - gasUsed + gasBest, 0, expectedGas) * 0.20;
    finalScore += gasScore;
    breakdown.gasEfficiency = gasScore;
  } else if (gasUsed === 0) {
    // No gas data available - give moderate credit
    finalScore += 0.10;
    breakdown.gasEfficiency = 0.10;
  }

  // ── Component 4: Timing quality (up to 0.20) ──────────────────────────
  // Did the solver trigger near the threshold? Not too early (wasting gas
  // on small drifts) and not too late (missing the optimal window).
  const drift = maxDrift(currentAllocations, targetAllocations);

  if (drift >= thresholdPct) {
    // Good: triggered at or above threshold
    // Best timing: trigger right at threshold (1.0x - 1.5x threshold)
    // Acceptable: up to 2x threshold
    // Late: above 2x threshold (should have triggered earlier)
    const driftRatio = drift / thresholdPct;

    if (driftRatio <= 1.5) {
      // Ideal timing: near the threshold
      finalScore += 0.20;
      breakdown.timingQuality = 0.20;
    } else if (driftRatio <= 2.0) {
      // Acceptable: somewhat late
      const timingScore = linearScore(2.0 - driftRatio, 0, 0.5) * 0.20;
      finalScore += timingScore;
      breakdown.timingQuality = timingScore;
    } else {
      // Very late: drift is more than 2x threshold
      const timingScore = clamp(0.05, 0, 0.20);
      finalScore += timingScore;
      breakdown.timingQuality = timingScore;
    }
  } else if (drift >= thresholdPct * 0.5) {
    // Slightly early but close to threshold - partial credit
    const earlyRatio = drift / thresholdPct;
    const timingScore = linearScore(earlyRatio, 0.5, 1.0) * 0.15;
    finalScore += timingScore;
    breakdown.timingQuality = timingScore;
  } else {
    // Too early: drift well below threshold
    breakdown.timingQuality = 0.0;
  }

  finalScore = clamp(finalScore, 0, 1);

  return {
    score: finalScore,
    breakdown,
    metadata: {
      maxDrift: drift.toFixed(4),
      thresholdPct: thresholdPct,
      estimatedSwaps,
      tokensNeedingRebalance,
      gasUsed,
      postAllocations: postAllocations || "unavailable",
    },
  };
}

// ─── shouldTrigger (auto-triggered: monitors drift) ─────────────────────────

async function shouldTrigger(state, context) {
  const params = runtimeParams(state);
  const targetAllocations = params.target_allocations;
  const currentAllocations = params.current_allocations;
  const thresholdPct = params.threshold_pct;

  if (!targetAllocations || !currentAllocations || !thresholdPct) {
    return false;
  }

  // Trigger when any token drifts beyond the threshold
  const drift = maxDrift(currentAllocations, targetAllocations);
  return drift > thresholdPct;
}

// ─── export ─────────────────────────────────────────────────────────────────

module.exports = {
  config: {
    name: "RebalanceIntent",
    version: "1.0.0",
    type: "rebalance",
    supportedChains: [1, 8453],
    scoring: { minScore: 0, maxScore: 1, threshold: 0.4 },
  },
  validate,
  score,
  shouldTrigger,
};
