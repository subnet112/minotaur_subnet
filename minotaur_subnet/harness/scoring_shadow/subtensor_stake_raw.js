// =============================================================================
// Subtensor staking/vault App — RAW-OUTPUT SHADOW Scoring Module
//
// The substrate analog of dex_aggregator_raw.js. Where the DEX scorer sums ERC-20
// Transfer outputs to the receiver, this reads the DELIVERED ALPHA that the
// SubtensorSimulator captured from the App's scored (terminal) call and published
// as a typed `delivered_output` state_change (see
// minotaur_subnet/simulator/subtensor_simulator.py). Staking precompiles emit NO
// EVM logs, so delivered output CANNOT come from Transfer topics — it comes from
// the state-delta the measuring call returns.
//
//   raw_output = delivered alpha (EXACT u64 wei, from state_changes.delivered_output)
//   valid      = raw_output >= min (the slippage guard; below-min => invalid)
//
// Operators upload this (or their own raw-output variant) via
//   PUT /apps/{app_id}/scoring-shadow
// The relative rule (epoch/relative_scoring.py) then compares challenger vs
// champion per order on this raw output — identical machinery to the DEX path.
//
// EXACT-INTEGER WEI: alpha amounts (u64, up to ~1.8e19) exceed JS's 2^53 safe
// integer, so they are handled as BigInt and published as an EXACT DECIMAL STRING
// in `metadata.raw_output`. `score` is only a bounded validity sentinel (the
// engine clamps it to [0,1]); the authoritative value is `metadata.raw_output`.
// =============================================================================

var config = {
  name: "SubtensorStakeRawShadow",
  version: "1.0.0",
  type: "subtensor_stake",
};

function runtimeParams(state) {
  return state.typed_context || state.raw_params || state.rawParams || {};
}

// state.simulation.state_changes / .stateChanges is what SubtensorSimulator
// populates (mirrors how the DEX scorer reads state.simulation.token_transfers).
function stateChanges(state) {
  var sim = state.simulation || state.sim || {};
  return sim.state_changes || sim.stateChanges || [];
}

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
      name: "stake",
      description:
        "SHADOW raw-output scorer for a subtensor staking/vault intent. Scores the raw alpha delivered by the stake (state-delta), not EVM logs.",
      params: {
        netuid: { type: "uint256", description: "Subnet to stake into", source: "user" },
        hotkey: { type: "bytes32", description: "Validator hotkey to stake to", source: "user" },
        amount_rao: { type: "uint256", description: "TAO to stake, in rao", source: "user" },
        min_output_amount: {
          type: "uint256",
          description: "Minimum acceptable alpha out",
          source: "quote",
          quote_field: "suggested_min_output",
        },
      },
    },
  ],
};

// Sum every delivered_output state_change (there is normally exactly one).
function deliveredAlpha(state) {
  var changes = stateChanges(state);
  var total = BigInt(0);
  var found = false;
  for (var i = 0; i < changes.length; i++) {
    var c = changes[i] || {};
    if (c.type === "delivered_output") {
      var amt = toBigIntAmount(c.amount);
      if (amt !== null) {
        total += amt;
        found = true;
      }
    }
  }
  return found ? total : null;
}

function score(state) {
  var delivered = deliveredAlpha(state);
  if (delivered === null) {
    // no delivered_output captured -> the plan didn't produce a measurable stake
    return { score: 0, valid: false, metadata: { raw_output: "0", reason: "no_delivered_output" } };
  }
  var params = runtimeParams(state);
  var min = toBigIntAmount(params.min_output_amount) || BigInt(0);
  var valid = delivered >= min;
  return {
    score: valid ? 1 : 0, // bounded validity sentinel; raw_output is authoritative
    valid: valid,
    metadata: { raw_output: delivered.toString() },
  };
}

// Export shape mirrors dex_aggregator_raw.js so it slots into the same engine.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { config: config, manifest: manifest, score: score };
}
