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
| API endpoint | `<PRODUCTION_API_URL>` (see project announcement channel) |
| Frontend | `<PRODUCTION_FRONTEND_URL>` |

Miners pointing the agent loop at production use `--validator-url <PRODUCTION_API_URL>` instead of `http://localhost:8080`. The current URL is published via the project announcement channel — check the [project README](../../README.md) for the active contact / status page. Don't hardcode a URL from a stale doc cache.

## Mainnet contract addresses

> The ValidatorRegistry and DexAggregatorApp addresses listed here are valid for the **pre-quorum-refactor** stack. When the new `ValidatorRegistry` (which holds the canonical `quorumBps`) and the matching new `DexAggregatorApp` are deployed and switched over, these addresses change. Check the project announcement channel before relying on a specific address.

### Base (chain `8453`)

| Contract | Address | Valid since |
|---|---|---|
| `ValidatorRegistry` (pre-refactor) | `0xD3c8eaf62ff29fe459bfBF523545b288600b4777` | 2026-04 |
| `DexAggregatorApp` | `0x27e789F6AFC7c77f2Cb094d868e7AD850ff4D45a` | 2026-04 |

Once the ValidatorRegistry redeploy lands, this page will list both the old and new addresses with a transition window.

### BT EVM (chain `964`)

| Contract | Address | Valid since |
|---|---|---|
| `ChampionRegistry` | `0x553F8651C7Ee73D11fD7b3b80f3ec96DBD28a16c` | 2026-04 |

ChampionRegistry holds its own independent `quorumBps` for champion-certification consensus. The validator-side env var `CHAMPION_QUORUM_BPS` should mirror whatever `cast call $CHAMPION_REGISTRY 'quorumBps()(uint256)'` returns.

### Ethereum mainnet (chain `1`)

The platform supports Ethereum mainnet execution, but the canonical DexAggregator deployment currently lives on Base. ValidatorRegistry on Ethereum mainnet TBD — operators running only Base do not need it; operators planning to support ETH-mainnet flows should ask before assuming an address.

## Cluster expectations

| Metric | Current target |
|---|---|
| Active validator count | 3 (post-refactor migration may grow this) |
| Quorum threshold (`quorumBps` on `ValidatorRegistry`) | `6666` (2-of-3 BFT). Read live with `make get-quorum-base`. |
| Champion quorum (`quorumBps` on `ChampionRegistry`) | `6666` — mirror the on-chain value with `CHAMPION_QUORUM_BPS` |
| Tick interval | `12s` (matches Ethereum block time) |
| Epoch duration | `60s` (weight emission cadence, `ProtocolConfig` refresh cadence) |
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
