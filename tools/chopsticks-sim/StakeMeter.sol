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
}
