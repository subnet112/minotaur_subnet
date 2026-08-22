# Bittensor Alpha Intents

Two intents on Bittensor EVM (chain 964) that no existing solver handles yet. Both
route through `AlphaVault`, a contract that turns a Bittensor staking position into
an ordinary ERC-20 so the rest of DeFi can hold it.

| | address |
|---|---|
| `AlphaVault` | `0xc2bf4b789F89644E62D04dcBBF51a8cD60A9e692` |
| `AlphaYieldApp` | `0x5338Cb9A8f8e0bf9413dFd39408323516A57949D` |
| `wAlpha112` (ERC-20) | `0xea634A2E093bdB4FDEaB66ad17DD2A7327524eC6` |
| wTAO on Ethereum | `0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44` |

## Units, before anything else

Three different decimal conventions meet in these intents, and mixing them is the
most likely way to lose a round.

| quantity | decimals | note |
|---|---|---|
| wTAO on Ethereum | **9** | not 18 — it matches substrate rao 1:1 |
| native TAO on chain 964 | 18 | ordinary wei |
| `IStakingV2` `amountRao` | 9 | the precompile's own unit |
| alpha (any subnet) | 9 | raw stake units |
| `wAlpha112` | 18 | see below |

`wAlpha112` lands on 18 decimals because the vault's first deposit mints
`VIRTUAL_SHARES` (1e9) shares per alpha as an inflation-attack offset, and alpha is
9-decimal. `shares == alpha * 1e9` on an empty market is expected, not a bug.

`purchaseWrapped` **rejects a `msg.value` that is not a whole number of rao** —
`UnalignedAmount`. Any value you send must be divisible by `1e9`.

---

# Intent 1 — `optimizeYield`

**App:** `AlphaYieldApp` · **selector:** `0x97bd061f` · **perpetual**

A pooled wrapped-alpha position must be delegated to some validator, and which one
changes what holders earn. That choice is competed here.

## The plan is data, not code

This is the one way this app differs from every other on the subnet: **`plan.calls`
is ignored**. Send an empty array. Anything you put there is dead weight that only
costs you gas.

Other apps execute your plan through an ephemeral proxy, moving funds the user just
supplied. A plan here would move a *pooled, custodied* position belonging to every
wAlpha holder, so the contract performs the move itself and your plan carries only a
recommendation.

```
order.intentParams = abi.encode(uint256 netuid)
plan.metadata      = abi.encode(bytes32 hotkey, uint16 uid)
plan.calls         = []
```

## Everything you need to decide, in one call

```solidity
AlphaYieldApp.survey(uint256 netuid) returns (
    bytes32[] hotkeys,   // the allowlist — you may not name anything else
    uint16[]  uids,      // each hotkey's CURRENT metagraph uid
    uint256[] rates,     // dilution-aware, see below
    uint256   readyAt    // cooldown; a move before this reverts
)
```

No indexer and no privileged data. The winning answer is the candidate with the
highest `rates` entry.

## How you are scored

Absolute, not relative to other solvers. There is a knowable best answer every
block, so finding it scores 1.0 whether or not anyone else competed that round.

```
score = (rate(chosen) - rate(worst)) / (rate(best) - rate(worst))
```

normalised across the allowlist: the worst eligible pick scores 0, the best 1.

### `rate` counts your own position in the denominator

```
rate(uid) = dividends(uid) / (stake(uid) + position moving in)
```

**Do not use `dividends / stake`.** That is a *marginal* rate — what the next
infinitesimal unit earns — and it is actively misleading for placing a large
position. Measured on SN112: uid 230 holds 8,659 dividends on 8,800,003 stake and
scores ~12,500× better marginally. Move the vault's position onto it and that
position earns **59% less** than uid 0. `survey` already returns the correct
figure; use it rather than recomputing from the metagraph.

The incumbent's reported stake already contains the vault's position, so it is not
added twice.

One trap while the market is new: with an empty vault the position is zero, so
`survey` returns the *marginal* rates — the ordering it implies can invert once
someone deposits. The intent reverts `NothingAtStake` in that state anyway, so it
costs you nothing, but do not build a model on rates read from an empty market.

## Four ways to score zero

| condition | result |
|---|---|
| hotkey not on the allowlist | reverts `NotAllowlisted` |
| fewer than 2 candidates | reverts `NothingToOptimize` — nothing to grade |
| vault holds no position | reverts `NothingAtStake` — a choice decides nothing |
| no allowlisted validator earns | reverts `NoScorableYield` |

