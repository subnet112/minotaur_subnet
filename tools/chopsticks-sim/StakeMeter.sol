// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IStakingV2 {
    function addStake(bytes32 hotkey, uint256 amountRao, uint256 netuid) external payable;
    function getStake(bytes32 hotkey, bytes32 coldkey, uint256 netuid) external view returns (uint256);
}

/// Measures alpha delivered by a stake, entirely within one dry-run (eth_call).
contract StakeMeter {
    IStakingV2 constant S = IStakingV2(0x0000000000000000000000000000000000000805);

    function readStake(bytes32 hotkey, bytes32 coldkey, uint256 netuid) external view returns (uint256) {
        return S.getStake(hotkey, coldkey, netuid);
    }

    function stakeAndMeasure(bytes32 hotkey, bytes32 coldkey, uint256 netuid, uint256 amountRao)
        external returns (uint256 before_, uint256 after_, uint256 delta)
    {
        before_ = S.getStake(hotkey, coldkey, netuid);
        S.addStake(hotkey, amountRao, netuid);
        after_ = S.getStake(hotkey, coldkey, netuid);
        delta = after_ - before_;
    }

    // AppIntentBase scoreIntent surface — the generic (IntentOrder, ExecutionPlan)
    // tuple. Present so the substrate backend's ported encoder can be exercised
    // end-to-end; returns a constant here (the real scoring lives in raw_output).
    struct IntentOrder {
        bytes32 orderId; address app; bytes4 selector; bytes intentParams; address submittedBy;
        uint256 chainId; uint256 deadline; uint256 nonce; bool perpetual;
        uint256 maxExecutions; uint256 cooldown;
    }
    struct Call { address target; uint256 value; bytes data; }
    struct Plan { Call[] calls; uint256 deadline; uint256 nonce; bytes metadata; }

    function scoreIntent(IntentOrder calldata, Plan calldata)
        external pure returns (uint256 score, bool valid)
    {
        return (4242, true);
    }

    // AppIntentBase exposes the configured relayer; the simulator discovers it via
    // this getter and uses it as the msg.sender for the scored call (see
    // SubtensorSimulator._discover_relayer). Fixed sentinel here for the test.
    function relayer() external pure returns (address) {
        return 0x000000000000000000000000000000000000bEEF;
    }
}
