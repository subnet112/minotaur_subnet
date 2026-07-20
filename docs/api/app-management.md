# App-Management API Reference

The API service (`:8080`, e.g. `https://api.minotaursubnet.com`) exposes the
create → validate → deploy → lifecycle surface for App Intents. **Deploy and
lifecycle management** use a **wallet-signature** auth model (a browser frontend
signs with MetaMask — no server secret). **Create and validate are admin-gated**
(`X-Admin-Key`) as of the 2026-07-18 security hardening (PR #933): both compile
and **execute the submitted App JS in the scoring sandbox**, which was the
credential-exfil vector that leaked `RELAYER_PRIVATE_KEY`, so untrusted-JS
execution is admin-only. Re-opening a hardened self-serve create path is a
follow-up.

This page documents the current surface. Runtime behavior notes live in the
[Code-Verified Runtime Guide](../code-verified-runtime.md); operator env defaults
live in [Network Reference](../operator/network-reference.md).

## Auth model

Three overlapping mechanisms, by endpoint class:

| Class | How to authorize |
|-------|------------------|
| **Create** (`POST /v1/apps/`) | **Admin-gated** (`X-Admin-Key`) — PR #933. Create runs a Forge/JS validation pass that **executes the submitted JS in the scoring sandbox**, so untrusted-JS execution is admin-only. An `owner_signature` self-serve branch remains in the handler but is unreachable behind the admin gate in every deployed config (dev-open only when no relayer **and** `LOCAL_TESTNET=1`). Per-IP rate-limited (`APP_CREATE_RATE_PER_MIN`, default 5/min). |
| **Validate** (`POST /v1/apps/validate`) | **Admin-gated** (`X-Admin-Key`) — PR #933. It **executes the submitted JS** in the scoring sandbox (loads the module to check it exports `score()`) — the same credential-exfil surface as create, so it is no longer open. Per-IP rate-limited (`APP_VALIDATE_RATE_PER_MIN`, default 5/min). |
| **Deploy** (`POST /v1/apps/{app_id}/deploy`) | ONE of: `X-Admin-Key` (free); a wallet signature (`action="deploy"`) from an allowed signer that is fee-exempt (`DEPLOY_FEE_EXEMPT_ADDRESSES`) → free; or a payment body (`payment_signature` + on-chain proof) → the public pay path. |
| **App-management** (lifecycle / registry / registration / reads) | EIP-712 wallet-signature headers (below). Allowed signers = the app's `deployer` ∪ `APP_ADMIN_SIGNERS`. |

### Wallet-signature headers (app-management endpoints)

Auth travels in headers so it covers GETs and body-less POSTs:

```
X-App-Auth-Signer:    0x… (an allowed signer)
X-App-Auth-Signature: 0x… (EIP-712 over the action + a paramsHash binding every
                            security-relevant field, e.g. app+chain+recipient+amount)
X-App-Auth-Nonce:     single-use nonce (writes only)
X-App-Auth-Deadline:  unix seconds
```

- **Writes** consume a single-use nonce; **reads** (`admin-state`, `registry-calldata`) are deadline-bound only.
- The paramsHash binds each request's fields, so a signature cannot be replayed or re-pointed (e.g. a `float/withdraw` signature is bound to its recipient and amount).
- Fetch the next nonce from `GET /v1/apps/{app_id}/auth-nonce?deployer=<addr>`.
- `REQUIRE_APP_ACTION_SIGNATURE` (default off) still lets the admin key bypass; set it to `1` to fully retire the shared key. `APP_ADMIN_SIGNERS` is the operator wallet allowlist.

## Create / validate / deploy

### `POST /v1/apps/` — create

Body includes the JS + Solidity source, constructor args, `contract_version`
(`"v1"`/`"v2"`; empty = legacy v1), and, for self-serve, `owner_signature` +
`owner_deadline`. Records the recovered signer as `deployer`.

### `POST /v1/apps/validate` — compile preflight

**Admin-gated** (`X-Admin-Key`), rate-limited. Compiles the pasted source
(ForgeCompiler now builds `DexAggregatorAppV2`; the contracts submodule was
bumped and V2 imports resolve) **and executes the JS in the scoring sandbox** to
check it exports `score()` — the reason it is admin-only (PR #933). Returns
compile diagnostics. No state change, nothing deployed.

### `POST /v1/apps/{app_id}/deploy` — deploy on-chain

Query params: `chain_id` (optional; defaults to first supported chain),
`wait` (default `false`).

**Async by default (PR #611/#609):** guards + fee authorization run in-request
(so `403` and fee errors are still synchronous), the chain's deployment record
flips to `deploying`, and the response returns immediately:

```json
{ "status": "deploying", "poll": "/v1/apps/{app_id}/status" }
```

Poll `GET /v1/apps/{app_id}/status` until `deployments[chain].status` flips to
`solving` (success — `contract_address` set) or back to `draft` with an `error`
(failure/rollback).

**`?wait=true`** preserves the legacy synchronous response whose body carries
`contract_address` — required for scripts that read it inline. (Leader nginx
allows this route 320s.)

**Deploy fee (PR #534, #238):** the optional `DeployRequest` body carries
`payment_ref` (fee-payment tx hash), `payment_nonce`, `payment_deadline`, and
`payment_signature` (EIP-712 `pay_deploy_fee` binding `app_id, payment_ref,
chain_id, amount`). The fee is **0.5 TAO in WTAO, once per app**, bound to the
payment chain (964) — subsequent chain deploys skip re-charging. Collection is
inert until `DEPLOY_FEE_COLLECTOR_EVM` is set **and** `ENABLE_PUBLIC_DEPLOYMENT=1`.

**Auto-registration (PR #531/#533):** a successful deploy best-effort registers
the app in AppRegistry (`registerApp(keccak(app_id), sha256(js), addr)`) and
returns a `registry` dict; it never fails the deploy. Only **approved** (or
legacy) apps auto-register — a new unapproved app deploys with
`registry.pending_approval=true` (owner-controlled but inert for live routing).
`AUTO_REGISTER_APPS` (default on) disables it when registry ownership is cold.

## Deployment lifecycle

All wallet-signature gated (writes need a nonce):

| Method | Path | Purpose |
|--------|------|---------|
| `PUT` | `/v1/apps/{app_id}/solidity` | Replace stored Solidity source / ctor args / `contract_version`. Refuses mid-deploy. |
| `POST` | `/v1/apps/{app_id}/deployments/{chain_id}/retire` | Mark the deployment RETIRED, releasing the deploy guard so the deploy route acts as an **in-place redeploy** (upserts on `(app_id, chain_id)`, same `app_id`). |
| `POST` | `/v1/apps/{app_id}/deregister` | **App-wide** deregister (admin key OR wallet signature `action="deregister_app"`): schedules every non-deploying deployment to RETIRING (stops new orders immediately) and, ~1 tempo later via a round-anchored fleet-uniform cutover, drops the app from the **whole benchmark corpus + pack hash** — keeping all order rows (deregister, not delete). |
| `POST` | `/v1/apps/{app_id}/deployments/{chain_id}/float/deposit` | Fund a V2 app-held WETH float from the relayer (optionally wrapping relayer ETH first). |
| `POST` | `/v1/apps/{app_id}/deployments/{chain_id}/float/withdraw` | Recover the float. |
| `PATCH` | `/v1/apps/{app_id}/deployments/{chain_id}/config` | V2 relayer-gated setters: `setFeeBps`, `setVolumeCapBps`, `setFeeCollector`, `setFeeMode` (0=USER / 1=APP), `setAppOwner` (V2 float-recovery co-signer). |

> V2 apps settle fees from an app-held WETH float — a production V2 app needs a
> funded float or nonzero-fee orders revert / score zero (PR #527).

## Registry & registration

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/apps/{app_id}/deployments/{chain_id}/registry/allow-developer` | Owner `setDeveloperAllowed` via the relayer (PR #531). |
| `GET` | `/v1/apps/{app_id}/deployments/{chain_id}/registry-calldata` | Prepared `registerApp` / `revokeApp` calldata for external signing (revoke needs the cold registry-owner key). |
| `POST` | `/v1/apps/{app_id}/registration/request` | Owner-signed → `registration_status = requested`. |
| `POST` | `/v1/apps/{app_id}/registration/approve` | **Admin only** (`APP_ADMIN_SIGNERS`; owner cannot approve own app) → `approved` + on-chain register. |
| `POST` | `/v1/apps/{app_id}/registration/reject` | **Admin only** → `rejected`. |

Registration moderation (PR #533) implements **permissionless deploy,
admin-gated activation**: anyone can deploy, but the app is inert for live order
routing until an admin approves it. `registration_status` is one of
`unrequested` / `requested` / `approved` / `rejected` (absent = legacy = approved).

## Reads

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/v1/apps/` | List; each item carries a per-chain `deployments` map `{ "<chain_id>": { status, contract_address } }` and a unified app `status` (`partial` for mixed multi-chain states, PR #598). Render per-chain from `deployments`; the singular `deployment` on `/status` is deprecated for rendering. |
| `GET` | `/v1/apps/{app_id}/status` | Per-app + per-chain deployment status (the deploy-poll target). |
| `GET` | `/v1/apps/{app_id}/admin-state` | Full operator view (PR #528): store record (JS+Solidity + sha256, ctor args, per-chain deployments), live per-chain app config (relayer, feeMode, collector, fee bounds, paymaster, wrappedNativeToken, thresholds, dex feeBps/volumeCapBps), fee-settlement balances (V2 float / V1 paymaster + relayer gas), and AppRegistry status (mode, developer, manifestHash, allowlist). Chain reads are best-effort — dead RPC degrades to nulls + a per-chain `errors` list, never 5xx. |
| `GET` | `/v1/apps/{app_id}/auth-nonce?deployer=<addr>` | Next single-use nonce for wallet-signature writes. |
| `GET` | `/v1/chains` | Per chain now includes `app_registry_address` (the AppRegistry gate) alongside `registry_address` (ValidatorRegistry); `""` when no app gate is configured (PR #553). |

## Related operator env

| Variable | Default | Notes |
|----------|---------|-------|
| `REQUIRE_APP_ACTION_SIGNATURE` | off | `1` retires the shared admin-key bypass for app-management writes. |
| `APP_ADMIN_SIGNERS` | -- | Operator wallet allowlist (admin signers). |
| `APP_CREATE_RATE_PER_MIN` | `5` | Non-admin create rate limit (per IP). |
| `APP_VALIDATE_RATE_PER_MIN` | `5` | Validate rate limit (per IP). |
| `ENABLE_PUBLIC_DEPLOYMENT` | off | Must be `1` for fee collection / public deploy to be live. |
| `DEPLOY_FEE_RAIL` | `evm` | `evm` (WTAO) or `finney`. |
| `DEPLOY_FEE_COLLECTOR_EVM` | -- | Unset ⇒ collection inert. |
| `DEPLOY_FEE_PAYMENT_CHAIN_ID` | `964` | Chain the once-per-app fee is bound to. |
| `DEPLOY_FEE_TOKEN_ADDRESS` | -- | WTAO token. |
| `DEPLOY_FEE_MIN_CONFIRMATIONS` | `6` | Confirmations required on the fee-payment tx. |
| `DEPLOY_FEE_EXEMPT_ADDRESSES` | -- | Signers that deploy free. |
| `AUTO_REGISTER_APPS` | on | Auto-register approved apps at deploy. |
| `DEPLOY_RECEIPT_TIMEOUT_SECONDS` | ETH 110s / Base·BTEVM 90s | Per-chain deploy receipt wait (type-2 gas, per-chain tip floors, PR #556). |
