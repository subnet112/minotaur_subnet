/**
 * Swap scoring module for E2E testing.
 *
 * Validates that a swap plan produced token transfers and scores
 * based on the number of output transfers observed.
 */
module.exports = {
    config: {
        name: "test-swap-scorer",
        version: "1.0",
        description: "Scoring module for swap E2E tests",
    },

    score: function(plan, state, context) {
        // Check simulation success
        if (!context || !context.simulation || !context.simulation.success) {
            return { score: 0, valid: false, reason: "simulation failed" };
        }

        var transfers = context.simulation.token_transfers || [];
        var hasOutput = transfers.length >= 2;

        // Check that at least one interaction exists
        var interactions = plan.interactions || [];
        if (interactions.length === 0) {
            return { score: 0, valid: false, reason: "no interactions in plan" };
        }

        // Score based on transfer quality
        var score = 0.3; // base
        if (hasOutput) {
            score = 0.85;
        } else if (transfers.length === 1) {
            score = 0.5;
        }

        return {
            score: score,
            valid: hasOutput,
            reason: hasOutput ? "swap produced expected transfers" : "insufficient output transfers",
            breakdown: {
                transfer_count: transfers.length,
                interaction_count: interactions.length,
            }
        };
    },

    shouldTrigger: function(state, context) {
        // For perpetual orders: trigger when price conditions are met
        var params = state.typed_context || state.raw_params || state.rawParams || {};
        var targetPrice = parseFloat(params.target_price || "0");
        if (targetPrice <= 0) return false;

        var prices = (context && context.prices) || {};
        var currentPrice = prices["ETH/USD"] || prices["WETH/USD"] || 0;

        return currentPrice > 0 && currentPrice <= targetPrice;
    }
};
