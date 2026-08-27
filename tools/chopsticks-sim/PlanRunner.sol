// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// Executes a whole plan inside ONE dry-run, so state composes between calls.
///
/// Chopsticks cannot build blocks (pallet_drand's per-block hook needs a
/// BLS12-381 host function its executor lacks), so every `ck_ethCall` is an
/// independent dry-run against the pinned fork. Running a plan's interactions
/// as N separate calls therefore gave NO composition: interaction 2 could not
/// see interaction 1's effects, so a destination leg that wrapped native and
/// then moved the wrapper measured nothing — the wrap had never happened as
/// far as the transfer was concerned.
///
/// Anvil has no such limit, so chain 964 silently scored by different rules
/// than chains 1 and 8453. This restores parity the only way the fork allows:
/// one call, all interactions, state composing inside it — the same
/// measuring-router trick StakeMeter already uses for staking.
///
/// Installed via `anvil_setCode` AT THE EXECUTOR ADDRESS, so sub-calls carry
/// `msg.sender == executor` exactly as they do on anvil. (Same idiom as the
/// anvil gas meter, which installs at the relayer address so `onlyRelayer`
/// passes untouched.)
contract PlanRunner {
    struct Call {
        address target;
        uint256 value;
        bytes data;
    }

    /// @param calls the plan's interactions, in order
    /// @param watch addresses whose NATIVE balance is sampled either side of
    ///        the whole span. Native movement emits no ERC-20 Transfer log, so
    ///        a bridge that credits native (Tensorplex on 964) is invisible to
    ///        log-based delivery accounting; this is how it becomes measurable.
    function runPlan(Call[] calldata calls, address[] calldata watch)
        external
        payable
        returns (uint256[] memory balancesBefore, uint256[] memory balancesAfter, bytes[] memory rets)
    {
        balancesBefore = new uint256[](watch.length);
        for (uint256 i; i < watch.length; ++i) {
            balancesBefore[i] = watch[i].balance;
        }

        rets = new bytes[](calls.length);
        for (uint256 i; i < calls.length; ++i) {
            (bool ok, bytes memory ret) = calls[i].target.call{value: calls[i].value}(calls[i].data);
            if (!ok) {
                // Bubble the revert unchanged. Swallowing it would report a
                // failed leg as a successful one that moved nothing, which is
                // the exact ambiguity the delivery diagnosis exists to remove.
                assembly {
                    revert(add(ret, 0x20), mload(ret))
                }
            }
            rets[i] = ret;
        }

        balancesAfter = new uint256[](watch.length);
        for (uint256 i; i < watch.length; ++i) {
            balancesAfter[i] = watch[i].balance;
        }
    }

    /// Behave like a code-less EOA for anything that is not `runPlan`. This
    /// code sits at the executor address for the span of one probe, and a
    /// contract poking that address must see what it would have seen without
    /// us: accepts value, returns empty.
    fallback() external payable {}

    receive() external payable {}
}
