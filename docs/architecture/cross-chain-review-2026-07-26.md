# Cross-Chain Phase 1 — Post-Merge Review (2026-07-26)

**Scope reviewed:** PRs #1103 (Across + unified quote + solver `revert_legs`),
#1105 (CCTP adapter), #1112 (plan-set signature plumbing), #1113
(revert-or-refresh recovery), #1117 (contracts submodule → `3d0f75d`), #1118
(CCTP mint self-relay + SpokePool verification) — plus the live leader state
after `CROSS_CHAIN_ENABLED=1`.

**Verdict:** the shipped code matches its design docs and its flags, and the
on-chain half is genuinely ready. Two structural gaps stood between "armed"
and "a cross-chain order can complete", both fixed in this PR. The remaining
open items are listed in §7 with the reason each was left out.

---

## 1. Verified live state

Checked against the running leader (`34.203.136.172`), not inferred:

| Check | Result |
|---|---|
| Flags in `.env.production` **and** in the api/relayer/validator containers | `CROSS_CHAIN_ENABLED=1`, `CROSS_CHAIN_USER_DECISION=1`, `CCTP_ENABLED=1` |
| Leader image | `7cd9190` = develop tip; `plan_set.py`, `across.py`, `cctp.py`, `AWAITING_USER_DECISION`, `executeLegSigned` ABI all present |
| Adapters registered at boot | mock, tensorplex, hyperlane, **across**, **cctp** |
| New routes | `/orders/{id}/plan-set`, `/recovery`, `/decision` served (404 is "order not found", not "route missing") |
| App record | repointed to the new contracts on both chains, status `solved` |
| ETH `0xcD42Cf6F…7A52` runtime | `executeLegSigned` ✓, `planSetSignatureRequired` ✓ → reads **false** |
| Base `0xE0D97941…73BF` runtime | `executeLegSigned` ✓, `planSetSignatureRequired` ✓ → reads **false** |
| Live bridge quotes from the leader | Across WETH 2.78 bps; USDC Base→ETH: Across 1.45 bps vs CCTP 1.00 bps |
| Cross-chain orders processed so far | none |

`planSetSignatureRequired = false` is the correct setting today and must stay
false until the pipeline actually emits signatures — see §3.

**Rail selection.** `BridgeRegistry.best_quote` ranks purely on estimated
output, so CCTP's flat 1.00 bps wins **every** USDC route by 0.03–0.45 bps.
Enabling `CCTP_ENABLED` therefore makes CCTP — the rail whose delivery depends
on our own relayer rather than a permissionless network — the default for
USDC. That is a deliberate choice worth restating; #1118 made it survivable by
self-relaying the mint, and `cctp_enabled()`'s docstring now says so plainly.

---

## 2. Fixed — the bridge delivered where nothing could spend it

`compiler.py` pinned the bridge recipient to `user_address`, and
`across.py` used that one address as **both** the destination recipient and
the origin-refund depositor. Meanwhile `BridgeTracker` calls `escrowDeposit`,
which requires:

```solidity
require(IERC20(token).balanceOf(address(this)) >= amount,
        "Insufficient token balance for escrow");   // AppIntentBaseV2.sol:555
```

`address(this)` is the App. So the hop delivered into the user's wallet, the
App's balance was unchanged, `escrowDeposit` reverted, no escrow was recorded,
and the destination leg had nothing to spend. Funds stayed safe (the user's
own wallet is a terminal safe state) but the intent could never complete — and
the recovery flow's promise that "the asset is in escrow, `escrowRefund` is
the backstop" was false for that path.

This predates the Phase 1 PRs (it is in the initial public release,
`73b2d7e`); the Across rail simply inherited it and made it reachable.

**Fix.** Split the two addresses, which want different owners on different
chains:

- **Destination recipient** → the App on the destination chain, so
  `escrowDeposit` can gate the funds and `escrowRefund` (callable by
  `dep.user`, i.e. the user, after the deadline) can return them.
- **Origin refund target** → still the user's own wallet. An expiry refund
  into the *destination* App would be stranded on the wrong chain.

`BridgeAdapter.build_bridge_interactions(quote, recipient, refund_recipient)`
carries the split; Across encodes them as `recipient` and `depositor`; CCTP
pins `recipient` as `mintRecipient` and has no refund path; Hyperlane and
Tensorplex ignore the second argument. The reverse-bridge rollback leg keeps
the user for both — it is the terminal step of a revert, and the goal is the
user's own wallet.

