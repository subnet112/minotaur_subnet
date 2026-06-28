// =============================================================================
// DexAggregatorApp — RAW-OUTPUT SHADOW Scoring Module
//
// SHADOW reference scorer for the relative per-order scoring path. It is a copy
// of the live dex_aggregator_scoring.js with the SCORING CHANGED to return the
// RAW delivered output to the receiver — no quote anchor, no gas term, no [0,1]
// clamp, no weighting. Everything else (output extraction, the min-output
// validity guard, config/manifest/validate) is identical so it slots into the
// same JsExecutionEngine and benchmark corpus.
//
//   score = outputAmount        (raw tokens delivered to the receiver)
//   valid = outputAmount >= min (the slippage guard; below-min => score 0)
//
// Operators upload this (or their own raw-output variant) via
//   PUT /apps/{app_id}/scoring-shadow
// and the validator scores it ALONGSIDE the live JS, attaching the raw output to
// each BenchmarkResult.shadow_score. The relative rule (epoch/relative_scoring.py)
// then compares challenger vs champion per order on this raw output.
//
// NOTE ON UNITS: the engine's score parser clamps `score` to [0, 1], so the
// authoritative raw value is ALSO published in `metadata.raw_output` (unclamped);
// the dual-load path reads it from there. `valid` here reflects the JS's own
// min-output guard (the engine's dict parser does not propagate it).
// =============================================================================

var config = {
  name: "DexAggregatorRawShadow",
  version: "1.0.0",
  type: "dex_aggregator",
};

function runtimeParams(state) {
  return state.typed_context || state.raw_params || state.rawParams || {};
}

var manifest = {
  intent_functions: [
    {
      name: "swap",
      description:
        "SHADOW raw-output scorer for the DEX swap intent. Same swap interface as the live app; scores the raw output delivered to the receiver.",
      params: {
        input_token: { type: "address", description: "Input token address", source: "user" },
        output_token: { type: "address", description: "Output token address", source: "user" },
        input_amount: { type: "uint256", description: "Amount of input tokens to swap", source: "user" },
        min_output_amount: {
          type: "uint256",
          description: "Minimum acceptable output amount",
          source: "quote",
          quote_field: "suggested_min_output",
        },
        receiver: {
          type: "address",
          description: "Address to receive output tokens (defaults to submitter)",
          source: "system",
        },
      },
    },
  ],
};

function score(plan, state, context) {
  var sim = context.simulation || {};

  // 1. Simulation must have succeeded.
  if (!sim.success) {
    return {
      score: 0,
      valid: false,
      reason: "Simulation failed: " + (sim.error || "unknown"),
      metadata: { raw_output: 0 },
    };
  }

  // 2. Order params (snake_case primary, camelCase fallback) — SAME extraction
  //    as the live scorer.
  var params = runtimeParams(state);
  var minAmountOut = params.min_output_amount || params.min_amount_out || params.minAmountOut || "0";
  var tokenOut = (params.output_token || params.token_out || params.tokenOut || "").toLowerCase();
  var receiver = (params.receiver || params.submitted_by || "").toLowerCase();
  var appAddr = (state.contract_address || "").toLowerCase();

  // 3. Token transfers from the simulation.
  var transfers = sim.token_transfers || sim.tokenTransfers || [];
  var gasUsed = sim.gas_used || sim.gasUsed || 0;

  if (transfers.length === 0) {
    return { score: 0, valid: false, reason: "No token transfers detected", metadata: { raw_output: 0 } };
  }

  // Sum the output-token transfers delivered to the receiver (or the app, which
  // delivers in _checkIntent) — IDENTICAL output-extraction logic to the live JS.
  var outputAmount = 0;
  for (var i = 0; i < transfers.length; i++) {
    var t = transfers[i];
    var toAddr = (t.to_addr || t.to || "").toLowerCase();
    var tokenAddr = (t.token || t.token_address || "").toLowerCase();
    if (tokenAddr === tokenOut && (toAddr === receiver || toAddr === appAddr)) {
      outputAmount += parseFloat(t.amount || t.value || "0");
    }
  }

  if (outputAmount === 0) {
    return {
      score: 0,
      valid: false,
      reason: "No output tokens received by receiver",
      metadata: { raw_output: 0 },
    };
  }

  // 4. Validity guard: the swap must clear the slippage-guard min if one is set —
  //    SAME `output < min => {score:0, valid:false}` guard as the live scorer.
  var minOut = parseFloat(minAmountOut);
  if (minOut > 0 && outputAmount < minOut) {
    return {
      score: 0,
      valid: false,
      reason:
        "Output below minimum: " + outputAmount.toFixed(0) + " < " + minOut.toFixed(0),
      metadata: { raw_output: 0, output_amount: outputAmount, min_amount_out: minOut },
    };
  }

  // 5. SHADOW SCORE = the RAW delivered output. No quote anchor, no gas term, no
  //    [0,1] clamp, no weighting — just what the receiver actually got.
  return {
    score: outputAmount,
    valid: true,
    reason: "Raw output delivered: " + outputAmount.toFixed(0) + " (min=" + minOut.toFixed(0) + " gas=" + gasUsed + ")",
    breakdown: {
      raw_output: outputAmount,
      min_amount_out: minOut,
      gas_used: gasUsed,
      num_transfers: transfers.length,
    },
    metadata: {
      raw_output: outputAmount,
      output_amount: outputAmount,
      min_amount_out: minOut,
    },
  };
}

function validate(plan, state, context) {
  // Structural validation before scoring — identical to the live scorer.
  if (!plan || !plan.calls || plan.calls.length === 0) {
    return { score: 0, valid: false, reason: "Empty execution plan" };
  }

  var params = runtimeParams(state);
  var tokenIn = params.input_token || params.tokenIn || params.token_in || "";
  var tokenOut = params.output_token || params.tokenOut || params.token_out || "";
  if (!tokenIn || !tokenOut) {
    return { score: 0, valid: false, reason: "Missing input_token or output_token in state" };
  }
  if (tokenIn.toLowerCase() === tokenOut.toLowerCase()) {
    return { score: 0, valid: false, reason: "input_token == output_token" };
  }

  return { score: 0, valid: true, reason: "Validation passed" };
}

module.exports = {
  config: config,
  manifest: manifest,
  score: score,
  validate: validate,
  get_manifest: function () {
    return manifest;
  },
};
