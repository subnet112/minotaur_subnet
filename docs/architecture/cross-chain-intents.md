# Cross-Chain Multi-Leg Intents — Design

**Status:** Draft — design approved direction, not yet scheduled
**Scope:** Base ↔ Ethereum mainnet
**Prerequisite flag:** `CROSS_CHAIN_ENABLED` (currently default OFF, `shared/feature_flags.py`)

---

## 1. Product model

A cross-chain intent is a **multi-leg intent**: multiple transactions, usually one
per chain, each carrying its own execution plan computed by the Solver Engine.

The user experience:

1. **One quote for the whole intent.** The quote contains the execution plan for
   every leg, the bridge route (protocol, fee, ETA), *and* the pre-agreed revert
   plan for each failure point.
2. **Every failure scenario has a known resolution.** If a bridge hop or a leg
   fails, the user chooses between:
   - **Revert** — execute the pre-agreed revert plan from the quote (e.g. swap
     back to the original token and bridge home), or
   - **Refresh** — request a fresh quote for the failed leg only (liquidity
     moved; re-price just that leg).
3. **On-chain funds safety equivalent to atomic intents.** Solvers are
   adversarial miners. At no point may a solver (or any single off-chain party)
   steal or strand user funds.

### The safety invariant, restated for multi-leg

True atomicity is impossible across chains. The cross-chain generalization of
our atomic guarantee is:

> At every moment, user funds are in exactly one of four states:
> 1. the user's wallet;
> 2. inside a transaction that reverts in full unless the app gained
>    ≥ `minAmountOut` (the existing `_handleIntent` invariant, per leg);
> 3. inside a bridge rail whose recipient is cryptographically pinned to a
>    protocol-owned escrow contract at source-commitment time;
> 4. in on-chain escrow the user can reclaim unilaterally after a deadline
>    (`escrowRefund`, permissionless for the depositor).
>
> A solver is thereby reduced to a permissionless keeper: it can cause delay,
> never loss.

---

## 2. Current state (what already exists)

A large fraction of this design is already in the tree, gated off. Inventory:

### Plan layer

| Concept | Where | Notes |
|---|---|---|
| Solver-facing cross-chain plan | `CrossChainPlan` — `shared/types.py:396` | `legs: list[ChainLeg]` + `bridge_requests`; solver declares "move asset X, chain A→B, min_output Y" and does **not** choose bridge protocol or calldata |
| Per-leg plan | `LegPlan` — `shared/types.py:218` | `leg_index`, `chain_id`, `interactions`, `depends_on`, `rollback_for` |
| Compiled plan | `MultiLegPlan` — `shared/types.py:268` | `forward_legs` + `rollback_legs` + `rollback_plan_hash` |
| Solver → platform handoff | `plan.metadata["cross_chain_plan"]` dict, parsed in `blockloop/order_processor.py:270-291` | All `from_dict`s use `.get()` + defaults → additive schema changes are non-breaking |
| Platform compiler (trust boundary) | `CrossChainCompiler` — `bridge/compiler.py:52` | Fetches real bridge quotes, injects bridge interactions, builds escrow params, generates rollback legs; **rejects bridge selectors inside solver legs** (`compiler.py:256-267`) |

### On-chain layer (AppIntentBaseV2 — canonical app is DexAggregatorAppV2)

| Concept | Where | Notes |
|---|---|---|
| Atomic min-out invariant | `executeIntent` gauntlet, `_handleIntent` override | Whole tx reverts unless app gained ≥ `minAmountOut`; `minAmountOut` pinned inside the user's EIP-712 signature |
| Per-leg execution | `executeLeg` | Same gauntlet per leg. **Currently submitted with `user_sig = b""`** — validator quorum only (`relayer/evm_relayer.py:364-371`) — see §5 |
| Escrow gate | `AppIntentBase(V2).sol` escrow section | Destination leg holding bridged funds cannot execute until validators quorum-sign `escrowRelease` |
| Timelock refund | `escrowRefund` | Permissionless for the original depositor after deadline — the unconditional backstop |
| Execution sandbox | `ExecutorProxy` (V2, EIP-1167 clones) | `require(msg.sender == app)` — V2 closed V1's permissionless-proxy hole; leftover dust/approvals not publicly sweepable |
| V2 fee settlement | app-held WETH float, `safeTransfer` from the app itself | No paymaster/allowance (documented `simulator/anvil_simulator.py:983-1011`); fee settles **per leg per chain** — see §7 failure matrix |

