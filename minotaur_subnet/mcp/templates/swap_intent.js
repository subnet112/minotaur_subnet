/**
 * Swap App Intent - JavaScript Scoring Layer
 *
 * Runs on validators inside a sandboxed environment. Solvers never see this
 * code; they only receive the final numeric score (black-box optimisation).
 *
 * Scoring breakdown (0 - 1.0):
 *   - Output amount vs quote:  up to 0.50  (core value)
 *   - Gas efficiency:          up to 0.20
 *   - Price impact:            up to 0.20
 *   - Route simplicity bonus:  up to 0.10
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

// ─── validate ───────────────────────────────────────────────────────────────

async function validate(plan, state, context) {
  // 1. Structural checks
  if (!plan.interactions || plan.interactions.length === 0) {
    return { valid: false, reason: "No interactions in plan" };
  }
  if (plan.deadline < context.timestamp) {
    return { valid: false, reason: "Plan deadline has passed" };
  }

  // 2. Simulate
  const simulation = context.simulation || await context.simulator.simulate(plan);
  if (simulation.reverts) {
    return { valid: false, reason: `Simulation reverted: ${simulation.error}` };
  }

  // 3. Verify output token received
  const params = runtimeParams(state);
  const transfers = simulation.tokenTransfers || simulation.token_transfers || [];
  const outputTransfers = transfers.filter(
    (t) =>
      t.token.toLowerCase() === params.output_token.toLowerCase() &&
      (t.to || t.to_addr || "").toLowerCase() === state.contractAddress.toLowerCase()
  );
  if (outputTransfers.length === 0) {
    return { valid: false, reason: "No output token received" };
  }

  const totalOutput = outputTransfers.reduce(
    (sum, t) => sum + BigInt(t.amount),
    0n
  );
  const minOutput = BigInt(params.min_output_amount || "0");
  if (minOutput > 0n && totalOutput < minOutput) {
    return {
      valid: false,
      reason: `Output ${totalOutput} below minimum ${minOutput}`,
    };
  }

  // 4. Warnings
  const warnings = [];
  const priceImpact = simulation.priceImpact || simulation.price_impact;
  if (priceImpact && priceImpact > 1.0) {
    warnings.push(`High price impact: ${priceImpact.toFixed(2)}%`);
  }

  return { valid: true, warnings: warnings.length > 0 ? warnings : undefined };
}

// ─── score ───────────────────────────────────────────────────────────────────

async function score(plan, state, context) {
  const simulation = context.simulation || await context.simulator.simulate(plan);
  if (simulation.reverts) {
    return { score: 0, breakdown: { reverted: -1 } };
  }

  // Calculate total output received
  const params = runtimeParams(state);
  const transfers = simulation.tokenTransfers || simulation.token_transfers || [];
  const outputTransfers = transfers.filter(
    (t) =>
      t.token.toLowerCase() === params.output_token.toLowerCase() &&
      (t.to || t.to_addr || "").toLowerCase() === state.contractAddress.toLowerCase()
  );
  const totalOutput = outputTransfers.reduce(
    (sum, t) => sum + BigInt(t.amount),
    0n
  );
  const minOutput = BigInt(params.min_output_amount || "0");

  let finalScore = 0;
  const breakdown = {};

  // ── Scoring weights ─────────────────────────────────────────────────
  const W_OUTPUT = 0.50;
  const W_GAS = 0.20;
  const W_IMPACT = 0.20;
  const W_ROUTE = 0.10;

  // ── Component 1: Output amount (up to W_OUTPUT) ──────────────────────
  if (minOutput > 0n && totalOutput < minOutput) {
    // Below minimum: partial credit based on how close we are
    const ratio = Number((totalOutput * 10000n) / minOutput) / 10000;
    const outputScore = clamp(ratio * (W_OUTPUT * 0.5), 0, W_OUTPUT * 0.5);
    finalScore += outputScore;
    breakdown.outputAmount = outputScore;
    // Early return - below minimum is a failing score
    return { score: clamp(finalScore, 0, 1), breakdown };
  }

  // Meets minimum: base 60% of W_OUTPUT, then up to +40% for exceeding it
  const outputBase = W_OUTPUT * 0.6;
  breakdown.outputBase = outputBase;
  finalScore += outputBase;

  // Bonus for exceeding minimum (1% improvement = +0.04, capped at 40% of W_OUTPUT)
  if (minOutput > 0n) {
    const outputBonusCap = W_OUTPUT * 0.4;
    const improvement =
      Number(((totalOutput - minOutput) * 10000n) / minOutput) / 100;
    const improvementBonus = clamp(improvement * 0.04, 0, outputBonusCap);
    finalScore += improvementBonus;
    breakdown.outputBonus = improvementBonus;
  }

  // ── Component 2: Gas efficiency (up to W_GAS) ────────────────────────
  const gasUsed = simulation.gasUsed || simulation.gas_used || 0;
  const gasBaseline = 250000; // Expected gas for a typical swap
  const gasBest = 100000; // Very efficient swap

  if (gasUsed > 0) {
    const gasScore = linearScore(gasBaseline - gasUsed + gasBest, 0, gasBaseline) * W_GAS;
    finalScore += gasScore;
    breakdown.gasEfficiency = gasScore;
  }

  // ── Component 3: Price impact (up to W_IMPACT) ───────────────────────
  const priceImpact = simulation.priceImpact !== undefined ? simulation.priceImpact
    : simulation.price_impact;
  if (priceImpact !== undefined && priceImpact !== null) {
    // Lower price impact is better
    const impactScore = linearScore(1.0 - priceImpact, 0, 1) * W_IMPACT;
    finalScore += impactScore;
    breakdown.priceImpact = impactScore;
  } else {
    // No price impact data: assume moderate
    finalScore += W_IMPACT * 0.5;
    breakdown.priceImpact = W_IMPACT * 0.5;
  }

  // ── Component 4: Route simplicity (up to W_ROUTE) ────────────────────
  const numHops = plan.interactions.length;
  if (numHops <= 1) {
    finalScore += W_ROUTE;
    breakdown.routeSimplicity = W_ROUTE;
  } else if (numHops <= 3) {
    finalScore += W_ROUTE * 0.5;
    breakdown.routeSimplicity = W_ROUTE * 0.5;
  } else {
    breakdown.routeSimplicity = 0.0;
  }

  finalScore = clamp(finalScore, 0, 1);

  return {
    score: finalScore,
    breakdown,
    metadata: {
      simulatedOutput: totalOutput.toString(),
      minOutput: minOutput.toString(),
      gasUsed,
      priceImpact: priceImpact,
      hops: numHops,
    },
  };
}

// ─── shouldTrigger (user-triggered swaps don't auto-trigger) ────────────────

async function shouldTrigger(state, context) {
  return false;
}

// ─── export ─────────────────────────────────────────────────────────────────

module.exports = { validate, score, shouldTrigger };
