/**
 * TWAP (Time-Weighted Average Price) App Intent - JavaScript Scoring Layer
 *
 * Runs on validators inside a sandboxed environment. Solvers never see this
 * code; they only receive the final numeric score (black-box optimisation).
 *
 * TWAP splits a large swap into time-spaced chunks to minimise price impact.
 * This is an auto-triggered intent -- the subnet monitors time intervals and
 * triggers each chunk automatically.
 *
 * Scoring breakdown (0 - 1.0):
 *   - Chunk size accuracy:       up to 0.25  (correct amount per chunk)
 *   - Timing accuracy:           up to 0.25  (executing at the right interval)
 *   - Price vs TWAP benchmark:   up to 0.30  (execution price vs running TWAP)
 *   - Gas efficiency:            up to 0.20  (lower gas = better)
 *
 * State fields expected in state.raw_params / state.typed_context:
 *   total_amount       - Total input amount across all chunks (wei string)
 *   num_chunks         - Number of chunks to split into
 *   interval_seconds   - Seconds between each chunk execution
 *   chunks_executed    - How many chunks have been executed so far
 *   twap_price         - Running time-weighted average price (output/input ratio)
 *   chunk_amount       - Expected amount per chunk (wei string)
 *   last_chunk_time    - Timestamp of the last executed chunk
 *   input_token        - Address of the token being sold
 *   output_token       - Address of the token being bought
 *
 * Configuration injected at deploy time via {{PARAM}} placeholders:
 *   {{INPUT_TOKEN}}       - Address of the token being sold
 *   {{OUTPUT_TOKEN}}      - Address of the token being bought
 *   {{TOTAL_AMOUNT}}      - Total amount to swap across all chunks (wei string)
 *   {{NUM_CHUNKS}}        - Number of chunks
 *   {{INTERVAL_SECONDS}}  - Seconds between chunks
 *   {{SUPPORTED_CHAINS}}  - JSON array of supported chain IDs
 */

// --- helpers ----------------------------------------------------------------

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

// --- validate ---------------------------------------------------------------

async function validate(plan, state, context) {
  // 1. Structural checks
  if (!plan.interactions || plan.interactions.length === 0) {
    return { valid: false, reason: "No interactions in plan" };
  }
  if (plan.deadline < context.timestamp) {
    return { valid: false, reason: "Plan deadline has passed" };
  }

  // 2. Check that chunks remain
  const params = runtimeParams(state);
  const chunksExecuted = parseInt(params.chunks_executed || "0", 10);
  const numChunks = parseInt(params.num_chunks || "{{NUM_CHUNKS}}", 10);
  if (chunksExecuted >= numChunks) {
    return { valid: false, reason: "All chunks already executed" };
  }

  // 3. Simulate
  const simulation = context.simulation || await context.simulator.simulate(plan);
  if (!simulation.success) {
    return { valid: false, reason: `Simulation reverted: ${simulation.error || "unknown"}` };
  }

  // 4. Verify output token received
  const outputTransfers = (simulation.token_transfers || []).filter(
    (t) =>
      t.token.toLowerCase() === "{{OUTPUT_TOKEN}}".toLowerCase() &&
      t.to_addr.toLowerCase() === state.contract_address.toLowerCase()
  );
  if (outputTransfers.length === 0) {
    return { valid: false, reason: "No output token received" };
  }

  return { valid: true };
}

// --- score ------------------------------------------------------------------