A plan scoring below `scoreThreshold` (5000 BPS) does **not** move the stake. That
is deliberate: `scoreIntent` applies no threshold of its own, so the app refuses to
act on a plan it hasn't first judged good enough.

## What the scorer cannot see

Delegate take. No precompile exposes it, so every `rate` here is pre-take and a
validator charging 100% scores identically to one charging nothing. The allowlist is
what contains that — take is vetted off-chain before a hotkey becomes eligible.
Don't try to price it; there is no honest on-chain source.

---

# Intent 2 — cross-chain, Ethereum → wAlpha

**App:** DEX Aggregator V2 · **intent:** `execute` · **route:** wTAO on chain 1 →
`wAlpha112` on chain 964

## Deliver the wrapped token, not raw alpha

`AlphaVault` offers two modes. Only one of them can be credited.

| mode | what the buyer gets | scoreable |
|---|---|---|
| `purchaseWrapped` | `wAlpha112`, an ERC-20 | **yes** |
| `purchase` | real alpha on their own substrate coldkey | **no** |

Delivery is measured as ERC-20 transfers of the requested token to the receiver.
`purchase` delivers via `transferStake` to a substrate account — not an EVM token
transfer — so it measures nothing and scores zero **even when it executes
perfectly**. Route to `purchaseWrapped`.

## Plan shape

Emit the solver shape; the compiler injects bridge calldata later, so your bridge
request carries no interactions of its own.

```python
plan.metadata["cross_chain_plan"] = {
    "legs": [
        {"chain_id": 1,   "interactions": [...]},   # source: acquire wTAO
        {"chain_id": 964, "interactions": [...]},   # destination: buy wAlpha
    ],
    "bridge_requests": [
        {
            "src_chain_id": 1,
            "dst_chain_id": 964,
            "token": "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44",
            "amount": <wTAO, 9-decimal>,
        },
    ],
}
```

A plan that does not declare cross-chain is the single most common way to score
zero on this demand — it reads back as `no_cross_chain_plan`.

## The bridge leg

Tensorplex, ~0.1% fee, ~30 min settlement. Bridging out of Ethereum is a **burn on
the wTAO token itself** — there is no bridge contract and no approval needed:

```solidity
wTAO.bridgeBack(uint256 amount, string ss58Destination)   // 0x2a383090
```

The destination is a **substrate ss58 string**, not an address. That is what makes a
964 destination work at all: Bittensor maps an EVM address to a substrate account by
`blake2_256("evm:" || address)`, and an H160's EVM balance *is* its mapped account's
balance. So bridging to the mapped ss58 of your destination H160 credits it native
TAO, spendable as `msg.value`.

The compiler builds this leg for you from the `bridge_requests` entry — you do not
hand-encode it. What arrives on 964 is **native TAO**, not a token.

A burn is irreversible and has no refund path. Size the leg accordingly.

## The destination leg

```solidity
AlphaVault.purchaseWrapped(uint256 netuid, address receiver, uint256 minSharesOut)
    external payable returns (uint256 shares)
```

Send the bridged TAO as `msg.value` (divisible by `1e9`), set `receiver` to the
order's receiver, and set `minSharesOut` as your slippage guard. The vault mints
`wAlpha112` to `receiver`, which is the transfer that gets measured.

Shares are minted against the **measured** alpha delta, so a pool move between quote
and execution cannot mint unbacked shares — but it does mean you receive fewer
shares, which `minSharesOut` should bound.

The market must be open for that netuid. Today only **netuid 112** is.

## Worked example — 7 wTAO

```
  7.000000000  wTAO on Ethereum          (9-dec: 7_000_000_000)
- 0.007        Tensorplex fee (10 bps)
= 6.993        TAO on 964                (18-dec: 6_993_000_000_000_000_000)
→ purchaseWrapped{value: 6_993_000_000_000_000_000}(112, receiver, minSharesOut)
→ wAlpha112 minted to receiver           (18-dec)
```

## Reference

- Contracts: [`subnet112/minotaur_contracts`](https://github.com/subnet112/minotaur_contracts) — `src/AlphaVault.sol`, `src/AlphaYieldApp.sol`
- Bridge adapter: `minotaur_subnet/bridge/tensorplex.py`
- Scoring module: `src/examples/alpha_yield_scoring.js`
