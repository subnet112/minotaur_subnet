# Champion merge — fork-PR redesign

How a miner-submitted solver becomes the champion on `subnet112/minotaur-solver`'s
`main` (→ `:latest` → fleet), safely, when the miner opens the PR **from their own
fork**. Adoption is **live** in production: `DISABLE_CHAMPION_ADOPTION` defaults OFF
(unset ⇒ adoption enabled) and is set in no deployment, so the flow below runs on
every certified round. The env gate remains available as a break-glass freeze
(`DISABLE_CHAMPION_ADOPTION=1`).

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

   **The merge is a hard precondition for adoption (always-on).** Despite the digest
   transport above, a champion is NOT adopted unless its on-chain attestation AND its
   PR squash-merge both succeed. At the activation commit boundary the leader runs the
   attestation + merge FIRST, and if either fails — no on-chain proof, or an
   unmergeable PR (e.g. the miner pushed the head past the certified commit, so the
   cert no longer binds the head SHA) — the round aborts (`merge_failed`), the champion
   is left unchanged, and the failure is mirrored onto the miner's PR via the same
   reject-feedback path used for benchmark rejections. This is **unconditional — there
   is no env var to disable it**. The certified image digest is still what the fleet
   RUNS at runtime; the gate just ensures a champion that can't be recorded on-chain +
   on `main` never earns weights. A failure **never closes a PR** — only a successful
   merge closes one — so the miner can fix the head and iterate on the same PR.

## What this PR ships (now live)

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

## Update — finalization runs on the RELAYER, not the leader

The merge gate above (`merge_miner_pr_when_certified` + `attest_champion_on_chain`)
is unchanged, but it **no longer runs in the leader's process — it runs in the
trusted relayer service.** In a decentralized fleet the leader rotates and may be a
3rd party we don't control (and that must NOT hold `RELAYER_PRIVATE_KEY` /
`SOLVER_REPO_TOKEN`). So:

- The leader certifies the round (quorum), then **POSTs the `ChampionCertificate`**
  to the relayer's `POST /v1/finalize-champion`
  (`relayer/solver_repo.py::on_champion_adopted_via_relayer`) and gates its local
  adoption on the boolean reply. The #326 adoption gate is byte-for-byte unchanged
  (`EpochManager.on_champion_adopted` is now the relayer client). **Fail-closed:**
  any error → False → round aborts `merge_failed` → never adopt on an unconfirmed merge.
- The relayer **does not trust the leader**: it independently re-verifies the
  validator quorum (`relayer/main.py::handle_finalize_champion`) — per-approval
  EIP-712 verify against the SAME champion domain separator validators sign with
  (`build_domain_separator(CHAMPION_CONSENSUS_CHAIN_ID, CHAMPION_REGISTRY_<chain>,
  "MinotaurChampionConsensus", "1")`), intersect with the on-chain `ValidatorRegistry`,
  require distinct authorized signers ≥ `quorum_required`, plus a leader-signed
  anti-spam wrapper — then attests + squash-merges with ITS OWN keys. On-chain
  `certify()` remains the ultimate quorum authority.
- **PR feedback stays on the leader** (`on_champion_rejected_pr` /
  `on_champion_finalist_pr`: benchmark-result + error comments, image GC). **The
  leader KEEPS its `SOLVER_REPO_TOKEN`** for those; only attest+merge moved.
- Wiring (`api/startup.py`): `_champion_merge_fn = on_champion_adopted_via_relayer`
  when `RELAYER_URL` is set, else the legacy in-process `on_champion_adopted_pr`
  (local testnet without a relayer).

### Deploy checklist (relayer finalization)

Apply in `platform/production/docker-compose.production.yml` (or equivalent env):

1. **Relayer service env — ADD:**
   - `SOLVER_REPO_TOKEN` — write-role PAT, for the squash-merge. *(Also stays on the leader for PR feedback.)*
   - `SOLVER_REPO_URL` (+ `SOLVER_REPO_PATH` if used) — the merge-target repo.
   - `CHAMPION_REGISTRY_964` — attest tx + the EIP-712 domain separator.
   - `CHAMPION_CONSENSUS_CHAIN_ID=964` — domain separator (defaults to 964; set explicitly to match the leader).
   - `VALIDATOR_REGISTRY_964` — so the relayer can read the authorized-validator set for the quorum check.
   - (Already on the relayer: `RELAYER_PRIVATE_KEY`, `BITTENSOR_EVM_RPC_URL`.)
2. **Leader (api) env:** keep `RELAYER_URL` (already set for order relay) + `VALIDATOR_PRIVATE_KEY` (signs the wrapper). **Do NOT remove `SOLVER_REPO_TOKEN` from the leader.**
3. **Deploy api + relayer together** (same image): the leader only delegates when `RELAYER_URL` is set and fail-closes if the relayer lacks `/v1/finalize-champion`, so the relayer must have the endpoint before/with the leader cutover (else adoptions safely abort `merge_failed`).
4. **Verify after deploy** on the next certified round: leader logs `Champion finalization: delegated to relayer at <url>`; relayer logs `finalize-champion accepted (… signers=N/Q) — attesting + merging` then `merge gate: PR #N squash-merged`; `ChampionRegistry.getLatestChampion()` binds the certified commit; the miner's PR shows MERGED.

## Activation checklist (historical — adoption is now live)

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
