// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title ToyScoreApp — minimal scoreIntent app for the GasMeter integration
/// test (tests/unit/test_gasmeter_anvil_integration.py).
///
/// Implements EXACTLY the two entry points AnvilSimulator._simulate_via_
/// score_intent needs, with the exact tuple layout the simulator ABI-encodes:
///   - relayer() (public getter) — resolved to impersonate/install the meter;
///   - scoreIntent(Order, Plan) payable onlyRelayer returning (score, valid),
///     selector == keccak("scoreIntent((bytes32,address,bytes4,bytes,address,
///     uint256,uint256,uint256,bool,uint256,uint256),((address,uint256,
///     bytes)[],uint256,uint256,bytes))")[:4].
///
/// Burns a deterministic amount of gas (fixed keccak spin) and mutates state
/// so the metered probe measures real, repeatable work.
///
/// Build (same profile as the vendored GasMeter runtime):
///   solc 0.8.24, evm_version=cancun, optimizer=true, runs=200 (forge build).
contract ToyScoreApp {
    address public relayer;
    uint256 public counter;

    struct Order {
        bytes32 orderId;
        address app;
        bytes4 selector;
        bytes params;
        address submittedBy;
        uint256 chainId;
        uint256 deadline;
        uint256 nonce;
        bool perpetual;
        uint256 maxExecutions;
        uint256 cooldown;
    }

    struct Call {
        address target;
        uint256 value;
        bytes data;
    }

    struct Plan {
        Call[] calls;
        uint256 deadline;
        uint256 nonce;
        bytes metadata;
    }

    event Scored(address caller, uint256 score);

    constructor(address _relayer) {
        relayer = _relayer;
    }

    function scoreIntent(Order calldata order, Plan calldata plan)
        external
        payable
        returns (uint256 score, bool valid)
    {
        require(msg.sender == relayer, "Only relayer");
        // Deliberate-revert switch for the metered-revert test case:
        // intent_params starting with 0xde bubbles a reasoned revert.
        if (order.params.length > 0 && order.params[0] == 0xde) {
            revert("toy: deliberate revert");
        }
        // Deterministic gas burn — repeatable across runs and hosts.
        uint256 acc = uint256(order.orderId) ^ plan.nonce;
        for (uint256 i = 0; i < 150; i++) {
            acc = uint256(keccak256(abi.encode(acc, i)));
        }
        counter += 1;
        score = 7500 + (acc % 500);
        valid = true;
        emit Scored(msg.sender, score);
    }
}