`OrderProcessor._resolve_app_addresses` builds the per-chain map and **fails
closed**: a bridge into a chain where the App isn't deployed (or isn't
order-ready) is rejected at compile time rather than moving funds somewhere
the plan can't continue from. The compiler itself keeps the old fallback for
callers that pass no map, so quote-time dry-compiles and existing tests are
unaffected.

> **Left open (contract-level):** the App's fee float and escrowed balances
> share one `balanceOf`. `escrowDeposit` only checks a floor, so in principle
> a deposit could be satisfied by float rather than by bridged funds. Today
> the tracker only calls it after the bridge reports delivery of exactly
> `outputAmount`, so it is not reachable — but a `totalEscrowed` accumulator
> in `AppIntentBaseV2` would make it structurally impossible. Tracked for the
> contracts repo.

---

## 3. Fixed — the plan-set signature could never be attached

#1112 built the whole plan-set path correctly: the typehash and chain-agnostic
domain mirror `EIP712Verifier.sol` exactly, `build_leg_execution_plan` is the
single canonical constructor that keeps hash-time and submit-time plans
byte-identical, and the relayer branches to `executeLegSigned` when a
signature is present.

Nothing could ever reach that branch. The plan set is computed inside
`compiler.compile()`, and `order_processor` hands the compiled plan straight
to the orchestrator on the next statement — there is no state in which an
order waits. `POST /orders/{id}/plan-set-signature` 409s until
`params["plan_set"]` exists, which is microseconds before execution begins.
The quote payload doesn't carry a plan set either (and can't meaningfully: the
digest binds `orderId`, and the leg hashes bind live bridge calldata that only
exists at order time). So `plan_set_signature` was always absent, every leg
went out under validator quorum alone, and `planSetSignatureRequired` could
never be armed.

**Fix.** A wait-state, gated by `CROSS_CHAIN_REQUIRE_PLAN_SET_SIG` (default
OFF, so current behaviour is unchanged):

1. After compilation, a multi-leg order with a plan set and no signature parks
   in `AWAITING_PLAN_SET_SIGNATURE`. Nothing has executed; no funds have moved.
2. The client fetches `GET /orders/{id}/plan-set`, signs
   `PlanSetApproval(orderId, planSetHash)` with one wallet prompt covering
   every chain, and POSTs it back.
3. The attach endpoint verifies the signature (as before) and kicks off
   orchestration in the background — leg execution takes minutes, far longer
   than an HTTP request should hold, so the client polls `GET /orders/{id}`.
4. An order the user never signs simply expires at its own deadline.

The compiled plan is now **persisted onto the order** before parking, so the
resume survives a restart between compile and signature — a wallet prompt can
take as long as it takes. That persist also fixes a separate latent bug:
`update_order(plan=...)` previously ran *before* compilation, so the stored
plan never carried `multi_leg_plan` / `cross_chain` / `plan_set`, which is why
`GET /orders/{id}/bridge` always answered `"cross_chain": false`.

**Arming order matters:** turn this flag on and soak it *before* arming the
contract's `planSetSignatureRequired`. Reversed, the contract rejects every
leg the pipeline submits, because none carry a signature.

### 3a. Both parked states now survive a restart

Neither parked state could outlive the process. The plan-set resume runs as a
background task; the decision-window auto-revert runs as a watcher task. A
restart killed both — and worse, `load_open_orders` only restores
`status="open"`, so a parked order wasn't even reloaded into the in-memory
OrderBook. Since every order endpoint keys on `orderbook.get`, a restart made
parked orders **unreachable**, not merely un-resumed: re-POSTing the signature
would have 404'd.

`MultiLegOrchestrator.recover_parked_orders()`, called from `BlockLoop.run_loop`
at boot, reloads both parked statuses with their params and compiled plan, then:

- `AWAITING_PLAN_SET_SIGNATURE` **with** a signature → resume orchestration
  (the signature landed, the process died before the legs ran);
- **without** one → stay parked; the user hasn't signed and nothing executed;
- `AWAITING_USER_DECISION` unresolved → re-arm the watcher on its **original**
  deadline, so an expired window fires promptly instead of never.

The `escrowRefund` timelock stays the unconditional backstop underneath all of
it. Docstrings that claimed the watcher "does not survive restarts" are updated.

---

## 4. Fixed — a failed bridge hop bypassed the recovery flow

