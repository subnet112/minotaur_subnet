# Champion merge — fork-PR redesign

How a miner-submitted solver becomes the champion on `subnet112/minotaur-solver`'s
`main` (→ `:latest` → fleet), safely, when the miner opens the PR **from their own
fork**. Adoption is currently FROZEN (`DISABLE_CHAMPION_ADOPTION=1`); everything
here is inert until activation.

## The problem

Miners open fork PRs on `minotaur-solver` and notify the leader with
`{pr_number, head_sha}` (`api/routes/submissions/github_pr.py::resolve_pr`). The
old `champion-merge.yml` only fired on `champion/*` branches, so it **never ran on
a miner's fork PR** and never merged it. A design that gated the merge on a GitHub
**required status check** is unsafe for fork PRs: under `pull_request` a fork runs
its **own copy** of the workflow, and required checks match **purely by name**, so a
malicious fork can emit a no-op `verify-champion-cert` that satisfies the gate with
zero on-chain verification. (Verified by adversarial review — see "Adversarial
findings".)

## The architecture

**Merge authority lives in the leader's trusted process, not in CI.**

1. **Leader-authority merge gate** (`relayer/solver_repo.py::merge_miner_pr_when_certified`, this PR):
   the leader, after attesting the cert on-chain, re-resolves the live PR head SHA
   (`resolve_pr`, TOCTOU), **refuses any PR whose diff touches `.github/**`**
   (CI-disarm guard), reads `ChampionRegistry` directly over web3
   (`getLatestChampion()` then `getChampion(round)`), and asserts
   `exists && commitHash == keccak(utf8(lowercase head_sha)) && approvalCount >= getQuorumRequired()`.
   Only then does it `PUT …/merge` with `merge_method=squash` **pinned to `sha=<head>`**
   so GitHub rejects on any head drift. It **never polls a GitHub check.**

2. **`champion-merge.yml` is advisory** (reference: `champion-merge.yml.reference`).
   `pull_request_target` (pins the workflow to the base branch so a fork can't
   substitute it), **never** `actions/checkout`s fork HEAD, read-only token, runs
   only `cast` on-chain reads against the authoritative event `head.sha`. It is
   visibility / fail-closed UX only — the leader ignores it.

3. **Non-admin token** (`assert_solver_repo_token_not_admin`, this PR): an admin
   token bypasses the `protect-main` ruleset *and* can edit/delete it, so the
   leader's `SOLVER_REPO_PR_TOKEN` must be a non-admin (write-role) fine-grained PAT
   scoped to the one repo (`Contents:write` + `Pull requests:write`). Startup
   **hard-fails** if the resolved token is admin (warns while frozen so the leader
   stays bootable before the PAT is provisioned; `ALLOW_ADMIN_SOLVER_REPO_TOKEN`
   escape hatch).

4. **Deploy-time provenance** is the backstop against a compromised leader / config
   breach: the fleet runs the certified **image digest** (`candidateImageId`,
   content-addressed transport), pulled by digest and self-verified, so the git SHA
   on `main` is provenance-only and the squash-vs-certified-SHA divergence is moot.
   A `docker-publish.yml` gate (build `push:false` → read `ChampionRegistry` →
   only push `:latest` if the built digest is a certified `candidateImageId`; always
   publish immutable `:sha-<short>` for audit) closes the
   `docker-publish push:[main] → :latest → GENESIS_SOLVER_IMAGE` alternate path.

## What this PR ships (inert under the freeze)

- `relayer/solver_repo.py`: ABI `getLatestChampion`/`getQuorumRequired`,
  `merge_miner_pr_when_certified`, `_onchain_cert_binds`, `_pr_touches_ci`,
  `_read_champion_registry`, `assert_solver_repo_token_not_admin`; rewired
  `on_champion_adopted_pr` (drops the legacy `create_champion_pr` parallel path).
- `api/startup.py`: gated startup token-admin assertion.
- `tests/unit/test_leader_merge_gate.py`: 15 tests.
- `docs/champion-merge/champion-merge.yml.reference`: the advisory workflow to copy
  to the solver repo at activation (needs a workflow-scoped push).

## Adversarial findings

A two-round adversarial review (design → attack → harden → re-attack) confirmed the
**miner-attack surface is closed** by this architecture: fork check-name spoofing,
the compromised leader-*process* token, and the on-chain verification path all fail
to land uncertified code. The remaining landable attacks are **not miner attacks**:

- **Compromised human admin / org owner** (`stalkervmr` is god over their own repo):
  can edit the ruleset / push directly. Mitigation is operational — demote the
  day-to-day identity, gate the org owner behind break-glass. *Decision required.*
- **Squash-SHA divergence / alternate fleet paths**: closed only once the
  **content-addressed digest transport** is active (fleet runs the certified digest)
  + the `docker-publish` provenance gate. *Cross-cutting with the P1 work.*
- **RPC trust/liveness**: the on-chain reads trust a public BT EVM RPC; pin/verify.

## Activation checklist (before flipping `DISABLE_CHAMPION_ADOPTION=0`)

1. Copy `champion-merge.yml.reference` → solver repo `.github/workflows/champion-merge.yml`
   (needs a workflow-scoped token or SSH push) + add `.github/CODEOWNERS` locking
   `.github/` and `Dockerfile`.
2. Provision `SOLVER_REPO_PR_TOKEN` = a non-admin (write-role) machine-account
   fine-grained PAT on the leader. Verify
   `gh api repos/subnet112/minotaur-solver/collaborators/<bot>/permission` == `write`.
3. Demote the human admin to non-admin for day-to-day; gate the org owner behind
   break-glass.
4. PUT the hardened `protect-main` ruleset (`bypass_actors:[]`,
   `required_status_checks` = `verify-champion-cert` advisory, `allowed_merge_methods:[squash]`,
   `require_code_owner_review:true`) **after** confirming the registered check-run
   name; back up first. Stand up a cron that pages on drift of
   `{enforcement==active, bypass_actors==0, required check present}`.
5. Add the `docker-publish.yml` provenance gate; keep `:sha-<short>` always-published.
6. Prove the content-addressed digest transport on a non-shared-volume 3-validator
   testnet (the load-bearing deploy backstop).
7. End-to-end: certify a test champion via quorum, confirm
   `getLatestChampion().commitHash == keccak(head sha)`, watch the leader gate
   squash-merge, the advisory check go green, and the fleet pull the certified digest.
