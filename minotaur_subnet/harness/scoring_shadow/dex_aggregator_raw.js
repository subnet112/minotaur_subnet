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
//   raw_output = sum of output tokens delivered to the receiver (EXACT wei)
//   valid      = raw_output >= min (the slippage guard; below-min => invalid)
//
// Operators upload this (or their own raw-output variant) via
//   PUT /apps/{app_id}/scoring-shadow
// and the validator scores it ALONGSIDE the live JS, attaching the raw output to
// each BenchmarkResult.shadow_score. The relative rule (epoch/relative_scoring.py)
// then compares challenger vs champion per order on this raw output.
//
// EXACT-INTEGER WEI: output amounts are token wei (1e18..1e22+), well above JS's
// 2^53 safe-integer limit, so they are summed as BigInt — NOT parseFloat/double —
// and published as an EXACT DECIMAL STRING in `metadata.raw_output`. This makes
// the per-order comparison bit-exact and host-deterministic (no IEEE-754 rounding
// at the BPS boundary). The engine's score parser clamps `score` to [0, 1], so
// `score` here is only a bounded validity SENTINEL (1 valid / 0 invalid) — the
// authoritative value is the `metadata.raw_output` string, which the dual-load
// path reads. `valid` reflects the JS's own min-output guard.
// =============================================================================

var config = {
  name: "DexAggregatorRawShadow",
  version: "1.0.0",
  type: "dex_aggregator",
};

function runtimeParams(state) {
  return state.typed_context || state.raw_params || state.rawParams || {};
}

// Parse a token amount as EXACT integer wei (BigInt). Amounts arrive as exact
// decimal strings; this guards each conversion so a non-integer / garbage amount
// is SKIPPED (returns null), never thrown — a single bad transfer can't break
// the observe-only shadow score.
function toBigIntAmount(v) {
  if (v === null || v === undefined) return null;
  var s = String(v).trim();
  if (!/^[0-9]+$/.test(s)) return null; // non-negative integer wei only
  try {
    return BigInt(s);
  } catch (e) {
    return null;
  }
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
      metadata: { raw_output: "0" },
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
    return { score: 0, valid: false, reason: "No token transfers detected", metadata: { raw_output: "0" } };
  }

  // Sum the output-token transfers delivered to the receiver (or the app, which
  // delivers in _checkIntent) — IDENTICAL output-extraction logic to the live JS,
  // but summed as EXACT integer wei (BigInt) so token amounts above 2^53 are not
  // rounded by IEEE-754. A garbage/non-integer amount is skipped, not thrown.
  var total = BigInt(0);
  for (var i = 0; i < transfers.length; i++) {
    var t = transfers[i];
    var toAddr = (t.to_addr || t.to || "").toLowerCase();
    var tokenAddr = (t.token || t.token_address || "").toLowerCase();
    if (tokenAddr === tokenOut && (toAddr === receiver || toAddr === appAddr)) {
      var amt = toBigIntAmount(t.amount !== undefined && t.amount !== null ? t.amount : t.value);
      if (amt !== null) {
        total += amt;
      }
    }
  }

  if (total === BigInt(0)) {
    return {
      score: 0,
      valid: false,
      reason: "No output tokens received by receiver",
      metadata: { raw_output: "0" },
    };
  }

  // 4. Validity guard: the swap must clear the slippage-guard min if one is set —
  //    SAME `output < min => invalid` guard as the live scorer, as a BigInt
  //    comparison (no float).
  var minOut = toBigIntAmount(minAmountOut);
  if (minOut === null) minOut = BigInt(0);
  if (minOut > BigInt(0) && total < minOut) {
    return {
      score: 0,
      valid: false,
      reason: "Output below minimum: " + total.toString() + " < " + minOut.toString(),
      metadata: {
        raw_output: "0",
        output_amount: total.toString(),
        min_amount_out: minOut.toString(),
      },
    };
  }

  // 5. SHADOW SCORE carrier = metadata.raw_output, the RAW delivered output as an
  //    EXACT DECIMAL WEI STRING. No quote anchor, no gas term, no weighting — just
  //    what the receiver actually got. `score` is only a bounded validity sentinel
  //    (1 valid) because the engine clamps it to [0, 1]; the relative rule reads
  //    metadata.raw_output, not `score`.
  return {
    score: 1,
    valid: true,
    reason: "Raw output delivered: " + total.toString() + " (min=" + minOut.toString() + " gas=" + gasUsed + ")",
    breakdown: {
      min_amount_out: minOut.toString(),
      gas_used: gasUsed,
      num_transfers: transfers.length,
    },
    metadata: {
      raw_output: total.toString(),
      output_amount: total.toString(),
      min_amount_out: minOut.toString(),
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