#1113 routed *destination-leg* failures into `AWAITING_USER_DECISION`, but the
bridge hop failing — `BridgeStatusEnum.FAILED` (an expired Across deposit) and
the poll timeout — still called `_mark_bridge_failed` directly. That is row 2
of the design's own failure matrix, the single most likely cross-chain
failure, dead-ending in a terminal state.

**Fix.** Both paths now go through `_fail_bridge`, and what it offers depends
on what the **rail** does with undelivered funds — declared on the adapter as
`REFUNDS_ON_ORIGIN`, defaulting to `False` so we never promise a refund we
can't verify:

- **Refundable (Across, `True`).** The deposit returns to the depositor — the
  user's own wallet — on the origin chain. Nothing to revert (the reverse-bridge
  legs execute on the destination, where funds never landed), so the order
  parks with **`refresh` as the only option**. `resolve_user_decision` rejects
  an action that wasn't offered, and a window that expires with no revert
  option settles into `BRIDGE_FAILED` rather than attempting a revert that
  would fail.
- **Committed (CCTP, Hyperlane, `False`).** A CCTP burn is irreversible and
  *always* mints; there is no origin refund and nothing to re-quote. Parking
  such an order with "refresh" and refund wording would be actively false. It
  records an accurate terminal state and raises an ops escalation instead —
  the mint stays permissionlessly completable.

For a committed rail the poll timeout also no longer **drops** the transfer:
the source funds are already gone and only delivery is outstanding, so
abandoning the entry would abandon the mint self-relay that still has to fire.
It keeps polling to a hard ceiling (`COMMITTED_MAX_POLL_FACTOR = 10`, ≈20h at
the 60s interval) behind a one-shot `logger.error` escalation, and only then
gives up. A rare edge in practice — Iris attests in minutes against a 2h
`max_polls` — but the semantics are now per-rail rather than Across-shaped.

---

## 5. Fixed — the cross-chain tests ran in no CI lane

All five suites added by #1103/#1105/#1112/#1113 carry
`pytestmark = pytest.mark.cross_chain`. `make test-unit` excludes that marker;
`make test-cross-chain` listed three files by name, none of them the new ones.
91 tests were invisible to CI from the day they landed.

**Fix.** `test-cross-chain` now selects by **marker** across `tests/unit/`
rather than by filename, so a new marked file can't be forgotten again. The
lane runs 135 tests (was 44).

---

## 6. Smaller corrections

- **`cross_chain_quote` said it floored the solver's estimate; it didn't.**
  The code takes the solver's declared output whenever it's positive. Rather
  than silently cap a number a destination-leg swap can legitimately exceed,
  the payload now labels its provenance —
  `estimated_output_source: "solver_declared" | "bridge_quote"` — and the
  docstring says outright that the destination leg is neither simulated nor
  bounded here. Per-leg simulation (design §10 step 2) remains the real fix.
- **`CCTP_MAX_FEE_HEADROOM_BPS` was a percentage.** `150` meant 1.5×, not
  0.15%. Renamed to `MAX_FEE_HEADROOM_PCT` / `CCTP_MAX_FEE_HEADROOM_PCT`; the
  old env name is still read so nothing breaks.
- **Stale strings.** The boot log claimed "mock + tensorplex + hyperlane"
  while registering five adapters — it now lists what it actually registered.
  `cctp_enabled()` still described the mint self-relay as a follow-up after
  #1118 shipped it.

---

## 7. Still open (deliberately not in this PR)

1. **Per-leg simulation in the unified quote** (design §10 step 2) — the
   destination leg's output is still solver-declared; now labelled, not fixed.
2. **`totalEscrowed` accounting in `AppIntentBaseV2`** (§2 note) — contracts
   repo, and not reachable through the current tracker sequencing.
3. **Across `message` handler.** Deposits still carry `message = b""`, so the
   destination leg is triggered by our polling rather than atomically inside
   the fill (`handleV3AcrossMessage`, design §10 step 3).
4. **Forge mirror of the `test_plan_set.py` digest vector** in
   `minotaur_contracts`, so both sides pin the same fixed digest.
5. **Frontend states.** `minotaur-apps`' swap UI `STALL_EXEMPT` list doesn't
   include `awaiting_user_decision` / `refreshing` / `awaiting_plan_set_signature`,
   so those render as falsely "stalled" now that the recovery flag is on.
6. **Solver support.** No solver emits `CrossChainPlan` yet, so none of this
   executes in production until one does — which is what makes landing these
   fixes now cheap.