### Runtime layer

| Concept | Where |
|---|---|
| Leg orchestration + auto-rollback | `MultiLegOrchestrator` — `blockloop/multi_leg.py:358-418` |
| Bridge polling → escrow → dest leg | `BridgeTracker` — `relayer/bridge_tracker.py:235-365` |
| Phased cross-chain submit | `CrossChainOrchestrator` — `blockloop/cross_chain.py:49-186` |
| Multi-chain simulation | `MultiChainSimulator` — one Anvil fork per chain; `simulate_cross_chain()` runs each leg on its own fork, bridge hop mocked from adapter `mock_config()` (`anvil_simulator.py:1419,1524`) |
| Order status machine | `orderbook.py:33-39` — `BRIDGING`, `BRIDGE_FAILED`, `EXECUTING_LEG`, `ROLLING_BACK`, `ROLLED_BACK`, `PARTIAL_ROLLBACK` |
| Bridge adapter ABC + registry | `bridge/base.py:51` (`quote` / `build_bridge_interactions` / `check_status` / `mock_config`), `BridgeRegistry.best_quote()` (`bridge/registry.py`) |
| Validator-side verification | `bridge/verifier.py` — `verify_platform_compiled`, `verify_escrow_on_chain` |

### Existing adapters — none cover Base↔Ethereum

- `hyperlane.py` — real, near-complete, but hardcoded **Base↔Bittensor-EVM** warp routes only.
- `tensorplex.py` — TAO bridge; EVM→Bittensor direction raises `NotImplementedError`.
- `mock.py` — testing.

**Net: the missing pieces are the Base↔Ethereum value rail, the unified quote,
the user-choice recovery flow, and the user signature over the plan set.**

---

## 3. Gap analysis vs. the product model

1. **No unified quote.** The quote path cannot simulate cross-chain plans; it
   copies a best-effort estimate from solver metadata
   (`api/routes/orders.py:1296-1309`). No per-leg quote, no revert-plan quote.
   The building blocks (`simulate_cross_chain`, `BridgeRegistry` quotes) exist
   but are not composed into the quote endpoint.
2. **Recovery is automatic and platform-authored, not agreed-and-chosen.** The
   compiler auto-generates rollback legs as reverse-bridge transfers only
   (`compiler.py:182-204`) and the orchestrator fires them automatically. There
   is no `AWAITING_USER_DECISION` state, no user choice, no per-leg re-quote
   flow, and "swap back to original token" is not expressible.
3. **The user's signature does not cover legs or revert plans.** Legs execute
   with an empty user signature under validator quorum alone. "Pre-agreed
   revert plan" is therefore not cryptographically agreed (§5).
4. **Smaller gaps:** rollback legs can themselves fail → `PARTIAL_ROLLBACK`
   dead-end; leg *sequencing* is enforced off-chain (acceptable — escrow gate
   covers the dangerous ordering — but must be stated); `DexAggregatorAppV2`
   source lives in the app store (`solidity_code`, mutable via the wallet-signed
   `update_solidity` endpoint), not in git — see §8 audit items.

---

## 4. Value rail: Across first, CCTP v2 fast-follow

Selection constraint: bridged funds must land in a protocol-owned destination
contract with the recipient fixed at source-commitment time, with no
custodian-class trust party. `BridgeRegistry.best_quote()` already arbitrates
across adapters per route, so this is a two-adapter plan, not a single choice.

