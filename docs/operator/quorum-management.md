# Quorum Management

Reconfiguring the network-wide validator quorum threshold on a chain.

## What you're changing

`quorumBps` lives on the `ValidatorRegistry` contract, one per chain. Every `AppIntentBase` deployed against that registry reads the value at execution time, so a single owner transaction reconfigures every App on the chain. Off-chain validators read the same value at startup and refresh it once per epoch (default ~60s) — they pick up your change without a restart.

This means:

- **One source of truth**: the on-chain `ValidatorRegistry.quorumBps()`. No env vars to coordinate across operators.
- **One operator action**: a `setQuorumBps(uint256)` tx as the registry owner.
- **Propagation lag**: up to one refresh interval (default 60s) before every validator's local cache catches up. During the lag window the on-chain enforcement is what matters, and that flips atomically with the transaction.

## Read the current value

```bash
make get-quorum-base     # Base mainnet
make get-quorum-eth      # Ethereum mainnet
make get-quorum-btevm    # Bittensor EVM
```

Or directly via `cast`:

```bash
cast call $VALIDATOR_REGISTRY_BASE 'quorumBps()(uint256)' --rpc-url $BASE_RPC_URL
```

The contract returns basis points: `6666` = 66.66% (2-of-3 BFT), `8000` = 80%, `10000` = unanimous.

## Change the value

The Makefile drives a Foundry script (`contracts/script/SetQuorum.s.sol`):

```bash
make set-quorum-base BPS=8000
```

Required env (export before invoking):

| Variable | Purpose |
|---|---|
| `REGISTRY_OWNER_PRIVATE_KEY` | Hex private key of the `ValidatorRegistry` owner |
| `BASE_VALIDATOR_REGISTRY` / `ETH_VALIDATOR_REGISTRY` / `BTEVM_VALIDATOR_REGISTRY` | Registry address per chain |
| `BASE_RPC_URL` / `ETH_RPC_URL` / `BITTENSOR_EVM_UPSTREAM_RPC_URL` | RPC endpoint per chain |

The script verifies the new value on-chain before exiting, so a silent failure on broadcast is detected.

For each operational chain you support, you'll typically run all three:

```bash
make set-quorum-eth   BPS=8000
make set-quorum-base  BPS=8000
make set-quorum-btevm BPS=8000
```

## Choosing a value

| BPS | Effective with N validators | Notes |
|---|---|---|
| `6666` | 2-of-3 / 3-of-4 / 5-of-7 | BFT-correct for any N ≥ 3 (`ceil(2/3 * N)`). Tolerates one byzantine fault at N=4, two at N=7. |
| `7500` | 3-of-3 / 3-of-4 / 4-of-5 | Stricter than BFT; rejects more proposals on liveness, tightens safety. |
| `8000` | 3-of-3 / 4-of-4 / 4-of-5 | Same trade as 7500 but rounded up. |
| `10000` | unanimous | All validators must sign. Maximum safety, minimum liveness — one offline validator halts the cluster. Default for single-validator MVP. |

Default at deploy: `6666`. Match this when you onboard a new chain unless you have a specific reason to deviate.

## What happens after `setQuorumBps`

1. **Tx mined**: `ValidatorRegistry.quorumBps` storage slot updated; `QuorumBpsUpdated` event emitted.
2. **Contract enforcement flips atomically**: any `executeIntent` after this block uses the new threshold.
3. **Off-chain refresh**: each validator's `ProtocolConfig.refresh_loop` re-reads the registry once per epoch (default 60s). On change it logs at WARNING:
   ```
   ProtocolConfig: quorum_bps changed 6666 -> 8000 on ValidatorRegistry 0x... — consumers pick up the new value on their next tick
   ```
4. **During the lag window** (≤ refresh interval): some validators may still locally believe the old threshold. That's fine — the contract is what enforces. The lag never causes safety issues, only at-most-one-epoch of slightly-suboptimal signature collection behaviour.

## ChampionRegistry (separate)

`ChampionRegistry` on BT EVM has its own independent `quorumBps` for champion adoption consensus. It is not currently routed through `ProtocolConfig`. Keep it in sync manually:

```bash
make set-champion-quorum BPS=8000
```

This is documented technical debt; a follow-up will consolidate both registries.

## Troubleshooting

**`Only owner` revert on broadcast**: `REGISTRY_OWNER_PRIVATE_KEY` doesn't match the registry's owner. Read the current owner with:

```bash
cast call $REGISTRY 'owner()(address)' --rpc-url $RPC_URL
```

**`Invalid quorum` revert**: BPS must be in `(0, 10000]`. Anything else reverts.

**Daemon logs don't show the change**: ProtocolConfig refresh is silent on no-change. Check the refresh interval setting (default 60s) and wait one cycle. If your daemon has `QUORUM_BPS_OVERRIDE` set, the on-chain change is ignored — clear the env var and restart.