async function score(plan, state, context) {
  const simulation = context.simulation || await context.simulator.simulate(plan);
  if (!simulation.success) {
    return { score: 0, breakdown: { reverted: -1 } };
  }

  const params = runtimeParams(state);
  const totalAmount = BigInt(params.total_amount || "{{TOTAL_AMOUNT}}");
  const numChunks = parseInt(params.num_chunks || "{{NUM_CHUNKS}}", 10);
  const intervalSeconds = parseInt(params.interval_seconds || "{{INTERVAL_SECONDS}}", 10);
  const chunksExecuted = parseInt(params.chunks_executed || "0", 10);
  const expectedChunkAmount = totalAmount / BigInt(numChunks);
  const twapPrice = parseFloat(params.twap_price || "0");
  const lastChunkTime = parseInt(params.last_chunk_time || "0", 10);

  let finalScore = 0;
  const breakdown = {};

  // --- Component 1: Chunk size accuracy (up to 0.25) -------------------------
  //
  // Measures whether the solver is swapping the correct per-chunk amount.
  // Perfect = full marks. Deviation >10% from expected chunk amount = 0.

  // Sum input token transfers out of the contract (the amount being swapped)
  const inputTransfers = (simulation.token_transfers || []).filter(
    (t) =>
      t.token.toLowerCase() === "{{INPUT_TOKEN}}".toLowerCase() &&
      t.from_addr.toLowerCase() === state.contract_address.toLowerCase()
  );
  const actualInputAmount = inputTransfers.reduce(
    (sum, t) => sum + BigInt(t.amount),
    0n
  );

  if (expectedChunkAmount > 0n && actualInputAmount > 0n) {
    // Calculate deviation as a ratio (0 = perfect, 1 = 100% off)
    const diff = actualInputAmount > expectedChunkAmount
      ? actualInputAmount - expectedChunkAmount
      : expectedChunkAmount - actualInputAmount;
    const deviationPct = Number((diff * 10000n) / expectedChunkAmount) / 10000;

    if (deviationPct <= 0.10) {
      // Within 10%: linear score from 0.25 (at 10% off) to full (at 0% off)
      const chunkScore = linearScore(1.0 - deviationPct, 0.9, 1.0) * 0.25;
      finalScore += chunkScore;
      breakdown.chunkSizeAccuracy = chunkScore;
    } else {
      breakdown.chunkSizeAccuracy = 0;
    }
  } else {
    breakdown.chunkSizeAccuracy = 0;
  }

  // --- Component 2: Timing accuracy (up to 0.25) ----------------------------
  //
  // Measures whether the chunk is executing at the expected interval.
  // On time = full marks. More than 30 seconds early or late = increasing penalty.

  if (lastChunkTime > 0 && intervalSeconds > 0) {
    const expectedTime = lastChunkTime + intervalSeconds;
    const timeDiff = Math.abs(context.timestamp - expectedTime);

    if (timeDiff <= 30) {
      // Within 30 seconds: full to near-full marks
      const timingScore = linearScore(30 - timeDiff, 0, 30) * 0.25;
      finalScore += timingScore;
      breakdown.timingAccuracy = timingScore;
    } else {
      // Beyond 30 seconds: penalty scales up to 2x interval (then 0)
      const maxLate = intervalSeconds * 2;
      const timingScore = linearScore(maxLate - timeDiff, 0, maxLate) * 0.10;
      finalScore += timingScore;
      breakdown.timingAccuracy = timingScore;
    }
  } else {
    // First chunk or no last_chunk_time: give full timing marks
    finalScore += 0.25;
    breakdown.timingAccuracy = 0.25;
  }

  // --- Component 3: Price vs TWAP benchmark (up to 0.30) --------------------
  //
  // Compare this chunk's execution price to the running TWAP.
  // Better than TWAP = bonus. Worse than TWAP = penalty.
  // "Price" here means output_amount / input_amount (higher is better for buyer).

  const outputTransfers = (simulation.token_transfers || []).filter(
    (t) =>
      t.token.toLowerCase() === "{{OUTPUT_TOKEN}}".toLowerCase() &&
      t.to_addr.toLowerCase() === state.contract_address.toLowerCase()
  );
  const totalOutput = outputTransfers.reduce(
    (sum, t) => sum + BigInt(t.amount),
    0n
  );

  if (actualInputAmount > 0n && totalOutput > 0n) {
    // Execution price: output per unit input (as a float)
    const executionPrice =
      Number((totalOutput * 1000000n) / actualInputAmount) / 1000000;

    if (twapPrice > 0) {
      // Compare to running TWAP
      const priceRatio = executionPrice / twapPrice;

      if (priceRatio >= 1.0) {
        // At or better than TWAP: base 0.20 + bonus up to 0.10
        const bonus = clamp((priceRatio - 1.0) * 10, 0, 1) * 0.10;
        const priceScore = 0.20 + bonus;
        finalScore += priceScore;
        breakdown.priceVsTwap = priceScore;
      } else {
        // Worse than TWAP: scale from 0.20 (at TWAP) down to 0 (at 90% of TWAP)
        const priceScore = linearScore(priceRatio, 0.90, 1.0) * 0.20;
        finalScore += priceScore;
        breakdown.priceVsTwap = priceScore;
      }
    } else {
      // No TWAP benchmark yet (first chunk): base score
      finalScore += 0.15;
      breakdown.priceVsTwap = 0.15;
    }
  } else {
    breakdown.priceVsTwap = 0;
  }

  // --- Component 4: Gas efficiency (up to 0.20) -----------------------------
  //
  // Lower gas usage is better. Baseline is a typical swap (~250k gas).

  const gasUsed = simulation.gas_used || 0;
  const gasBaseline = 250000;
  const gasBest = 100000;

  if (gasUsed > 0) {
    const gasScore = linearScore(gasBaseline - gasUsed + gasBest, 0, gasBaseline) * 0.20;
    finalScore += gasScore;
    breakdown.gasEfficiency = gasScore;
  } else {
    breakdown.gasEfficiency = 0;
  }

  finalScore = clamp(finalScore, 0, 1);

  return {
    score: finalScore,
    breakdown,
    metadata: {
      chunkIndex: chunksExecuted,
      expectedChunkAmount: expectedChunkAmount.toString(),
      actualInputAmount: actualInputAmount.toString(),
      totalOutput: totalOutput.toString(),
      twapPrice,
      gasUsed,
      numChunks,
      intervalSeconds,
    },
  };
}

// --- shouldTrigger ----------------------------------------------------------
//
// TWAP is auto-triggered. Returns true when:
// 1. There are remaining chunks to execute
// 2. Enough time has passed since the last chunk

async function shouldTrigger(state, context) {
  const params = runtimeParams(state);
  const chunksExecuted = parseInt(params.chunks_executed || "0", 10);
  const numChunks = parseInt(params.num_chunks || "{{NUM_CHUNKS}}", 10);

  // All chunks executed -- nothing to trigger
  if (chunksExecuted >= numChunks) {
    return false;
  }

  const lastChunkTime = parseInt(params.last_chunk_time || "0", 10);
  const intervalSeconds = parseInt(params.interval_seconds || "{{INTERVAL_SECONDS}}", 10);

  // First chunk: trigger immediately
  if (lastChunkTime === 0) {
    return true;
  }

  // Subsequent chunks: wait for the interval
  return context.timestamp - lastChunkTime >= intervalSeconds;
}

// --- export -----------------------------------------------------------------

module.exports = {
  config: {
    name: "TWAPIntent",
    version: "1.0.0",
    type: "twap",
    supportedChains: [1, 8453],
    scoring: { minScore: 0, maxScore: 1, threshold: 0.4 },
  },

  validate,
  score,
  shouldTrigger,
};
