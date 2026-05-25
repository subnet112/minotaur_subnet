# Minotaur Roadmap

**Updated**: April 9, 2026

Minotaur is a **generic intent execution platform** on Bittensor Subnet 112.
Developers define an outcome and a scoring function; the network's Solving
Engine figures out optimal execution. The first app is a DEX aggregator —
the platform supports any on-chain operation expressible as an intent.
Launch is centralised-first, then progressively decentralised. Smart
contracts enforce safety invariants at every phase.

---

## Timeline

| Phase | Name                              | Target            | Duration |
|------:|-----------------------------------|-------------------|---------:|
| **0** | Build + Local Testnet             | Dec '25 – Apr 14  | ~17 wks  |
| **1** | Closed Beta — Mainnet             | Apr 14 – Apr 28   | 2 wks    |
| **2** | Code Release + Onboarding         | Apr 28 – May 5    | 1 wk     |
| **3** | Subnet Goes Live                  | May 5 – May 19    | 2 wks    |
| **4** | Public DEX Launch                 | May 19 – Jun 9    | 3 wks    |
| **5** | Cross-Chain                       | Jun 9 – Jun 23    | 2 wks    |
| **6** | Fee Calibration + Third-Party Apps| Jun 23 – Jun 30   | 1 wk     |
| **7** | Progressive Decentralisation      | Jun 30 – Jul 7    | 1 wk     |

---

## Phase 0 — Build + Local Testnet  *(Dec '25 – Apr 14)*

Pivoted from a DEX aggregator (miners as solvers) to a generic intent
execution platform. Five months of prior work informed the new architecture.
The DEX aggregator became the first **app on the platform**, not the
platform itself.

**What was built**

- **Intent platform** — `AppIntentBase` contracts, dual scoring (JS +
  Solidity), EIP-712 user consent, ephemeral proxy execution.
- **Miner pipeline** — AI agents write solver code, git/API submission,
  Docker-sandboxed benchmarks, champion lifecycle.
- **Validator consensus** — independent Anvil-fork simulation, N-of-M
  quorum, leader failover.
- **Platform fees** — WETH collection at the contract layer on every
  intent execution.
- **Frontend + MCP** — swap UI, app marketplace, order tracking, 35-tool
  MCP server for agent access.
- **Tested end-to-end** — same-chain swaps on Ethereum and Base against
  live DEX pools.

`Ethereum` `Base` `Intent Platform` `Consensus` `Miner Pipeline` `Platform Fees` `Swap Frontend` `MCP Server`

---

## Phase 1 — Closed Beta on Mainnet  *(Apr 14 – Apr 28)*

Same-chain swaps on Ethereum, Base, and Bittensor EVM. Team-operated
validators and miners, **not yet connected to Bittensor**. Tests:
leader changes, champion elections, solver sandbox, relayer execution,
submission pipeline.

`Ethereum` `Base` `Bittensor EVM` `Team Only` `Same-Chain Only`

---

## Phase 2 — Code Release + Onboarding  *(Apr 28 – May 5)*

Open-source the validator, miner, and solver SDK. Security audit of
contracts, validator, and SDK. Miners use frontier AI models to write
solver code. First external participants onboard and sync.

`Open Source` `Solver SDK` `Security Audit` `Onboarding`

---

## Phase 3 — Subnet Goes Live  *(May 5 – May 19)*

Connected to Bittensor. Validators and miners register on Subnet 112,
weight setting begins. Bootstrap test miners retire — independent miners
compete for champion. **Miner emissions are gated to 5% at Alpha launch
and ramp as the network proves stable** (champion-takes-all, dethrone
margin `0.005`).

`Weight Setting` `5% Emission Ramp` `Independent Miners` `Independent Validators`

---

## Phase 4 — Public DEX Launch  *(May 19 – Jun 9)*

Anyone can swap on Ethereum, Base, and Bittensor EVM. Same-chain only —
no cross-chain bridging yet. App deployment is still restricted to the
Minotaur team.

`Ethereum` `Base` `Bittensor EVM` `Public Users` `Agent API`

---

## Phase 5 — Cross-Chain  *(Jun 9 – Jun 23)*

Bridge integration, escrow system, multi-leg intent execution.
Post-bridge validation and rollback hardening. Bridge adapter
architecture is provider-agnostic.

`Bridge Integration` `Escrow` `Multi-Leg Execution`

---

## Phase 6 — Fee Calibration + Third-Party Apps  *(Jun 23 – Jun 30)*

Platform fee infrastructure already exists (WETH collection at intent
layer). Remaining work: perpetual-intent pricing, app deployment fees,
fee calibration based on usage data, treasury auto-buy (WETH → TAO →
stake). Third-party developers can deploy their own Apps. Developer
documentation for Solidity + JS scoring.

`Fee Calibration` `Treasury Auto-Buy` `Third-Party Apps` `Developer Docs`

---

## Phase 7 — Progressive Decentralisation  *(Jun 30 – Jul 7)*

Begin decentralising validator operations. Leader is the highest-stake
validator. Independent N-of-M quorum across external validators.

`Stake-Based Leader` `Independent Quorum`

---

*Minotaur — Bittensor Subnet 112*
