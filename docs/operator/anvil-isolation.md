# Anvil isolation — operator runbook

## What this is

Each Minotaur validator runs three Anvil forks (eth / base / btevm). The forks
live on the `minotaur` Docker network **only** — the validator / API / simulator
reach them there. They are **deliberately NOT on the sealed `benchmark-sandbox`
network** (PR-7 / audit finding C4). Solver containers spawned during reactive
champion benchmarking run on `benchmark-sandbox` and can reach **only the
read-only block-pin proxy (`172.30.0.5`)** for price discovery — never a raw
anvil RPC — and cannot egress to the internet.

Why the split: Anvil's `anvil_*` / `hardhat_*` / `evm_*` JSON-RPC namespaces
(set-storage, set-balance, impersonate, snapshot, etc.) are
**unauthenticated by design** and Foundry has no flag to disable them. Any
container that could reach the anvil RPC could mutate fork state — so no
untrusted container is allowed to reach it. The block-pin proxy serves only
deterministic read methods and refuses the `anvil_*` / `hardhat_*` / `evm_*`
cheat-code namespace outright.

## What we do about it (defense-in-depth)

**Primary containment is the network layer** (PR-7 / audit finding C4): because
the anvil forks are off `benchmark-sandbox`, an untrusted benchmark solver can
dial only the read-only block-pin proxy (`172.30.0.5`), which refuses the
cheat-code namespace. A malicious solver therefore has no path to a raw anvil RPC.

> ⚠️ Do **not** re-attach the anvil forks to `benchmark-sandbox`. That would put a
> raw, unauthenticated anvil RPC back in reach of untrusted solver containers and
> re-open the closed cheat-code vulnerability.

As a **backstop** for an in-process failed revert (not for an external attacker,
which the network layer already cuts off), the simulator keeps an
application-layer boundary in `minotaur_subnet/simulator/anvil_simulator.py`:

1. **Baseline snapshot** at simulator init (immediately after first
   connect). Recovery anchor if any later revert fails.
2. **Per-simulation snapshot + revert** wraps every `simulate()` call.
   The snapshot is taken at the top, reverted in the `finally` — no
   state can cross simulation boundaries.
3. **`_reset_fork`** either re-forks at upstream head (Base, BT EVM) or,
   on no-upstream chains (local-testnet 31337), reverts to the baseline
   and takes a fresh one. Anvil consumes snapshot IDs on revert.
4. **Baseline-alive probe**: once every 100 simulations (configurable
   via `BASELINE_PROBE_EVERY` in the module), read a known-stable
   storage slot. On mismatch, force a re-fork (upstream available) or
   raise `SimulatorStateError` (no upstream — operator must recycle).

## How to tell if a fork has been poisoned

Compare a known-canonical read against the upstream RPC:

```bash
# WETH total supply on Base — compare anvil vs upstream
cast call 0x4200000000000000000000000000000000000006 \
  "totalSupply()(uint256)" --rpc-url http://localhost:8546
cast call 0x4200000000000000000000000000000000000006 \
  "totalSupply()(uint256)" --rpc-url "$BASE_UPSTREAM_RPC_URL"
```

If the validator logs include `SimulatorStateError`, `Refusing to
simulate on poisoned fork`, or `Baseline probe mismatch`, the simulator
has already detected mutation and is failing closed.

## Manual force-recycle

```bash
docker compose restart anvil-eth anvil-base anvil-btevm
```

This drops every in-flight simulation. Only do it if poisoning is
genuinely suspected — the validator daemon will fail open consensus
proposals until anvils are healthy again (typically ~30-60 s).

## Recommended cron

The existing every-6h disk-bloat-recycle cron
(`/etc/cron.d/minotaur-anvil-recycle`) already restarts the anvils to
keep overlay growth under control. That cron also clears any state
poisoning. For belt-and-suspenders, add a weekly forced restart that
runs even if disk is fine:

```cron
# /etc/cron.d/minotaur-anvil-weekly-isolation
# Sundays 04:00 UTC — extra anvil recycle for isolation hygiene
# Replace <your-compose-dir> with the absolute path holding platform/validator/docker-compose.yml
0 4 * * 0 root cd <your-compose-dir> && docker compose restart anvil-eth anvil-base anvil-btevm
```

A future PR may spin per-benchmark ephemeral anvil containers (true
isolation, no shared state at all), but the cold-start cost (~30 s per
proposal) doesn't pay back for the current threat model.