### Primary: Across Protocol (`AcrossAdapter`)

- **Covers both assets** (WETH + USDC, both directions Base↔Ethereum) — one
  adapter unblocks the feature.
- **Trust:** origin SpokePool escrow; permissionless relayers front destination
  liquidity from their own capital and are repaid via UMA optimistic settlement
  (bond + challenge window). No committee that can mint or steal. Fills must
  match deposit params exactly — a relayer cannot alter recipient or amount.
- **Latency:** fills in seconds to ~1 min. Fee ~4 bps (verify at integration).
- **Composability:** deposit carries a `message`; destination SpokePool calls
  `handleV3AcrossMessage` on our handler atomically within the fill → leg-2
  trigger and in-handler fallback live here.
- **Failure semantics:** unfilled by `fillDeadline` → automatic **origin-chain
  refund** (hours — quote this into the user's worst case). Fee-quoted-at-deposit
  can go unfilled in gas spikes; use Across's speed-up (fee bump) to unstick.
- **Ecosystem:** ERC-7683 co-author, production settler; cross-chain UniswapX
  made the same choice.

### Fast-follow: Circle CCTP v2 Fast Transfer (`CCTPAdapter`) — USDC legs

- Strictly dominates for USDC: 1–1.3 bps, sub-30s both directions, and the only
  added trust is Circle — already implied by holding USDC.
- `mintRecipient` is pinned in the burn message: nobody can redirect the mint.
  Mint to **our** destination escrow contract; run our own hook executor for
  leg-2. If the hook fails, the mint is unaffected — USDC sits in our escrow,
  which is exactly the state the revert-or-refresh decision needs.
- More integration work (attestation fetch + mint-and-execute executor), hence
  second. Note: if `maxFee` is below the live Fast fee, CCTP silently downgrades
  to Standard (~13–19 min) — quoting must handle this.

### Rejected for value

- **Hyperlane warp routes** — default ISM is an external N-of-M validator
  multisig with no fraud-proof/escrow backstop (custodian-class trust), and
  users would receive synthetic assets. Keep the adapter for
  Base↔Bittensor-EVM where it already operates; never Base↔Ethereum value.
- **Canonical Base bridge** — strongest trust but ~7-day Base→Ethereum
  withdrawals. Optional zero-added-trust rail for Ethereum→Base only (1–3 min).
- **CCIP** (10–20 min latency), **LayerZero/Stargate** (DVN committee trust,
  dominated for our pair), **Relay.link** (operator trust on refund path).

> **Strategic note:** Base announced (Feb 2026) a migration off the OP Stack
> onto its own stack, and Superchain interop is OP↔OP only. Do not architect
> against Superchain native interop; watch the migration for canonical-bridge
> and fault-proof continuity.

---

## 5. Signature model: user signs the plan set

Today the user's EIP-712 `IntentOrder` pins outcome params (`minAmountOut`,
receiver, deadline) and validator quorum picks the plan. For multi-leg, legs
execute with an empty user signature — meaning the recovery path is whatever
quorum signs at rollback time, not what the user accepted at quote time.

**Change:** the user's signature binds a commitment to the **full plan set** —
every forward-leg plan hash and every revert-leg plan hash (generalize
`MultiLegPlan.rollback_plan_hash`, which already exists for exactly this but
never reaches the contract). `executeLeg` verifies the leg being executed is a
member of the signed set.

Consequences:

- "Pre-agreed revert plan" becomes cryptographically true: a colluding quorum
  cannot substitute a different recovery path than the user accepted.
- A **refresh** naturally requires a fresh user signature for the replacement
  leg — which is the product UX anyway.
