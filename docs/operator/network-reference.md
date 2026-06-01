# Network Reference — Subnet 112

Single source of truth for operator-facing addresses and endpoints. Use this when configuring a new validator or miner against the live network.

> **Versioning**: this page tracks the *current* mainnet state. When contracts get redeployed (e.g. the ValidatorRegistry-quorum migration), addresses change. Each entry below carries a "valid since" line so you can spot stale references.

## Bittensor

| Item | Value |
|---|---|
| Netuid | `112` |
| Network | `finney` |
| Subtensor endpoint | `wss://entrypoint-finney.opentensor.ai:443` |
| Public BT EVM RPC | `https://lite.chain.opentensor.ai` (chain `964`) |

Register a hotkey:

```bash
btcli subnet register --netuid 112 --subtensor.network finney \
  --wallet.name <YOUR_WALLET> --wallet.hotkey <YOUR_HOTKEY>
```

## Production API

| Item | Value |
|---|---|
| API endpoint | `https://api.minotaursubnet.com` |
| Frontend | `<PRODUCTION_FRONTEND_URL>` |

Miners pointing the agent loop at production use `--validator-url https://api.minotaursubnet.com` instead of `http://localhost:8080`.

## Mainnet contract addresses

> Addresses below reflect the **post-quorum-refactor** stack landed on 2026-05-21. `ValidatorRegistry` now holds the canonical `quorumBps` (single source of truth — `AppIntentBase` reads it at verification time, off-chain code reads it through `ProtocolConfig.from_validator_registry`). `AppRegistry` (introduced 2026-05-19) gates which `AppIntentBase`-derived contracts are accepted by validators and the relayer. All `AppIntentBase`-derived contracts were redeployed at the same time because their constructor signature changed. Verify any address against on-chain state before relying on it — `cast call $VALIDATOR_REGISTRY 'quorumBps()(uint256)'` should return a non-zero value on a current contract.

### Base (chain `8453`)

| Contract | Address | Valid since |
|---|---|---|
| `ValidatorRegistry` | `0x88a08d1105393EACE9B6f5ff678DbE508B8639aC` | 2026-05-21 |
| `AppRegistry` | `0x0B5fE44e90515571761D86C28c4855F325EDE098` | 2026-05-19 (state preserved across the 2026-05-21 refactor) |
| `DexAggregatorApp` | `0x0AeA6Ab70B384ADC6493d40e927ce53A7cefE035` | 2026-05-21 |

### BT EVM (chain `964`)

| Contract | Address | Valid since |
|---|---|---|
| `ValidatorRegistry` | `0x0B5fE44e90515571761D86C28c4855F325EDE098` | 2026-05-21 |
| `ChampionRegistry` | `0x33105027d03e76bf1F3679C0CB9b2688da383fb3` | 2026-05-21 |
| `AppRegistry` | `0x80758D3Bf11715c82dB9964C634d5Fd8a0C58aBF` | 2026-05-19 |

> The string `0x0B5fE44e9...` appears on both chains (Base `AppRegistry`, BT EVM `ValidatorRegistry`). They are independent contracts on independent chains — the deployer's nonce happened to align across the two deploys. Different code at each address; the collision is purely cosmetic.

ChampionRegistry holds its own independent `quorumBps` for champion-certification consensus. The validator-side env var `CHAMPION_QUORUM_BPS` should mirror whatever `cast call $CHAMPION_REGISTRY 'quorumBps()(uint256)' --rpc-url https://lite.chain.opentensor.ai` returns (currently `6666`).

### Ethereum mainnet (chain `1`)

The platform supports Ethereum mainnet execution, but the canonical DexAggregator deployment currently lives on Base. ValidatorRegistry on Ethereum mainnet TBD — operators running only Base do not need it; operators planning to support ETH-mainnet flows should ask before assuming an address.

## Cluster expectations

| Metric | Current target |
|---|---|
| Active validator count | 3 (post-refactor migration may grow this) |
| Quorum threshold (`quorumBps` on `ValidatorRegistry`) | `6666` (2-of-3 BFT). Read live with `cast call $VALIDATOR_REGISTRY 'quorumBps()(uint256)' --rpc-url $BASE_RPC`. |
| Champion quorum (`quorumBps` on `ChampionRegistry`) | `6666` — mirror the on-chain value with `CHAMPION_QUORUM_BPS` |
| Tick interval | `12s` (matches Ethereum block time) |
| Weight emission cadence | `1200s` (20 min) — matches Bittensor's `weights_set_rate_limit` of 100 blocks. Pass `--epoch-seconds 1200` to the validator daemon. The 60s default spam-rejects ~95% of attempts on subnet 112. |
| `ProtocolConfig` refresh cadence | `60s` — how often the daemon re-reads `quorumBps` and the validator set from `ValidatorRegistry`. Independent of weight emission. |
| Stake requirement for emissions | (TBD — set by Bittensor subnet rules; check current `metagraph` output) |

## Onboarding handshake

New validators need their EVM signing address added to the on-chain `ValidatorRegistry` on every chain they operate on. See the [validator quickstart Step 4](../validator/quickstart.md#step-4-get-onboarded-to-the-on-chain-validatorregistry) for the required information you send to the registry owner and the `cast` commands the owner runs to add you.

After the on-chain handshake, your daemon's signatures count toward order-consensus quorum and your hotkey can be assigned to the leader role when stake rotation puts you on top.

## Operational runbooks

- [Quorum management](./quorum-management.md) — how to change the network-wide threshold
- [Validator quickstart](../validator/quickstart.md) — full validator setup
- [Validator configuration](../validator/configuration.md) — env var reference
- [Validator troubleshooting](../validator/troubleshooting.md) — common failure modes
- [Miner quickstart](../miner/quickstart.md) — miner-side onboarding

## Changing this page

This page is the authoritative network reference. When you redeploy a contract or update a production endpoint, update this page in the same PR. Don't let it drift.
