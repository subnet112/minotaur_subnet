// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IAppIntentBase - Interface for Minotaur App Intent contracts
/// @notice All App contracts inherit from AppIntentBase which implements this interface
interface IAppIntentBase {
    // ── Structs ──────────────────────────────────────────────────────────

    struct Call {
        address target;
        uint256 value;
        bytes callData;
    }

    struct IntentOrder {
        bytes32 orderId;
        address app;
        bytes4 intentSelector;
        bytes intentParams;
        address submittedBy;
        uint256 chainId;
        uint256 deadline;
        uint256 nonce;
        bool perpetual;
        uint256 maxExecutions;
        uint256 cooldown;
    }

    struct ExecutionPlan {
        Call[] calls;
        uint256 deadline;
        uint256 nonce;
        bytes metadata;
    }

    // ── Events ───────────────────────────────────────────────────────────

    event IntentExecuted(
        bytes32 indexed orderId,
        address indexed submittedBy,
        uint256 score,
        bytes32 planHash,
        uint256 gasUsed
    );

    event IntentRejected(
        bytes32 indexed orderId,
        string reason
    );

    // ── Core ─────────────────────────────────────────────────────────────

    function executeIntent(
        IntentOrder calldata order,
        ExecutionPlan calldata plan,
        bytes calldata userSignature,
        bytes[] calldata validatorSignatures
    ) external payable;

    // ── Simulation ────────────────────────────────────────────────────────

    function scoreIntent(
        IntentOrder calldata order,
        ExecutionPlan calldata plan
    ) external returns (uint256 score, bool valid);

    // ── Views ────────────────────────────────────────────────────────────

    function quorumBps() external view returns (uint256);
    function scoreThreshold() external view returns (uint256);
    function relayer() external view returns (address);
}