- Per-leg `minAmountOut` (each leg's `_handleIntent`) remains the hard safety
  floor; the plan-set signature adds path integrity on top.

This is a contract change to `AppIntentBaseV2` / the app's `executeLeg` path
plus relayer/encoder support. It is solver-neutral (solvers never handle user
signatures).

---

## 6. Recovery flow: revert or refresh

New order state: **`AWAITING_USER_DECISION`** — entered when a leg fails or a
bridge deposit expires while user funds sit in a safe state (origin refund
received, or asset in destination escrow).

- **Revert:** execute the pre-signed revert leg(s) from the quote. Each revert
  leg passes through `_handleIntent` with its own `minAmountOut` — a stale
  revert plan (liquidity moved) fails its min-out and falls back to refresh or
  timelock refund; it never executes at a worse price than agreed.
- **Refresh:** re-quote the failed leg only. **This needs no solver-side
  concept:** a leg refresh is framed as a new *atomic single-chain intent* on
  the destination chain — input = the escrowed asset, output = the user's
  target token. Current solvers handle it natively as an ordinary order. New
  user signature required (§5).
- **Decision deadline:** if the user does nothing, auto-execute the agreed
  revert plan before the escrow deadline; if that fails, `escrowRefund` after
  the deadline remains the permissionless backstop. Funds never depend on an
  absent user or live validators.
- **`PARTIAL_ROLLBACK` is no longer a dead-end:** it routes into the same
  decision state for the remaining funds.

---

## 7. Failure matrix

| Failure point | Where funds are | Resolution |
|---|---|---|
| Leg 1 reverts | User's wallet (atomic min-out revert, as today) | Nothing lost; retry/refresh whole intent |
| Bridge unfilled/slow (Across) | Origin SpokePool → auto-refund at `fillDeadline` | Revert plan on source chain, or re-quote; refund arrives regardless (hours) |
| Bridge slow (CCTP) | In-flight burn→mint; cannot be stranded | Wait; worst case Fast downgrades to Standard (~15 min) |
| Leg 2 reverts / slippage moved | Protocol escrow on destination chain (CCTP `mintRecipient` / Across handler recipient) | **User decision: revert (pre-signed, min-out protected) or refresh (new atomic intent over escrowed amount, new signature)** |
| Revert plan itself stale | Escrow | Revert leg fails its own min-out → refresh or timelock refund |
| User absent / validators offline | Escrow | Auto-revert before deadline; else permissionless `escrowRefund` after deadline |
| V2 app WETH fee-float empty on dest chain | Escrow (leg reverts at fee step) | Ops alert + refill; escrow gate + refund protect funds meanwhile. Fee must be quoted **per leg per chain**; float level is a monitored liveness dependency |

---

## 8. Solver compatibility (hard constraint)

Miners write the Solver Engine; we cannot modify it and must not break the
current solver state. Miners adapt on their own timeline via scoring pressure.

The interface boundary is deliberately thin — `plan.metadata["cross_chain_plan"]`
as a plain dict, all deserialization via `.get()` + defaults — so:

- **Solver-neutral changes (no solver impact):** unified quote (platform
  composition), plan-set signature (API/relayer/contract layer), CCTP/Across
  adapters (`BridgeRequest` already isolates solvers from bridge protocol),
  and the whole recovery flow (refresh = ordinary single-chain intent).
- **The one solver-facing change — additive and incentivized, never required:**
  optional `revert_legs` on `CrossChainPlan`. Absent → compiler's auto
  reverse-bridge rollback remains the fallback (current behavior, every
  existing solver stays valid). Present → richer revert ("swap back to original
  token") is used and quoted. Migration via benchmark scoring weight
  (scaffolding in `contracts/src/examples/cross_chain_scoring.js`), observe-only
  Phase 0 first, then let fork-and-improve pressure do the rest. No flag day.

Compatibility traps:

1. **Dual leg conventions.** Legacy `metadata["legs"]`
   (`partition_plan_by_leg`, `types.py:467-526`) coexists with
   `CrossChainPlan`. New code accepts both; do not consolidate here.
2. **Benchmark determinism.** New optional fields must be invisible to the
   single-chain scoring path — champion scores must be bit-identical
   before/after the schema lands. Run the before/after soak as part of the
   promotion checklist even though the change is "only additive."
3. **Compiler selector blacklist growth.** Adding CCTP/Across selectors to the
   bridge-selector reject list applies to cross-chain legs only — verify no
   live champion's *single-chain* routing legitimately touches an Across
   SpokePool before widening enforcement.

---

## 9. Message passing for non-bridging apps

**Do not use the value rails as a message bus.** Across messages ride only with
a token fill; CCTP hooks ride only with a USDC burn. Dust-value transfers as
message carriers inherit value-rail fees and failure semantics.

**Use the validator quorum — we already run a cross-chain attestation system.**
`escrowRelease` *is* one: validators observe a fact on chain A, quorum-sign an
EIP-712 attestation, a relayer submits to chain B, the contract verifies
against the `ValidatorRegistry` deployed there. Registries exist on both
Ethereum and Base; the relayer already spans both chains.

**Design:** generalize the pattern into a quorum-verified `MessageBox` contract
(or `receiveMessage` on `AppIntentBaseV2`): payload + source/destination
chain-ids + nonce + deadline under the signed hash (replay + ordering safety —
all patterns the EIP-712 layer already implements for orders).

**Why not Hyperlane for messages:** the validator quorum already gates every
execution (plan approvals, escrow releases) — using it for messages adds *zero
new trust parties*. Hyperlane's default ISM adds an external multisig users
currently never trust. If Hyperlane's transport/relayer plumbing is ever wanted
operationally, its ISM is pluggable — the validator set can *be* the ISM
signers, preserving the trust model.

**Caveats:**

1. **Quorum decentralization is the security bound — today effectively one
   operator** (leader supermajority; follower veto plane not quorum-ready).
   Messaging doesn't add trust beyond what's already load-bearing, and it
   strengthens automatically as the follower fleet does. State this honestly in
   user-facing docs.
2. **Cap message authority.** Receiving contracts treat a quorum message as a
   *trigger* subject to their own invariants — never unconditional authority to
   move user funds. Value stays bounded by min-out + timelock refunds.

The resulting split: **value moves over external rails chosen for refund
semantics; authority moves over our own quorum** — the same trust split the
atomic intent design already embodies.

---

## 10. Build plan (dependency order)

1. **Audit `DexAggregatorAppV2`.** Pull source from the app store record on the
   leader, verify against deployed bytecode, review the `_bridge` →
   `escrowDeposit` tie-in. Because app source is mutable post-deploy
   (`update_solidity`), pin the audited version's bytecode hash as a
   precondition for arming `CROSS_CHAIN_ENABLED`.
2. **Unified quote.** Compose `simulate_cross_chain()` + live `BridgeRegistry`
   quotes + revert-plan simulation into the quote endpoint: per-leg expected
   outputs, per-leg-per-chain fees, bridge fee/ETA, quoted revert outcomes,
   worst-case timelines (incl. Across refund hours).
3. **`AcrossAdapter`** implementing the existing `BridgeAdapter` ABC
   (quote / build_bridge_interactions / check_status / mock_config), plus the
   destination `handleV3AcrossMessage` handler contract.
4. **Plan-set user signature** (§5): contract change + encoder/relayer support;
   close the empty-user-sig hole on `executeLeg`.
5. **Recovery flow** (§6): `AWAITING_USER_DECISION` state, revert/refresh API
   endpoints, leg re-quote round, decision deadline → auto-revert → refund
   chain.
6. **Optional solver `revert_legs`** on `CrossChainPlan` + scoring weight,
   observe-only Phase 0.
7. **`CCTPAdapter`** (Fast Transfer, hook executor) — `best_quote()` starts
   routing USDC over CCTP automatically.
8. **Quorum `MessageBox`** (§9) — independent track; needed only when the first
   non-bridging cross-chain app needs it.

Each numbered item is independently shippable behind the existing flag; the
feature arms end-to-end after 5 (WETH+USDC via Across), with 6–8 as
improvements.

---

## 11. Open questions / unverified items

- Across refund SLA for expired deposits (hours-order inferred from bundle +
  UMA challenge mechanics; docs page unavailable) — measure at integration.
- Exact live fee bps for Across (secondary-source ~4 bps) and CCTP Fast (Circle
  claims 1–1.3 bps, sub-30s) — confirm via their quote APIs in the adapter.
- Base's post-OP-Stack migration proof system and canonical-bridge continuity.
- ERC-7683 adoption: cheap to emit their order structs (opaque `orderData` can
  carry our plan-set commitment) — decide whether solver-ecosystem interop is
  worth it at step 3 or later.
- Whether the `AWAITING_USER_DECISION` deadline should be user-configurable in
  the quote (longer window = later escrow deadline = longer worst-case refund).

## 12. The adoption incentive: destination delivery reaches the app's scorer (2026-07-27)

No solver emits a `CrossChainPlan` until doing so WINS benchmark cases, and
nothing in `minotaur_subnet` may know what a swap is — the platform measures,
the app prices. The mechanism is therefore split across that boundary:

**Platform (app-agnostic).** The benchmark already measures
`destination_delivered` / `destination_amount_source` (#1133, observe-only).
Two additions make it creditable:

1. *The measurement rides the sim into the scorer.* The same values persisted
   on the benchmark row are attached to the `SimulationResult` handed to
   `score_fn`, and `context.simulation` exposes them to the app's JS (decimal
   strings — wei above 2^53 must never round-trip through a JS Number). One
   computation feeds row and scorer; they cannot disagree. The fields are
   absent everywhere but the benchmark path, so any scorer that ignores them
   is bit-identical to before.
2. *The solver shape became observable.* A solver's `bridge_requests` carry no
   calldata, so its deposit could only measure as `"declared"` — its own
   number, inflatable, hence never creditable, hence NO gradient. The
   benchmark now synthesizes the same `transfer(_MOCK_BRIDGE_TARGET, amount)`
   the mocking path produces (one shared encoder, `mock_bridge_deposit`) and
   executes it in ONE simulation together with every preceding same-chain leg
   (`bridge_execution_plan`): a swap-then-bridge journey carries its own
   earnings to the deposit, and a declared amount the journey never earned
   reverts. Both plan shapes now measure `"simulated"` on the same terms.

**App (`dex_aggregator_scoring.js`, companion PR in minotaur-apps).** For an
intent that names `dest_chain_id` (new manifest param, `in_signature:false` so
the on-chain selector is untouched), the scorer redefines only
`metadata.raw_output` — the benchmark adoption signal, never read on the live
path: the measured destination delivery when provenance is `"simulated"`,
otherwise `"0"`, including for any plan that ignored the requested chain.
`score`/`valid` keep their exact single-chain semantics in every context, so
live per-leg scoring and the follower's re-score are untouched. On a
cross-chain corpus case, bridging-and-delivering now beats every same-chain
answer, and an unproven claim beats nothing. That is the entire miner
incentive.

**Arming ladder (order is load-bearing):**

1. Promote the platform exposure fleet-wide (`:stable`) — the app JS is
   fleet-synced but platform code is not; an app reading a field only the
   leader emits would split verdicts.
2. Prove the #1133 exit criterion with `tools/destination_delivery_replay.py`:
   same case file + same per-chain pins on leader and a follower must produce
   byte-identical `destination_delivered`, intra- and cross-node.
3. Update the app record (manifest + scorer) — one app-store mutation,
   propagated by app-sync; pack hash flips with the record, so this is its own
   step, never bundled.
4. Seed cross-chain cases into the corpus (hash-critical, its own promote) and
   drive live cross-chain `/quote` traffic so blind-spot capture keeps demand
   fresh.
5. Announce: point miners at the reference `_generate_cross_chain_plan` in
   `minotaur-solver` HEAD.
