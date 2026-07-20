"""Solver repo management — trustless champion merge via on-chain attestation.

When a miner's solver wins the benchmark and passes champion consensus (N-of-M
EIP-712 signatures), the leader:
  1. Records the certification on-chain (ChampionRegistry on BT EVM, chain 964)
  2. Creates a GitHub PR with the on-chain tx hash as proof
  3. A GitHub Action verifies the on-chain record and auto-merges

The leader CANNOT push directly to main — branch protection + the Action are
the only merge authority. This prevents a compromised leader from self-
certifying malicious solver code.

Branch model:
  - main: current champion solver (merged only by GitHub Action)
  - miner/{id}: each miner pushes improvements to their own branch
  - champion/{round_id}: PR branches created by the leader after certification
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


# ── On-chain attestation ─────────────────────────────────────────────────────


def _str_to_bytes32(value: str | None) -> bytes:
    """Convert a string to bytes32, matching champion_manager.py logic."""
    from eth_hash.auto import keccak

    raw = (value or "").strip()
    if not raw:
        return b"\x00" * 32
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return keccak(raw.encode("utf-8"))


def _resolve_full_sha(short_hash: str) -> str | None:
    """Resolve a short git commit hash to full 40-char SHA via GitHub API."""
    import urllib.request
    import urllib.error

    owner_repo = _parse_github_owner_repo()
    if owner_repo is None:
        return None
    owner, repo = owner_repo
    headers = _github_api_headers()
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{short_hash}",
            headers=headers,
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read()).get("sha")
    except Exception:
        return None


# Minimal ABI for ChampionRegistry.certify() — just what we need to call it.
# v2 of the signature: commitHash, deadline, nonces[] are part of the digest.
CHAMPION_REGISTRY_ABI = [
    {
        "inputs": [
            {"name": "roundId", "type": "bytes32"},
            {"name": "committeeHash", "type": "bytes32"},
            {"name": "incumbentImageId", "type": "bytes32"},
            {"name": "candidateSubmissionId", "type": "bytes32"},
            {"name": "candidateImageId", "type": "bytes32"},
            {"name": "benchmarkPackHash", "type": "bytes32"},
            {"name": "shadowCaseLogHash", "type": "bytes32"},
            {"name": "effectiveEpoch", "type": "uint256"},
            {"name": "commitHash", "type": "bytes32"},
            {"name": "deadline", "type": "uint256"},
            {"name": "nonces", "type": "uint256[]"},
            {"name": "signatures", "type": "bytes[]"},
        ],
        "name": "certify",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "roundId", "type": "bytes32"}],
        "name": "getChampion",
        "outputs": [
            {
                "components": [
                    {"name": "roundId", "type": "bytes32"},
                    {"name": "candidateSubmissionId", "type": "bytes32"},
                    {"name": "candidateImageId", "type": "bytes32"},
                    {"name": "commitHash", "type": "bytes32"},
                    {"name": "effectiveEpoch", "type": "uint256"},
                    {"name": "certifiedAt", "type": "uint256"},
                    {"name": "approvalCount", "type": "uint256"},
                    {"name": "exists", "type": "bool"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "roundId", "type": "bytes32"}],
        "name": "isChampionCertified",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        # Latest certified champion — O(1) state read (no log scan), used by the
        # leader's in-process merge gate. champions[latestRoundId].
        "inputs": [],
        "name": "getLatestChampion",
        "outputs": [
            {
                "components": [
                    {"name": "roundId", "type": "bytes32"},
                    {"name": "candidateSubmissionId", "type": "bytes32"},
                    {"name": "candidateImageId", "type": "bytes32"},
                    {"name": "commitHash", "type": "bytes32"},
                    {"name": "effectiveEpoch", "type": "uint256"},
                    {"name": "certifiedAt", "type": "uint256"},
                    {"name": "approvalCount", "type": "uint256"},
                    {"name": "exists", "type": "bool"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getQuorumRequired",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# Gas ceiling for the ChampionRegistry.certify() attestation. The node reserves
# gas_limit × price up front, so this × the price is the exact balance the
# relayer wallet must hold to even SUBMIT an attest (below it: "insufficient
# funds for gas * price + value", champion adoption freezes).
_ATTEST_GAS_LIMIT = 500_000
# BT-EVM tip floor (mirrors the deploy path, #556) so a node reporting ~0 tip
# doesn't underprice the attest.
_ATTEST_TIP_FLOOR_WEI = 500_000_000  # 0.5 gwei
# Receipt-poll cadence for the BT-EVM attest wait. web3.py defaults to 0.1s,
# which fires ~10 eth_getTransactionReceipt/sec while a ~12s-block BT-EVM tx
# mines — a self-inflicted burst (~120 calls/attempt × 3 retries) that trips
# the public RPC's per-IP rate limit (429 → attest skipped → merge gate refuses
# → champion freeze; diagnosed live 2026-07-09, with the official
# lite.chain.opentensor.ai endpoint healthy for a normal caller). At 3s we poll
# ~4×/block — plenty to catch the receipt promptly, ~30× less RPC load.
# Env-tunable for other BT-EVM block times.
_ATTEST_RECEIPT_POLL_LATENCY_S = float(
    os.environ.get("ATTEST_RECEIPT_POLL_LATENCY_S", "3.0")
)

# Merge-gate cert-read resilience. By the time the merge gate re-reads the
# ChampionRegistry to confirm a quorum cert binds the head SHA, the attest has
# ALREADY landed on-chain — so a transient 429/timeout on that READ must not
# throw away a certified, already-attested win (the merge_failed churn: attest
# succeeds, cert-read 429s on the public BT-EVM endpoint, merge refused, champion
# frozen; diagnosed live 2026-07-09). Retry the read with exponential backoff
# before fail-closing. A read that SUCCEEDS but does not bind (or quorum < 1) is
# a DEFINITIVE negative and is never retried — the gate stays fail-closed against
# an uncertified head. Env-tunable, same discipline as the attest retry.
_CERT_READ_ATTEMPTS = int(os.environ.get("MERGE_GATE_CERT_READ_ATTEMPTS", "4"))
_CERT_READ_BACKOFF_S = float(os.environ.get("MERGE_GATE_CERT_READ_BACKOFF_S", "2.0"))

# Trust this round's own status=1 certify() receipt as proof the cert binds the
# head, skipping the registry re-read entirely (there's no read to 429). DEFAULT
# ON. Trade-off: unlike a fresh read, it can't observe a chain reorg that orphans
# the certify() tx between the receipt and the merge — a rare, non-attacker
# robustness gap that self-heals (the leader re-attests next round). Set to 0 to
# force the authoritative (retrying) read on every merge.
_TRUST_ATTEST_RECEIPT = os.environ.get(
    "MERGE_GATE_TRUST_ATTEST_RECEIPT", "1"
).strip().lower() in ("1", "true", "yes", "on")


def _attest_gas_fields(w3: Any) -> dict:
    """EIP-1559 (type-2) gas fields for the attest tx, with a legacy fallback.

    Mirrors the deploy path (#556): maxFee = base*4 + tip, tip floored so it
    isn't underpriced when the node reports ~0. Falls back to legacy gasPrice on
    a pre-1559 node (no baseFeePerGas)."""
    base = None
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:  # noqa: BLE001 — best-effort; fall back to legacy
        base = None
    if base:
        try:
            node_tip = int(w3.eth.max_priority_fee or 0)
        except Exception:  # noqa: BLE001
            node_tip = 0
        tip = max(node_tip, _ATTEST_TIP_FLOOR_WEI)
        return {"maxFeePerGas": int(base) * 4 + tip, "maxPriorityFeePerGas": tip}
    return {"gasPrice": w3.eth.gas_price}


def _warn_if_low_attest_balance(w3: Any, addr: str, gas_fields: dict) -> None:
    """Log a LOUD warning when the relayer can't cover several more attestations.

    The failure mode this surfaces: the BT-EVM wallet silently drains, the attest
    reverts "insufficient funds", there's no on-chain quorum cert, and the
    merge-gate refuses to adopt — champion adoption freezes with only a cryptic
    web3 error. Best-effort; never raises."""
    try:
        bal = int(w3.eth.get_balance(addr))
        price = int(gas_fields.get("maxFeePerGas") or gas_fields.get("gasPrice") or 0)
        cost = _ATTEST_GAS_LIMIT * price
        if cost and bal < cost * 3:
            logger.warning(
                "LOW ATTEST BALANCE: relayer %s holds %.6f TAO on BT-EVM — only "
                "~%d attestation(s) of runway (each reserves %.6f TAO @ %d gas). "
                "Fund it or champion adoption freezes (attest fails → merge gate "
                "refuses to adopt).",
                addr, bal / 1e18, bal // cost, cost / 1e18, _ATTEST_GAS_LIMIT,
            )
    except Exception:  # noqa: BLE001 — a diagnostic must never break attest
        pass


def attest_champion_on_chain(
    certificate: Any,
    commit_hash: str,
) -> str | None:
    """Record a champion certification on BT EVM's ChampionRegistry.

    Calls ``ChampionRegistry.certify()`` with the validator EIP-712 signatures.
    The relayer fronts the gas (paid in TAO on BT EVM).

    Args:
        certificate: ChampionCertificate with approvals (sorted by validator_id).
        commit_hash: Git commit hash of the champion's code.

    Returns:
        Transaction hash (0x-prefixed) on success, None on failure.
    """
    registry_addr = os.environ.get("CHAMPION_REGISTRY_964", "").strip()
    rpc_url = os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
    relayer_key = os.environ.get("RELAYER_PRIVATE_KEY", "").strip()

    if not registry_addr or not rpc_url or not relayer_key:
        logger.warning(
            "On-chain attestation skipped: missing CHAMPION_REGISTRY_964 "
            "(%s) or BITTENSOR_EVM_RPC_URL (%s) or RELAYER_PRIVATE_KEY",
            "set" if registry_addr else "unset",
            "set" if rpc_url else "unset",
        )
        return None

    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            logger.error("Cannot connect to BT EVM at %s", rpc_url)
            return None

        registry = w3.eth.contract(
            address=Web3.to_checksum_address(registry_addr),
            abi=CHAMPION_REGISTRY_ABI,
        )

        # Convert certificate fields to bytes32
        round_id = _str_to_bytes32(certificate.round_id)
        committee_hash = _str_to_bytes32(certificate.committee_hash)
        incumbent_image_id = _str_to_bytes32(certificate.incumbent_image_id)
        candidate_submission_id = _str_to_bytes32(certificate.candidate_submission_id)
        candidate_image_id = _str_to_bytes32(certificate.candidate_image_id)
        benchmark_pack_hash = _str_to_bytes32(certificate.benchmark_pack_hash)
        shadow_case_log_hash = _str_to_bytes32(certificate.shadow_case_log_hash)
        effective_epoch = int(certificate.effective_epoch or 0)

        # Resolve short commit hash to full SHA for consistent on-chain
        # encoding. The GitHub Action computes keccak(full_sha) and compares
        # with the on-chain bytes32 — mismatched lengths produce different hashes.
        if len(commit_hash) < 40:
            full_sha = _resolve_full_sha(commit_hash)
            if full_sha:
                commit_hash = full_sha
        commit_hash_bytes = _str_to_bytes32(commit_hash)

        # Extract signatures + nonces parallel-indexed, sorted by validator_id
        # ascending (already sorted by the consensus manager, but sort again
        # for safety). v2 of the contract requires a nonce per signature and
        # a shared deadline.
        sorted_approvals = sorted(
            certificate.approvals,
            key=lambda a: int(
                (getattr(a, "validator_id", "0x0") or "0x0").replace("0x", "") or "0",
                16,
            ),
        )
        signatures: list[bytes] = []
        nonces: list[int] = []
        deadlines: list[int] = []
        for approval in sorted_approvals:
            sig_hex = getattr(approval, "signature", "") or ""
            if sig_hex:
                signatures.append(bytes.fromhex(sig_hex.replace("0x", "")))
                nonces.append(int(getattr(approval, "nonce", 0) or 0))
                deadlines.append(int(getattr(approval, "deadline", 0) or 0))

        if not signatures:
            logger.error("No signatures in certificate — cannot attest")
            return None

        # All approvals must share the same deadline — if they don't, the
        # digest per-signer differs and the contract-level recovery will
        # yield the wrong signer. Consensus manager guarantees this today
        # (one proposal → one deadline).
        if len(set(deadlines)) != 1:
            logger.error(
                "Approvals have inconsistent deadlines %s — aborting attest",
                deadlines,
            )
            return None
        deadline = deadlines[0]

        # Build and send transaction
        relayer_addr = Account.from_key(relayer_key).address
        nonce = w3.eth.get_transaction_count(relayer_addr, "pending")

        _gas_fields = _attest_gas_fields(w3)
        _warn_if_low_attest_balance(w3, relayer_addr, _gas_fields)

        tx = registry.functions.certify(
            round_id,
            committee_hash,
            incumbent_image_id,
            candidate_submission_id,
            candidate_image_id,
            benchmark_pack_hash,
            shadow_case_log_hash,
            effective_epoch,
            commit_hash_bytes,
            deadline,
            nonces,
            signatures,
        ).build_transaction({
            "from": relayer_addr,
            "nonce": nonce,
            "gas": _ATTEST_GAS_LIMIT,
            "chainId": w3.eth.chain_id,
            **_gas_fields,
        })

        signed = w3.eth.account.sign_transaction(tx, relayer_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        # poll_latency: don't hammer the RPC with getTransactionReceipt at the
        # web3 default 0.1s (see _ATTEST_RECEIPT_POLL_LATENCY_S) — that burst is
        # what 429s us on the public BT-EVM endpoint.
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=60, poll_latency=_ATTEST_RECEIPT_POLL_LATENCY_S,
        )

        if receipt["status"] != 1:
            # Surface the on-chain revert reason rather than failing silently.
            # A mined-but-reverted tx carries no reason in the receipt, so
            # re-execute it at its (reverted) block — web3 raises with the
            # require() string. Best-effort: never let the diagnostic itself throw.
            revert_reason = "unknown (could not re-call)"
            try:
                w3.eth.call(
                    {
                        "from": tx["from"],
                        "to": tx["to"],
                        "data": tx["data"],
                        "value": tx.get("value", 0),
                    },
                    block_identifier=receipt["blockNumber"],
                )
            except Exception as call_exc:  # noqa: BLE001 — best-effort diagnostic
                revert_reason = str(call_exc)
            hint = ""
            if "Nonce not increasing" in revert_reason:
                hint = (
                    " — a champion nonce <= the on-chain per-signer high-water "
                    "(lastNonce). Usually a BACKWARD wall-clock movement on the "
                    "proposing leader (NTP step / VM migration / restart onto a "
                    "skewed host). The off-chain floor (_floor_champion_nonce) "
                    "should prevent this; if it recurs, suspect the leader clock "
                    "or its BT-EVM RPC read of lastNonce."
                )
            logger.error(
                "ChampionRegistry.certify() reverted: tx=%s reason=%s nonces=%s%s",
                tx_hash.hex(), revert_reason, nonces, hint,
            )
            return None

        tx_hash_hex = "0x" + tx_hash.hex()
        logger.info(
            "Champion attested on-chain: tx=%s round=%s",
            tx_hash_hex,
            certificate.round_id,
        )
        return tx_hash_hex

    except Exception as exc:
        logger.error("On-chain attestation failed: %s", exc, exc_info=True)
        return None


# ── GitHub PR creation ────────────────────────────────────────────────────────


def _github_api_headers(token: str | None = None) -> dict[str, str]:
    """Build GitHub API headers.

    ``token`` (the private path's per-submission PAT) wins; otherwise fall back to
    the validator's canonical-repo token from the environment.
    """
    token = (token or os.environ.get(
        "SOLVER_REPO_PR_TOKEN",
        os.environ.get("SOLVER_REPO_TOKEN", ""),
    )).strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── PR lifecycle (the PR-based submission fold) ──────────────────────────────
# The miner opens the PR; the leader MIRRORS the off-chain quorum's decision onto
# it — a scoring-report comment + merge on ADOPT (the cert-gated Action merges), or
# a comment + close + candidate-image GC on REJECT. These are thin GitHub/GHCR API
# wrappers; they no-op without SOLVER_REPO_URL / a token, so they are inert on a
# node that isn't the configured leader.

# Transient GitHub statuses worth retrying: network/timeout (0), secondary
# rate-limit (429), and server errors (5xx). A 4xx is a deterministic client
# error and is never retried.
_GITHUB_RETRY_STATUSES = frozenset({0, 429, 500, 502, 503, 504})
_GITHUB_MAX_ATTEMPTS = 4        # 1 initial attempt + 3 retries
_GITHUB_RETRY_BACKOFF_S = 1.0   # base seconds; backs off 1s, 2s, 4s


def _github_api_request(
    method: str, url: str, payload: dict | None = None, *, token: str | None = None,
) -> tuple[int, dict | None]:
    """Issue a GitHub API request. Returns (status, json|None); never raises.

    ``token`` (private path) authenticates against the miner's private repo;
    otherwise the canonical-repo environment token is used.

    Transient failures (network/timeout, 429, or 5xx) are retried with bounded
    exponential backoff. GitHub's git-object writes are content-addressed and
    idempotent (blobs/trees by content, ref-update to a fixed SHA) and every
    read is idempotent, so retrying the champion-publish path is safe — a
    one-off GitHub 503 must not drop a certified dethrone (incident 2026-07-20:
    round-e29741775 aborted merge_failed:publish_failed on a transient 503).
    A 4xx is a deterministic client error and is returned immediately.
    """
    import time
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    result: tuple[int, dict | None] = (0, None)
    for attempt in range(_GITHUB_MAX_ATTEMPTS):
        req = urllib.request.Request(
            url, data=data, headers=_github_api_headers(token), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed github host
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            result = (exc.code, None)
            logger.warning("GitHub %s %s -> %s: %s", method, url, exc.code, exc.reason)
        except Exception as exc:  # network / json
            result = (0, None)
            logger.warning("GitHub %s %s failed: %s", method, url, exc)
        if result[0] not in _GITHUB_RETRY_STATUSES or attempt == _GITHUB_MAX_ATTEMPTS - 1:
            break
        time.sleep(_GITHUB_RETRY_BACKOFF_S * (2 ** attempt))
    return result


def comment_on_pr(
    pr_number: int, body: str, *, owner_repo=None, token: str | None = None,
) -> bool:
    """Post a comment on a solver-repo PR (used for the scoring report).

    Public path: ``owner_repo``/``token`` are None → canonical repo + env token.
    Private path: pass the miner's ``(owner, repo)`` + per-submission ``token`` so
    the report/feedback lands on the miner's private PR.
    """
    owner_repo = owner_repo or _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        return False
    owner, repo = owner_repo
    status, _ = _github_api_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        {"body": body},
        token=token,
    )
    return status in (200, 201)


def post_intake_ack(
    pr_number: int, *, owner_repo: tuple[str, str], token: str, round_id: str,
) -> int:
    """Post the intake ACK comment on a PRIVATE-path PR; return the HTTP status.

    This is the write-scope probe for the miner's per-submission PAT: commenting
    on a PR is gated by the SAME fine-grained permission (``Pull requests: Write``)
    the post-benchmark report needs, and there is no reliable read-only way to
    check a fine-grained token's granted permissions. Without it an under-scoped
    (read-only) token passes intake — resolve_pr and clone only need reads — and
    the failure surfaces days later as a silent 403 when the report posts (seen
    live 2026-07-03: 39 x 403 across two miners' private repos).

    Returns the raw status so the intake gate can distinguish a definitive
    permission failure (401/403/404 → reject the submission with an actionable
    message) from a transient one (0/5xx → fail open). Unlike ``comment_on_pr``
    this never falls back to the canonical repo: the probe is only meaningful
    against the miner's own repo with the miner's own token.
    """
    owner, repo = owner_repo
    status, _ = _github_api_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        {
            "body": (
                "### Submission received\n\n"
                f"Queued for screening and benchmarking in round `{round_id}`. "
                "The benchmark report (or rejection reason) will be posted on "
                "this PR.\n\n"
                "_This comment also verifies your `repo_token` can post that "
                "report (`Pull requests: Read and write`)._"
            )
        },
        token=token,
    )
    return status


def close_pr(pr_number: int) -> bool:
    """Close a solver-repo PR (the REJECT path — no certificate was emitted)."""
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        return False
    owner, repo = owner_repo
    status, _ = _github_api_request(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        {"state": "closed"},
    )
    return status == 200


def close_stale_submission_prs(
    winner_pr_number: int,
    *,
    champion_label: str = "the new champion",
) -> int:
    """Close OTHER open miner-submission PRs after a champion's PR merges to main.

    A champion merge advances ``main`` to its ``solver.py``. Every other open
    submission PR replaces ``solver.py`` too, so they ALL become conflicting against
    the new ``main`` and can no longer be merged/adopted — even if they win a future
    benchmark — until rebased. Close them (with a rebase instruction) so the miner
    resubmits on the new base. This is a DISTINCT trigger from the per-round reject
    path (``on_champion_rejected_pr`` keeps PRs OPEN): here the base itself moved.

    Only FORK PRs (head owned by a non-canonical account) are closed — team branch
    PRs on the solver repo are left alone — and the just-merged ``winner_pr_number``
    is skipped (the merge already closed it). Best-effort; returns the count closed.
    """
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None:
        return 0
    owner, repo = owner_repo
    status, prs = _github_api_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=100",
    )
    if status != 200 or not isinstance(prs, list):
        logger.warning("close-stale: could not list open PRs (HTTP %s)", status)
        return 0
    closed = 0
    for pr in prs:
        num = pr.get("number")
        if not num or int(num) == int(winner_pr_number):
            continue
        head_owner = (
            (((pr.get("head") or {}).get("repo") or {}).get("owner") or {}).get("login")
            or ""
        )
        # Only miner FORK submissions go stale on a champion change; skip team branch
        # PRs (head on the canonical repo) and headless/ghost PRs (deleted fork).
        if not head_owner or head_owner.lower() == owner.lower():
            continue
        comment_on_pr(
            int(num),
            "### Closed — a new champion was elected\n\n"
            f"`main` has advanced to {champion_label}, so this PR's base is now stale: "
            "it replaces `solver.py` from an older `main` and will conflict — it can no "
            "longer be merged or adopted, even if it wins a benchmark.\n\n"
            "**Rebase your fork onto the latest `main` and resubmit** to compete "
            "against the new champion.",
        )
        if close_pr(int(num)):
            closed += 1
    logger.info(
        "close-stale: closed %d stale submission PR(s) after champion merge "
        "(winner #%s)", closed, winner_pr_number,
    )
    return closed


def delete_candidate_image(pr_number: int) -> bool:
    """GC a rejected candidate's ``pr-<N>`` image tag from GHCR.

    Finds the org container-package version whose tags include ``pr-<N>`` and
    deletes it. Best-effort (needs ``delete:packages``); never raises. The
    org/package are derived from ``CANDIDATE_IMAGE_REPO`` (``ghcr.io/ORG/PKG``).
    """
    from minotaur_subnet.harness.image_transport import candidate_repo

    repo_ref = candidate_repo()  # ghcr.io/ORG/PKG
    parts = repo_ref.split("/")
    if len(parts) < 3:
        return False
    org, package = parts[1], "/".join(parts[2:])
    tag = f"pr-{pr_number}"
    status, versions = _github_api_request(
        "GET",
        f"https://api.github.com/orgs/{org}/packages/container/{package}/versions?per_page=100",
    )
    if status != 200 or not isinstance(versions, list):
        return False
    for v in versions:
        tags = (((v or {}).get("metadata") or {}).get("container") or {}).get("tags") or []
        if tag in tags:
            vid = v.get("id")
            del_status, _ = _github_api_request(
                "DELETE",
                f"https://api.github.com/orgs/{org}/packages/container/{package}/versions/{vid}",
            )
            return del_status in (200, 204)
    return False  # no matching tag (already pruned / never pushed)


def _render_report_body(
    submission: Any,
    reason: str,
    champion_score: float | None = None,   # retained for call-site compat; unused
    dethrone_margin: float | None = None,  # (report is now per-order only)
    *,
    won: bool = False,
) -> str:
    """PR-comment body for a champion-consensus outcome: the same-pin per-order
    ``relative`` report when the submission was benchmarked, else the concise
    reason. ``won=True`` renders it as a win (header + fallback), otherwise as a
    rejection. Never raises.

    ``champion_score`` / ``dethrone_margin`` are accepted for call-site
    compatibility but no longer used — the report dropped the aggregate scalars
    they fed (see ``report.py`` module docstring)."""
    fallback = (
        f"### 🏆 Beat the champion\n\n{reason}" if won
        else f"### ❌ Submission rejected\n\n{reason}"
    )
    try:
        from minotaur_subnet.api.routes.submissions.report import (
            build_submission_report,
            render_report_md,
        )

        report = build_submission_report(submission, reason=reason, won=won)
        # Only enrich when there's real per-order detail to show; a screening or
        # otherwise-empty (no stored ``relative`` block) report falls back to the
        # concise reason message.
        if not report or not report.get("relative"):
            return fallback
        md = render_report_md(report, submission_id=getattr(submission, "submission_id", None))
        return md or fallback
    except Exception as exc:
        logger.warning("PR rejection report render failed: %s", exc)
        return fallback


def _pr_comment_target(submission: Any, repo_token: str | None):
    """Return ``(owner_repo, token)`` for commenting on a submission's PR,
    or ``None`` when there is NO safe target and the comment must be skipped.

    Public submissions return ``(None, None)`` so ``comment_on_pr`` falls back
    to the canonical repo + env token. Private submissions comment on the
    miner's private repo using the per-submission PAT — and when that token is
    unavailable (lost pre-#500, purged, SUBMISSION_TOKEN_PERSIST=0) the answer
    is ``None``, NOT the canonical fallback: a private submission's pr_number
    is meaningless on the canonical repo, and when a same-numbered PR happens
    to exist there the post SUCCEEDS silently — misdirecting a private miner's
    per-order report onto a public team PR (seen live 2026-07-02, canonical
    PR#1/#3). Callers must treat ``None`` as skip-with-warning.
    """
    if not getattr(submission, "is_private", False):
        return None, None
    if getattr(submission, "private_repo_full", None) and repo_token:
        owner, _, repo = submission.private_repo_full.partition("/")
        if owner and repo:
            return (owner, repo), repo_token
    return None


def on_champion_rejected_pr(
    submission: Any,
    reason: str,
    report_md: str | None = None,
    *,
    champion_score: float | None = None,
    dethrone_margin: float | None = None,
    repo_token: str | None = None,
) -> bool:
    """REJECT path: comment the reason + scored report on the miner's PR and GC the
    candidate image. The PR is left OPEN — only a successful merge ever closes a PR;
    reject / merge-gate failures keep it open so the miner can read the feedback and
    iterate on the same PR. Mirrors the off-chain quorum's reject decision onto the
    PR. Usable while adoption is frozen — pure miner feedback, no chain writes.

    When ``report_md`` isn't supplied, builds the scored benchmark report (the
    aggregate-vs-champion scalars plus the same-pin per-order ``relative`` count
    summary) from the submission, given ``champion_score`` / ``dethrone_margin``."""
    pr_number = getattr(submission, "pr_number", None)
    if not pr_number:
        logger.info(
            "Champion reject for %s has no pr_number — skipping PR feedback",
            getattr(submission, "submission_id", "?"),
        )
        return False
    body = report_md or _render_report_body(
        submission, reason, champion_score, dethrone_margin,
    )
    _target = _pr_comment_target(submission, repo_token)
    if _target is None:
        logger.warning(
            "Champion reject for private %s (PR#%s): no usable repo token — "
            "SKIPPING the PR comment (never fall back to the canonical repo)",
            getattr(submission, "submission_id", "?"), pr_number,
        )
        commented = False
    else:
        _owner_repo, _tok = _target
        commented = comment_on_pr(pr_number, body, owner_repo=_owner_repo, token=_tok)
    # Do NOT close the PR on a failure — only a successful squash-merge ever closes a
    # PR (GitHub auto-closes on merge). Leaving reject / merge-gate failures OPEN lets
    # the miner read the feedback and iterate on the same PR.
    gced = delete_candidate_image(pr_number)
    logger.info(
        "Champion reject PR#%s: comment=%s gc=%s (PR left OPEN — only a merge closes)",
        pr_number, commented, gced,
    )
    return commented


def on_champion_finalist_pr(
    submission: Any,
    reason: str,
    report_md: str | None = None,
    *,
    champion_score: float | None = None,
    dethrone_margin: float | None = None,
    repo_token: str | None = None,
) -> bool:
    """WIN path: comment the full scored report on a WINNING candidate's PR so
    winners get feedback even when the round later fails to certify — and NEVER
    close the PR (the winner's PR must stay open for the cert-gated merge).

    Mirrors the leader's finalist selection (the candidate beat the champion by
    the dethrone margin) onto the miner's PR. Unlike the reject path, this only
    comments — no ``close_pr`` and no candidate-image GC.

    When ``report_md`` isn't supplied, builds the scored benchmark report (the
    aggregate-vs-champion scalars plus the same-pin per-order ``relative`` count
    summary) from the submission, given ``champion_score`` / ``dethrone_margin``."""
    pr_number = getattr(submission, "pr_number", None)
    if not pr_number:
        logger.info(
            "Champion finalist for %s has no pr_number — skipping PR comment",
            getattr(submission, "submission_id", "?"),
        )
        return False
    body = report_md or _render_report_body(
        submission, reason, champion_score, dethrone_margin,
        won=True,
    )
    _target = _pr_comment_target(submission, repo_token)
    if _target is None:
        logger.warning(
            "Champion finalist for private %s (PR#%s): no usable repo token — "
            "SKIPPING the PR comment (never fall back to the canonical repo)",
            getattr(submission, "submission_id", "?"), pr_number,
        )
        commented = False
    else:
        _owner_repo, _tok = _target
        commented = comment_on_pr(pr_number, body, owner_repo=_owner_repo, token=_tok)
    logger.info(
        "Champion finalist PR#%s: comment=%s (kept open for cert-gated merge)",
        pr_number, commented,
    )
    return commented


def on_round_not_selected_pr(
    submission: Any, reason: str, *, repo_token: str | None = None,
) -> bool:
    """ROTATION path: light "not selected this round" comment on the miner's PR.

    Fired at round close for submissions the LRU rotation left off the benched
    slate — previously they were terminal-rejected with a reason only visible on
    the status endpoint, and the PR read as pure silence. No scored report
    (nothing was benchmarked), no image GC, PR stays open.

    MUST run BEFORE the store's terminal ``reject()``: reject purges a private
    submission's repo token, and this comment needs it to post.
    """
    pr_number = getattr(submission, "pr_number", None)
    if not pr_number:
        return False
    _target = _pr_comment_target(submission, repo_token)
    if _target is None:
        logger.warning(
            "Round not-selected for private %s (PR#%s): no usable repo token — "
            "SKIPPING the PR comment (never fall back to the canonical repo)",
            getattr(submission, "submission_id", "?"), pr_number,
        )
        return False
    _owner_repo, _tok = _target
    body = (
        "### ⏭️ Not selected this round\n\n"
        f"{reason}\n\n"
        "_This was slate rotation (fair round entry), not a verdict on your "
        "solver — no benchmark was run._"
    )
    commented = comment_on_pr(pr_number, body, owner_repo=_owner_repo, token=_tok)
    logger.info(
        "Round not-selected PR#%s: comment=%s (PR left OPEN)", pr_number, commented,
    )
    return commented


def _parse_github_owner_repo() -> tuple[str, str] | None:
    """Extract owner/repo from SOLVER_REPO_URL."""
    url = os.environ.get("SOLVER_REPO_URL", "").strip()
    if not url:
        return None
    # Handle various GitHub URL formats
    for prefix in (
        "https://github.com/",
        "git@github.com:",
        "ssh://git@github.com/",
    ):
        if url.startswith(prefix):
            path = url[len(prefix):]
            path = path.removesuffix(".git")
            parts = path.split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
    return None


def create_champion_pr(
    submission: Any,
    round_id: str | None,
    tx_hash: str | None,
    certificate: Any,
) -> str | None:
    """Create a GitHub PR for the champion's code, with on-chain proof.

    The PR body contains machine-readable HTML comments that the GitHub Action
    parses to find the on-chain tx hash and round ID for verification.

    Args:
        submission: Adopted Submission (has repo_url, commit_hash, hotkey, etc.)
        round_id: Solver round identifier.
        tx_hash: BT EVM transaction hash from attest_champion_on_chain().
        certificate: ChampionCertificate (for metadata in the PR body).

    Returns:
        PR URL on success, None on failure.
    """
    import urllib.request
    import urllib.error

    owner_repo = _parse_github_owner_repo()
    if owner_repo is None:
        logger.error("Cannot parse SOLVER_REPO_URL — PR creation skipped")
        return None

    owner, repo = owner_repo
    commit_hash = getattr(submission, "commit_hash", "") or ""
    submission_id = getattr(submission, "submission_id", "") or ""
    hotkey = getattr(submission, "hotkey", "") or ""
    benchmark_score = getattr(submission, "benchmark_score", None)

    if not commit_hash or commit_hash in ("builtin", ""):
        logger.info("Skipping PR for non-git submission: %s", submission_id)
        return None

    # Resolve short hash to full SHA — both the branch and PR body
    # must use the full SHA for the GitHub Action to verify against on-chain.
    if len(commit_hash) < 40:
        full_sha = _resolve_full_sha(commit_hash)
        if full_sha:
            commit_hash = full_sha

    if not commit_hash or commit_hash in ("builtin", ""):
        logger.info("Skipping PR for non-git submission: %s", submission_id)
        return None

    headers = _github_api_headers()
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # Step 1: Create the champion branch from the submission's commit.
    # Use the Git refs API to create a branch pointing at the miner's commit.
    branch_name = f"champion/{round_id or 'unknown'}"
    ref_name = f"refs/heads/{branch_name}"

    try:
        # Resolve short commit hash to full SHA — GitHub refs API requires 40-char SHA.
        full_sha = commit_hash
        if len(commit_hash) < 40:
            try:
                resolve_req = urllib.request.Request(
                    f"{api_base}/commits/{commit_hash}",
                    headers=headers,
                )
                resolve_resp = urllib.request.urlopen(resolve_req, timeout=15)
                full_sha = json.loads(resolve_resp.read()).get("sha", commit_hash)
            except Exception as exc:
                logger.warning("Could not resolve short SHA %s: %s", commit_hash, exc)

        create_ref_body = json.dumps({
            "ref": ref_name,
            "sha": full_sha,
        }).encode()
        req = urllib.request.Request(
            f"{api_base}/git/refs",
            data=create_ref_body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                # Branch already exists — update it
                update_body = json.dumps({"sha": commit_hash, "force": True}).encode()
                update_req = urllib.request.Request(
                    f"{api_base}/git/refs/heads/{branch_name}",
                    data=update_body,
                    headers={**headers, "Content-Type": "application/json"},
                    method="PATCH",
                )
                urllib.request.urlopen(update_req, timeout=30)
            else:
                raise
    except Exception as exc:
        logger.error("Failed to create champion branch: %s", exc)
        return None

    # Step 2: Create the PR.
    effective_epoch = getattr(certificate, "effective_epoch", 0) if certificate else 0
    approvals_count = len(certificate.approvals) if certificate else 0
    explorer_url = f"https://evm.taostats.io/tx/{tx_hash}" if tx_hash else "N/A"

    pr_body = (
        f"## Champion Certification\n\n"
        f"<!-- CHAMPION_TX_HASH: {tx_hash or 'pending'} -->\n"
        f"<!-- CHAMPION_ROUND_ID: {round_id or 'unknown'} -->\n"
        f"<!-- CHAMPION_COMMIT_HASH: {commit_hash} -->\n"
        f"<!-- CHAMPION_SUBMISSION_ID: {submission_id} -->\n\n"
        f"| Field | Value |\n"
        f"|-------|-------|\n"
        f"| **Round** | `{round_id}` |\n"
        f"| **Epoch** | {effective_epoch} |\n"
        f"| **Miner** | `{hotkey[:16]}...` |\n"
        f"| **Score** | {benchmark_score or 'N/A'} |\n"
        f"| **Approvals** | {approvals_count} validators |\n"
        f"| **On-chain proof** | [{tx_hash[:16] + '...' if tx_hash else 'pending'}]({explorer_url}) |\n"
        f"| **Submission** | `{submission_id}` |\n\n"
        f"This PR was created automatically after champion consensus reached quorum.\n"
        f"The GitHub Action will verify the on-chain attestation on BT EVM before merging.\n"
    )

    try:
        create_pr_body = json.dumps({
            "title": f"Champion: {submission_id} (round {round_id})",
            "body": pr_body,
            "head": branch_name,
            "base": "main",
        }).encode()
        req = urllib.request.Request(
            f"{api_base}/pulls",
            data=create_pr_body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        pr_data = json.loads(resp.read())
        pr_url = pr_data.get("html_url", "")
        logger.info("Champion PR created: %s", pr_url)
        return pr_url
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        logger.error("Failed to create champion PR: HTTP %d — %s", e.code, body)
        return None
    except Exception as exc:
        logger.error("Failed to create champion PR: %s", exc)
        return None


# ── Leader-authority merge gate ──────────────────────────────────────────────
# MERGE AUTHORITY for the solver repo's main lives HERE, in the leader's trusted
# process — NOT in a GitHub status check. Under fork PRs a GitHub check is
# fork-authored (the fork runs its own workflow copy and required checks match by
# NAME), so polling a check is spoofable. Instead the leader re-resolves the live
# head SHA, refuses any PR whose diff touches .github/** (CI-disarm guard), reads
# ChampionRegistry directly over web3, and squash-merges pinned to the head SHA
# ONLY when a quorum cert binds keccak(head_sha). The champion-merge.yml Action is
# advisory/visibility only. See project_champion_merge_fork_pr_redesign_2026_06_20.


def _read_champion_registry() -> Any | None:
    """web3 read handle for ChampionRegistry on BT EVM, or None if unconfigured.

    Reuses the same env as attest_champion_on_chain (CHAMPION_REGISTRY_964 +
    BITTENSOR_EVM_RPC_URL). State reads work on pruned RPCs (latest state), so no
    archival node or log scan is needed.
    """
    addr = os.environ.get("CHAMPION_REGISTRY_964", "").strip()
    rpc = os.environ.get("BITTENSOR_EVM_RPC_URL", "").strip()
    if not addr or not rpc:
        return None
    try:
        from web3 import Web3
        from minotaur_subnet.blockchain.web3_retry import build_retrying_web3
        w3 = build_retrying_web3(rpc)
        if not w3.is_connected():
            logger.error("merge gate: cannot connect to BT EVM at %s", rpc)
            return None
        return w3.eth.contract(
            address=Web3.to_checksum_address(addr), abi=CHAMPION_REGISTRY_ABI
        )
    except Exception as exc:
        logger.error("merge gate: ChampionRegistry handle failed: %s", exc)
        return None


def _pr_touches_ci(owner: str, repo: str, pr_number: int) -> bool:
    """True if the PR diff changes any .github/** path (CI-disarm guard).

    A real champion submission only changes solver source. A PR that also edits
    CI (e.g. to disarm/replace the advisory check, or land a malicious workflow)
    is refused. FAIL-CLOSED: a read failure is treated as touching.
    """
    st, files = _github_api_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
    )
    if st != 200 or not isinstance(files, list):
        logger.warning("merge gate: could not read PR #%s files (HTTP %s) — fail-closed", pr_number, st)
        return True
    return any((f.get("filename") or "").startswith(".github/") for f in files)


def _onchain_cert_binds(
    head_sha: str, round_id: str | None, *, attest_confirmed_sha: str | None = None,
) -> bool:
    """Leader's OWN authority check: does a quorum cert on-chain bind this head SHA?

    Asserts exists AND commitHash == keccak(utf8(lowercase head_sha)) AND
    approvalCount >= getQuorumRequired(). Tries getLatestChampion() first (the
    common case — the leader attests immediately before merging), then falls back
    to getChampion(round_id) if latest has already moved past this round. The PR
    body/comments are NEVER an input — only the on-chain record.

    Transient RPC failures on the read (registry unreadable, 429/timeout/5xx from
    the public BT-EVM endpoint) are RETRIED with exponential backoff before the
    gate fail-closes — the attest has already landed on-chain by now, so a
    rate-limited read must not discard a certified win. A read that SUCCEEDS but
    does not bind — or reports quorum < 1 — is a DEFINITIVE negative and returns
    immediately, never retried: the gate stays fail-closed against an uncertified
    head regardless of RPC weather.

    ``attest_confirmed_sha`` is the head this round's certify() tx just wrote with
    a status=1 receipt (verified in attest_champion_on_chain). certify() is
    contract-enforced quorum, so a successful receipt IS proof the cert exists and
    binds ``_str_to_bytes32(commit_hash)`` with approvalCount >= quorum. When its
    on-chain commitHash byte-encoding equals ``target``, we trust that receipt and
    SKIP the registry re-read entirely — the exact read that 429s on the public
    BT-EVM endpoint. It is set ONLY on a confirmed attest (tx_hash != None) for
    this same SHA; any mismatch, or no attest, falls through to the authoritative
    (retrying) read below. Gated by MERGE_GATE_TRUST_ATTEST_RECEIPT (default on).
    """
    target = _str_to_bytes32(head_sha.strip().lower())  # == keccak(utf8(head_sha))

    # Fast path: skip the RPC entirely when this round's own attest already
    # confirmed this head on-chain (see attest_confirmed_sha above) — the merge is
    # resilient to a rate-limited read-back because there is no read to rate-limit.
    # Compare with the SAME byte derivation the on-chain bind + the read use
    # (_str_to_bytes32), NOT a lowercased string == : _str_to_bytes32 does not
    # lowercase a non-64-char value, so a case/format-skewed attest_confirmed_sha
    # that would NOT actually bind on-chain must NOT short-circuit — it falls
    # through to the authoritative read. Equivalent to the read's commitHash check.
    if (
        _TRUST_ATTEST_RECEIPT
        and attest_confirmed_sha
        and _str_to_bytes32(attest_confirmed_sha) == target
    ):
        logger.info(
            "merge gate: head %s confirmed by this round's on-chain attest receipt "
            "(status=1 certify) — skipping registry re-read", head_sha,
        )
        return True

    # A missing registry address / RPC URL is a PERSISTENT config error, never a
    # transient RPC blip — refuse immediately rather than burning the whole
    # backoff budget on every finalize (retrying a misconfig can never succeed).
    # Mirrors the env gate in _read_champion_registry (CHAMPION_REGISTRY_964 +
    # BITTENSOR_EVM_RPC_URL) so it stays a fast, definitive refusal.
    if not os.environ.get("CHAMPION_REGISTRY_964", "").strip() or not os.environ.get(
        "BITTENSOR_EVM_RPC_URL", ""
    ).strip():
        logger.error("merge gate: ChampionRegistry env unconfigured — refusing merge")
        return False

    def _binds(rec: Any, quorum: int) -> bool:
        # ChampionRecord = (roundId, candidateSubmissionId, candidateImageId,
        # commitHash[3], effectiveEpoch, certifiedAt, approvalCount[6], exists[7]).
        # A successful read that yields an empty/malformed record (e.g. a
        # never-certified round decodes to a zero-tuple, or None) DOES NOT BIND —
        # a definitive negative, NOT a transient RPC error to retry on.
        try:
            return bool(rec[7]) and rec[3] == target and int(rec[6]) >= quorum
        except (TypeError, IndexError, ValueError):
            return False

    attempts = max(1, _CERT_READ_ATTEMPTS)
    last_exc: Any = None
    for attempt in range(attempts):
        try:
            reg = _read_champion_registry()
            if reg is None:
                # Provider/registry could not be built — transient (RPC down /
                # rate-limited). Raise into the retry path rather than fail-close.
                raise ConnectionError("ChampionRegistry unreadable")
            quorum = int(reg.functions.getQuorumRequired().call())
            if quorum < 1:
                logger.error("merge gate: on-chain quorum %s < 1 — refusing (fail-closed)", quorum)
                return False  # DEFINITIVE — read succeeded
            if _binds(reg.functions.getLatestChampion().call(), quorum):
                return True
            if round_id and _binds(
                reg.functions.getChampion(_str_to_bytes32(round_id)).call(), quorum
            ):
                return True
            # Reads succeeded; the cert genuinely does not bind this head — a
            # DEFINITIVE negative, not an RPC blip. Refuse without retrying.
            return False
        except Exception as exc:  # noqa: BLE001 — transient RPC failure: retry with backoff
            last_exc = exc
            if attempt < attempts - 1:
                wait = _CERT_READ_BACKOFF_S * (2 ** attempt)
                logger.warning(
                    "merge gate: cert read attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt + 1, attempts, exc, wait,
                )
                time.sleep(wait)
    logger.error(
        "merge gate: on-chain cert read failed after %d attempt(s): %s — refusing merge",
        attempts, last_exc,
    )
    return False


def merge_miner_pr_when_certified(
    pr_number: int,
    expected_head_sha: str,
    *,
    round_id: str | None = None,
    attest_confirmed_sha: str | None = None,
) -> "MergeResult":
    """Squash-merge the miner's fork PR ONLY after the leader's OWN on-chain check.

    Never polls a GitHub status check (fork-spoofable). Steps:
      1. Re-resolve the LIVE head SHA via resolve_pr (TOCTOU). If the head
         drifted off the miner-signed SHA — or the PR was closed — the PR itself
         is UNMERGEABLE (its live state is uncertified), but the certified win
         still stands: fall back to publishing the CERTIFIED tree directly
         (``_publish_certified_tree_despite_pr``). Without the fallback a miner
         could void his own certified round (and block everyone else's
         adoption) just by force-pushing or closing his PR post-certification —
         the round-e29716481-n1 grief.
      2. Refuse if the PR diff touches .github/** (CI-disarm guard).
      3. Assert a quorum cert on-chain binds keccak(head_sha) (the authority).
      4. PUT a squash merge pinned to ``sha=<resolved head>`` so GitHub itself
         rejects on any head drift between the check and the merge.
    Fails loud; never force-merges.
    """
    from minotaur_subnet.api.routes.submissions.github_pr import (
        PRResolutionError,
        resolve_pr,
    )

    owner_repo = _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        logger.error("merge gate: no owner/repo or pr_number — cannot merge")
        return MergeResult(False, "no_pr_number", "merge", "no owner/repo or pr_number")
    owner, repo = owner_repo
    certified = (expected_head_sha or "").strip().lower()

    # 1) TOCTOU — re-resolve the authoritative live head SHA. An unresolvable
    # (closed/vanished) PR is handled like a drifted one below: the certificate
    # binds a SHA, not the PR's live state.
    live_head = ""
    try:
        resolved = resolve_pr(int(pr_number))
        live_head = (resolved.get("head_sha") or "").strip().lower()
    except PRResolutionError as exc:
        logger.warning(
            "merge gate: PR #%s unresolvable (closed/forced/bad base?): %s", pr_number, exc,
        )
    if not live_head and not certified:
        logger.error("merge gate: PR #%s has no resolvable head SHA", pr_number)
        return MergeResult(False, "pr_unresolvable", "merge", "PR has no resolvable head SHA")
    if certified and live_head != certified:
        logger.warning(
            "merge gate: PR #%s live head (%s) is not the certified/signed SHA (%s) — "
            "the PR is unmergeable, publishing the CERTIFIED tree directly instead "
            "(a post-certification push/close can neither invalidate nor replace a "
            "certified win)",
            pr_number, live_head or "<unresolvable>", certified,
        )
        return _publish_certified_tree_despite_pr(
            owner, repo, int(pr_number), certified, round_id,
            attest_confirmed_sha=attest_confirmed_sha,
        )

    # 2) CI-disarm guard.
    if _pr_touches_ci(owner, repo, int(pr_number)):
        logger.error("merge gate: PR #%s diff touches .github/** — refusing (CI-disarm guard)", pr_number)
        return MergeResult(False, "ci_disarm", "merge", "PR diff touches .github/**")

    # 3) The authority: on-chain quorum cert must bind this exact head SHA.
    if not _onchain_cert_binds(live_head, round_id, attest_confirmed_sha=attest_confirmed_sha):
        logger.error(
            "merge gate: no on-chain quorum cert binds head %s (round %s) — refusing merge",
            live_head, round_id,
        )
        return MergeResult(
            False, "no_quorum_cert", "merge",
            "no on-chain quorum cert binds the certified head SHA",
        )

    # 4) Squash-merge pinned to the resolved head (GitHub rejects on drift).
    st, body = _github_api_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{int(pr_number)}/merge",
        {"merge_method": "squash", "sha": live_head},
    )
    if st == 200:
        logger.info("merge gate: PR #%s squash-merged (head %s on-chain-certified)", pr_number, live_head)
        return MergeResult(True)
    logger.error("merge gate: PR #%s merge failed: HTTP %s %s", pr_number, st, body)
    return MergeResult(False, "merge_http_error", "merge", f"GitHub squash-merge HTTP {st}")


def _publish_certified_tree_despite_pr(
    owner: str,
    repo: str,
    pr_number: int,
    certified_sha: str,
    round_id: str | None,
    *,
    attest_confirmed_sha: str | None = None,
) -> "MergeResult":
    """Land a certified PUBLIC win whose PR drifted or closed post-certification.

    The quorum certified ``certified_sha`` (and the fleet verified the image
    built from it) — the PR's live state is irrelevant to that authority. So:

      1. Assert the on-chain quorum cert binds ``certified_sha`` (THE authority —
         same check the normal merge path runs on the live head).
      2. Publish the tree at ``certified_sha`` directly onto canonical ``main``
         via the shared Git-Data publisher. The commit is read through the
         canonical repo's object network (fork-PR commits stay reachable there
         by SHA even after a force-push orphans them). CI-disarm holds by
         construction: the source ``.github/**`` is excluded and canonical's own
         is preserved verbatim — stronger than the ``_pr_touches_ci`` refusal on
         the squash path.
      3. Comment + close the miner's PR UNMERGED (its current head is
         uncertified and never lands).

    The post-certification push/close thus accomplishes nothing: the certified
    code activates anyway. FAIL-CLOSED on any error (adoption then refuses as
    before).
    """
    if not certified_sha:
        return MergeResult(False, "no_certified_sha", "merge", "empty certified SHA")
    if not _onchain_cert_binds(
        certified_sha, round_id, attest_confirmed_sha=attest_confirmed_sha,
    ):
        logger.error(
            "merge gate: no on-chain quorum cert binds certified SHA %s (round %s) "
            "— refusing drift-fallback publish",
            certified_sha, round_id,
        )
        return MergeResult(
            False, "no_quorum_cert", "merge",
            "no on-chain quorum cert binds the certified SHA (drift-fallback publish)",
        )
    published = _publish_certified_tree_to_canonical(
        owner, repo, certified_sha, round_id,
        pr_number=pr_number,
        source_token=None,
        branch_prefix="certified-champion",
        label="certified submission (PR drifted/closed post-certification)",
    )
    if not published:
        return MergeResult(False, "publish_failed", "merge", "certified-tree publish to canonical failed")
    comment_on_pr(
        pr_number,
        "### ⚠️ Certified tree published directly\n\n"
        f"- round: `{round_id}`\n- certified SHA: `{certified_sha}`\n\n"
        "This PR's head changed (or the PR was closed) AFTER the round was "
        "certified, so the PR itself cannot be merged — only the certified SHA "
        "is quorum-approved. The certified tree was published to canonical "
        "`main` directly and the adoption proceeds; the post-certification "
        "changes were NOT included. Submit them in a future round.",
    )
    close_pr(pr_number)
    return MergeResult(True)


def _gh_json(method: str, url: str, payload: dict | None = None, *, token: str | None = None):
    """``_github_api_request`` wrapper that returns ``(ok, json)``; ``ok`` is True
    only on a 2xx. Never raises."""
    status, body = _github_api_request(method, url, payload, token=token)
    return (200 <= int(status or 0) < 300), body


def _private_tree_blobs(
    owner: str, repo: str, head_sha: str, token: str | None,
) -> list[dict] | None:
    """Return the source repo's tree at ``head_sha`` as a flat list of blob
    entries ``{path, mode, sha}``, EXCLUDING anything under ``.github/``
    (CI-disarm: a miner can never introduce/alter canonical CI). ``token`` is
    the miner's per-submission token (private path) or None for the env default
    (public path — fork-PR commits are readable through the canonical repo's
    object network, even after a force-push orphans them). Returns None on any
    API error or if GitHub truncated the tree (fail closed)."""
    ok, body = _gh_json(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{head_sha}?recursive=1",
        token=token,
    )
    if not ok or not isinstance(body, dict):
        logger.error("publish: cannot read private tree %s/%s@%s", owner, repo, head_sha[:12])
        return None
    if body.get("truncated"):
        logger.error("publish: private tree truncated (too large) — refusing")
        return None
    blobs = [
        {"path": e["path"], "mode": e["mode"], "sha": e["sha"]}
        for e in body.get("tree", [])
        if e.get("type") == "blob" and not (e.get("path") or "").startswith(".github/")
    ]
    if not blobs:
        logger.error("publish: private tree has no blobs — refusing")
        return None
    return blobs


def _canonical_github_entries(
    owner: str, repo: str, main_tree_sha: str, *, token: str | None,
) -> list[dict] | None:
    """Return canonical ``main``'s ``.github/**`` blob entries (``{path, mode,
    type, sha}``) so they can be preserved verbatim in the published tree.
    Returns None on API error; an empty list (no .github) is valid."""
    ok, body = _gh_json(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{main_tree_sha}?recursive=1",
        token=token,
    )
    if not ok or not isinstance(body, dict):
        logger.error("publish: cannot read canonical tree %s", main_tree_sha[:12])
        return None
    return [
        {"path": e["path"], "mode": e["mode"], "type": "blob", "sha": e["sha"]}
        for e in body.get("tree", [])
        if e.get("type") == "blob" and (e.get("path") or "").startswith(".github/")
    ]


def publish_private_champion_when_certified(
    pr_number: int,
    expected_head_sha: str,
    round_id: str | None,
    *,
    private_repo: str,
    repo_token: str,
    attest_confirmed_sha: str | None = None,
) -> "MergeResult":
    """Finalize a PRIVATE-submission champion onto canonical ``main`` (leak-on-win).

    A private PR lives in the miner's own repo, so GitHub cannot cross-repo merge
    it. Instead the relayer reconstructs the certified source on the canonical repo
    via the GitHub **Git Data API** (no git CLI, no direct push) and merges it the
    SAME way the public path merges a fork PR (PR + squash-merge with the
    validator's own ``SOLVER_REPO_TOKEN``), so the protect-main ruleset is honored.

    Authority is identical to the public path: the on-chain quorum cert must bind
    ``keccak(head_sha)`` (the cert's ``commit_hash`` IS the private head SHA, so
    ``_onchain_cert_binds`` applies unchanged), and the followers independently
    verified the candidate by pulling the certified image digest. Steps:

      1. Re-resolve the live head via the per-submission token; abort on drift off
         ``expected_head_sha`` (TOCTOU).
      2. Assert the on-chain quorum cert binds this head SHA (THE authority).
      3. Read the private tree at the certified head (miner token), EXCLUDING
         ``.github/**`` (CI-disarm).
      4. Recreate every blob on canonical (canonical token), assemble a tree that
         is the private source + canonical's own ``.github/**`` preserved verbatim,
         commit it onto a fresh ``private-champion/<round>`` branch (parent = current
         ``main``), open a PR, and squash-merge it pinned to the new commit.

    FAIL-CLOSED: any error returns False (the caller's #326 gate then leaves the
    champion unchanged), best-effort cleaning up the branch/PR it created.
    """
    from minotaur_subnet.api.routes.submissions.github_pr import (
        PRResolutionError,
        resolve_pr,
    )

    owner_repo = _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        logger.error("publish: no canonical owner/repo or pr_number")
        return MergeResult(False, "no_pr_number", "merge", "no canonical owner/repo or pr_number")
    c_owner, c_repo = owner_repo
    if "/" not in (private_repo or ""):
        logger.error("publish: malformed private_repo %r", private_repo)
        return MergeResult(False, "malformed_repo", "merge", "malformed private_repo")
    p_owner, p_repo = private_repo.split("/", 1)

    # 1) TOCTOU visibility — re-resolve the live head with the miner token. The
    # CERTIFIED SHA is the publish target either way: a post-certification push
    # or close cannot void a certified win (the drifted content just never gets
    # published); we fail closed only when the certified commit itself is no
    # longer fetchable (miner-controlled repo — a deleted repo/token still
    # refuses, as before).
    certified = (expected_head_sha or "").strip().lower()
    live_head = ""
    try:
        resolved = resolve_pr(int(pr_number), owner_repo=(p_owner, p_repo), token=repo_token)
        live_head = (resolved.get("head_sha") or "").strip().lower()
    except PRResolutionError as exc:
        logger.warning("publish: PR #%s (%s) unresolvable: %s", pr_number, private_repo, exc)
    target = certified or live_head
    if not target:
        logger.error("publish: PR #%s has no certified or resolvable head SHA", pr_number)
        return MergeResult(False, "pr_unresolvable", "merge", "no certified or resolvable head SHA")
    if certified and live_head != certified:
        logger.warning(
            "publish: PR #%s live head (%s) is not the certified SHA (%s) — "
            "publishing the CERTIFIED tree anyway (post-certification drift/close "
            "never voids a certified win)",
            pr_number, live_head or "<unresolvable>", certified,
        )

    # 2) THE authority: on-chain quorum cert must bind this exact SHA (the
    # cert's commit_hash is the private head SHA — same check as the public path).
    if not _onchain_cert_binds(target, round_id, attest_confirmed_sha=attest_confirmed_sha):
        logger.error(
            "publish: no on-chain quorum cert binds head %s (round %s) — refusing",
            target, round_id,
        )
        return MergeResult(
            False, "no_quorum_cert", "merge",
            "no on-chain quorum cert binds the private head SHA",
        )

    # 3+4) Land the certified tree on canonical main (shared Git-Data publisher).
    _published = _publish_certified_tree_to_canonical(
        p_owner, p_repo, target, round_id,
        pr_number=pr_number,
        source_token=repo_token,
        branch_prefix="private-champion",
        label="private submission",
    )
    if not _published:
        return MergeResult(False, "publish_failed", "merge", "private certified-tree publish failed")
    return MergeResult(True)


def _publish_certified_tree_to_canonical(
    source_owner: str,
    source_repo: str,
    certified_sha: str,
    round_id: str | None,
    *,
    pr_number: int,
    source_token: str | None,
    branch_prefix: str,
    label: str,
) -> bool:
    """Land the tree at ``source_repo@certified_sha`` on canonical ``main``.

    The shared Git-Data publisher behind BOTH certified-tree paths (the private
    leak-on-win publish and the public drifted-PR fallback): no git CLI, no
    direct push — blobs are recreated on canonical, assembled into a tree that
    preserves canonical's own ``.github/**`` verbatim (CI-disarm: the source
    tree's ``.github/**`` is excluded by ``_private_tree_blobs``), committed
    onto a fresh ``<branch_prefix>/<round>`` branch (parent = current ``main``,
    deliberately NOT ``champion/*`` so the auto-merge Action ignores it), opened
    as a PR and squash-merged pinned to the new commit — so the protect-main
    ruleset is honored.

    AUTHORITY IS THE CALLER'S JOB: every caller must have asserted the on-chain
    quorum cert binds ``certified_sha`` (``_onchain_cert_binds``) BEFORE calling.
    FAIL-CLOSED: any error returns False, best-effort cleaning up the branch it
    created.
    """
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        logger.error("publish: no canonical owner/repo or pr_number")
        return False
    c_owner, c_repo = owner_repo

    push_token = (
        os.environ.get("SOLVER_REPO_PR_TOKEN") or os.environ.get("SOLVER_REPO_TOKEN") or ""
    ).strip()
    if not push_token:
        logger.error("publish: no SOLVER_REPO_TOKEN to write canonical — refusing")
        return False

    # Read the certified source tree (.github excluded).
    blobs = _private_tree_blobs(source_owner, source_repo, certified_sha, source_token)
    if blobs is None:
        return False

    branch = f"{branch_prefix}/{(round_id or 'round').replace('/', '-')}-{certified_sha[:12]}"
    created_ref = False
    new_pr_number: int | None = None
    try:
        # Canonical main commit + tree (to preserve .github and parent the commit).
        ok, ref_body = _gh_json(
            "GET", f"https://api.github.com/repos/{c_owner}/{c_repo}/git/ref/heads/main",
            token=push_token,
        )
        if not ok or not isinstance(ref_body, dict):
            logger.error("publish: cannot read canonical main ref"); return False
        main_commit_sha = ((ref_body.get("object") or {}).get("sha") or "").strip()
        ok, commit_body = _gh_json(
            "GET",
            f"https://api.github.com/repos/{c_owner}/{c_repo}/git/commits/{main_commit_sha}",
            token=push_token,
        )
        if not ok or not isinstance(commit_body, dict):
            logger.error("publish: cannot read canonical main commit"); return False
        main_tree_sha = ((commit_body.get("tree") or {}).get("sha") or "").strip()

        github_entries = _canonical_github_entries(c_owner, c_repo, main_tree_sha, token=push_token)
        if github_entries is None:
            return False

        # Recreate each source blob on canonical, building the new tree entries.
        tree_entries: list[dict] = []
        for b in blobs:
            ok, blob = _gh_json(
                "GET",
                f"https://api.github.com/repos/{source_owner}/{source_repo}/git/blobs/{b['sha']}",
                token=source_token,
            )
            if not ok or not isinstance(blob, dict):
                logger.error("publish: cannot read source blob %s", b["path"]); return False
            ok, created = _gh_json(
                "POST", f"https://api.github.com/repos/{c_owner}/{c_repo}/git/blobs",
                {"content": blob.get("content", ""), "encoding": blob.get("encoding", "base64")},
                token=push_token,
            )
            if not ok or not isinstance(created, dict) or not created.get("sha"):
                logger.error("publish: cannot create canonical blob %s", b["path"]); return False
            tree_entries.append(
                {"path": b["path"], "mode": b["mode"], "type": "blob", "sha": created["sha"]}
            )
        # Preserve canonical .github/** verbatim — the miner's tree never touches CI.
        tree_entries.extend(github_entries)

        # Build tree (no base_tree → exact mirror: solver source + canonical .github).
        ok, tree = _gh_json(
            "POST", f"https://api.github.com/repos/{c_owner}/{c_repo}/git/trees",
            {"tree": tree_entries}, token=push_token,
        )
        if not ok or not isinstance(tree, dict) or not tree.get("sha"):
            logger.error("publish: cannot create canonical tree"); return False

        # Commit (parent = current main), crediting the source.
        msg = (
            f"champion: {label} via PR #{pr_number} (round {round_id})\n\n"
            f"source: {source_owner}/{source_repo}@{certified_sha}\n"
        )
        ok, commit = _gh_json(
            "POST", f"https://api.github.com/repos/{c_owner}/{c_repo}/git/commits",
            {"message": msg, "tree": tree["sha"], "parents": [main_commit_sha]},
            token=push_token,
        )
        if not ok or not isinstance(commit, dict) or not commit.get("sha"):
            logger.error("publish: cannot create canonical commit"); return False
        new_commit_sha = commit["sha"]

        # Branch (deliberately NOT champion/* so the auto-merge Action ignores it).
        ok, _ = _gh_json(
            "POST", f"https://api.github.com/repos/{c_owner}/{c_repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": new_commit_sha}, token=push_token,
        )
        if not ok:
            # Maybe a stale ref from a previous attempt — force it to the new commit.
            ok, _ = _gh_json(
                "PATCH",
                f"https://api.github.com/repos/{c_owner}/{c_repo}/git/refs/heads/{branch}",
                {"sha": new_commit_sha, "force": True}, token=push_token,
            )
            if not ok:
                logger.error("publish: cannot create/update branch %s", branch); return False
        created_ref = True

        # PR + squash-merge pinned to the new commit (same mechanism as public).
        ok, pr = _gh_json(
            "POST", f"https://api.github.com/repos/{c_owner}/{c_repo}/pulls",
            {"title": f"champion: {label} round {round_id}", "head": branch, "base": "main", "body": msg},
            token=push_token,
        )
        if not ok or not isinstance(pr, dict) or not pr.get("number"):
            logger.error("publish: cannot open canonical PR for %s", branch); return False
        new_pr_number = int(pr["number"])

        ok, _ = _gh_json(
            "PUT",
            f"https://api.github.com/repos/{c_owner}/{c_repo}/pulls/{new_pr_number}/merge",
            {"merge_method": "squash", "sha": new_commit_sha}, token=push_token,
        )
        if not ok:
            logger.error("publish: squash-merge of PR #%s failed", new_pr_number); return False

        logger.info(
            "publish: PR #%s %s published to %s/%s main "
            "(source %s/%s@%s, canonical PR #%s)",
            pr_number, label, c_owner, c_repo, source_owner, source_repo,
            certified_sha, new_pr_number,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never raise into the finalize path
        logger.error("publish: unexpected error: %s", exc)
        return False
    finally:
        # Best-effort cleanup if we didn't land the merge (a successful squash-merge
        # auto-deletes nothing; leave the merged branch for audit, only GC on failure).
        if created_ref:
            # If the merge succeeded GitHub leaves the branch; remove it either way
            # to keep the canonical repo clean (the commit is on main regardless).
            _gh_json(
                "DELETE",
                f"https://api.github.com/repos/{c_owner}/{c_repo}/git/refs/heads/{branch}",
                token=push_token,
            )


def assert_solver_repo_token_not_admin() -> None:
    """HARD-FAIL leader startup if the resolved solver-repo token is admin-scoped.

    An admin token bypasses the protect-main ruleset regardless of empty
    bypass_actors AND can edit/delete the ruleset — so it must never drive merges.
    Requires SOLVER_REPO_PR_TOKEN to be set (refuses the possibly-admin
    SOLVER_REPO_TOKEN fallback) and the bearer's collaborator permission to be
    'write', not 'admin'. Call at leader boot before wiring the adopt/merge path.
    """
    if os.environ.get("ALLOW_ADMIN_SOLVER_REPO_TOKEN", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning("assert_solver_repo_token_not_admin: BYPASSED via ALLOW_ADMIN_SOLVER_REPO_TOKEN (unsafe)")
        return
    pr_tok = os.environ.get("SOLVER_REPO_PR_TOKEN", "").strip()
    if not pr_tok:
        raise RuntimeError(
            "SOLVER_REPO_PR_TOKEN is unset — refusing to arm the adopt/merge path on the "
            "possibly-admin SOLVER_REPO_TOKEN fallback. Provision a non-admin (write-role) "
            "fine-grained PAT scoped to the solver repo (Contents:write + Pull requests:write)."
        )
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None:
        raise RuntimeError("SOLVER_REPO_URL unparseable — cannot verify solver-repo token scope")
    owner, repo = owner_repo
    su, who = _github_api_request("GET", "https://api.github.com/user")
    login = (who or {}).get("login") if isinstance(who, dict) else None
    if su != 200 or not login:
        raise RuntimeError("Could not resolve SOLVER_REPO_PR_TOKEN bearer login — refusing to proceed")
    sp, perm = _github_api_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/collaborators/{login}/permission"
    )
    permission = (perm or {}).get("permission") if isinstance(perm, dict) else None
    if sp != 200 or not permission:
        raise RuntimeError(f"Could not read collaborator permission for {login} on {owner}/{repo} — refusing")
    if permission == "admin":
        raise RuntimeError(
            f"SOLVER_REPO_PR_TOKEN bearer {login} is repo ADMIN — refusing. An admin token "
            "bypasses the protect-main ruleset and can edit/delete it. Use a WRITE-role machine account."
        )
    logger.info("solver-repo token OK: %s permission=%s (non-admin)", login, permission)


# ── Orchestrator (replaces merge_champion_to_main) ───────────────────────────


class MergeResult:
    """Structured champion-adoption outcome: ok + a specific failure reason.

    ``__bool__`` returns ``ok`` so every existing ``bool(result)`` / ``if
    result:`` adoption gate stays correct — a failed result is FALSY even though
    it carries a reason. ``code`` is a stable machine token (e.g.
    ``no_quorum_cert``), ``stage`` is where it failed (``quorum``/``attest``/
    ``merge``), ``detail`` is human text. ``.reason`` aliases ``.code`` for the
    round store's ``abort_reason`` (``merge_failed:<code>``) and older callers.
    """

    __slots__ = ("ok", "code", "stage", "detail", "main_sha")

    def __init__(
        self, ok: bool, code: str = "", stage: str = "", detail: str = "",
        *, main_sha: str = "",
    ) -> None:
        self.ok = bool(ok)
        self.code = str(code or "")
        self.stage = str(stage or "")
        self.detail = str(detail or "")
        # Canonical ``main`` HEAD SHA after a SUCCESSFUL publish — recorded on adoption
        # so the champion-main reconciler can later detect an orphaned merge. Empty on
        # failure or when it couldn't be read.
        self.main_sha = str(main_sha or "")

    @property
    def reason(self) -> str:
        return self.code

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"MergeResult(ok={self.ok}, code={self.code!r}, stage={self.stage!r})"


class FinalizeOutcome:
    """Structured result of the finalize-champion endpoint, serialized per API
    version. Carries the adoption verdict + a specific reason (code/stage/detail)
    and round context, so ``/v2`` returns a clean typed body instead of ``/v1``'s
    loose ``{merge_ok, reason}`` fields. Every validation refusal and the adoption
    outcome funnel through this one type.
    """

    __slots__ = ("ok", "code", "stage", "detail", "round_id", "submission_id")

    def __init__(
        self, ok: bool, code: str = "", stage: str = "", detail: str = "",
        *, round_id: str = "", submission_id: str = "",
    ) -> None:
        self.ok = bool(ok)
        self.code = str(code or "")
        self.stage = str(stage or "")
        self.detail = str(detail or "")
        self.round_id = str(round_id or "")
        self.submission_id = str(submission_id or "")

    @classmethod
    def from_merge(cls, res: Any, *, round_id: str = "", submission_id: str = "") -> "FinalizeOutcome":
        # Tolerate a MergeResult OR a bare bool (legacy/tests): getattr falls back.
        ok = bool(res)
        code = str(getattr(res, "code", "") or ("" if ok else "merge_refused"))
        return cls(
            ok, code, str(getattr(res, "stage", "")), str(getattr(res, "detail", "")),
            round_id=round_id, submission_id=submission_id,
        )

    def to_v1(self) -> dict:
        # Minimal legacy shape — no reason accretion (v1 stays a compat shim).
        return {"merge_ok": self.ok, "round_id": self.round_id, "submission_id": self.submission_id}

    def to_v2(self) -> dict:
        return {
            "ok": self.ok,
            "outcome": "adopted" if self.ok else "refused",
            "round_id": self.round_id,
            "submission_id": self.submission_id,
            "reason": None if self.ok else {
                "code": self.code, "stage": self.stage, "detail": self.detail,
            },
        }


def _canonical_main_head_sha() -> str:
    """Canonical solver-repo ``main`` HEAD commit SHA (empty on any error). Read right
    after a successful publish so the reconciler can record where ``main`` should be for
    the just-adopted champion — works for private champions (the published-to-main
    commit is on canonical, unlike the miner's private commit)."""
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None:
        return ""
    owner, repo = owner_repo
    token = (
        os.environ.get("SOLVER_REPO_PR_TOKEN") or os.environ.get("SOLVER_REPO_TOKEN") or ""
    ).strip() or None
    ok, ref = _gh_json(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/main", token=token,
    )
    if not ok or not isinstance(ref, dict):
        return ""
    return ((ref.get("object") or {}).get("sha") or "").strip()


def on_champion_adopted_pr(
    submission: Any,
    round_id: str | None = None,
    *,
    certificate: Any = None,
) -> "MergeResult":
    """Handle champion adoption: attest on-chain + create GitHub PR.

    Replaces the old merge_champion_to_main() function. The leader no longer
    pushes directly to main — it records proof on BT EVM and opens a PR that
    the GitHub Action verifies and merges.

    Args:
        submission: The adopted Submission object.
        round_id: Solver round identifier.
        certificate: ChampionCertificate with validator approvals.

    Returns:
        True if both attestation and PR creation succeeded.
    """
    commit_hash = getattr(submission, "commit_hash", "") or ""
    submission_id = getattr(submission, "submission_id", "") or ""

    if not commit_hash or commit_hash in ("builtin", ""):
        logger.info("Skipping on-chain attestation for non-git submission: %s", submission_id)
        return MergeResult(False, "non_git_submission", "attest", "non-git submission (nothing to attest/merge)")

    # Step 1: On-chain attestation (retry up to 3 times)
    tx_hash = None
    # Idempotent re-drive: a PRIOR finalize may have already landed the certify()
    # on-chain and then failed downstream (e.g. a transient GitHub 503 at publish).
    # A re-attest of the same round then reverts "Nonce not increasing" (tx_hash
    # stays None), but the win IS certified — so a landed on-chain cert also counts
    # as attested, letting the retry COMPLETE the merge instead of looping to a
    # deadline abort (incident 2026-07-20 round-e29741775).
    _attest_already_onchain = False
    if certificate is not None:
        for attempt in range(3):
            tx_hash = attest_champion_on_chain(certificate, commit_hash)
            if tx_hash:
                break
            if attempt < 2:
                wait = 5 * (attempt + 1)
                logger.warning(
                    "On-chain attestation attempt %d failed, retrying in %ds",
                    attempt + 1, wait,
                )
                time.sleep(wait)

        if not tx_hash:
            # Fall back to the authoritative on-chain read (the SAME quorum-cert
            # check the merge gate uses) — resolving the head SHA exactly as the
            # attest does so keccak(full_sha) matches. Fabricates nothing: a
            # missing/unbound cert leaves this False and the attest a genuine
            # failure, unchanged.
            _full_head = commit_hash
            if len(_full_head) < 40:
                _full_head = _resolve_full_sha(commit_hash) or commit_hash
            if _onchain_cert_binds(_full_head, round_id):
                _attest_already_onchain = True
                logger.info(
                    "Champion re-drive: fresh attest reverted but an on-chain quorum "
                    "cert already binds head %s (round %s) — treating as attested, "
                    "proceeding to publish.",
                    _full_head[:12], round_id,
                )
            else:
                logger.error(
                    "On-chain attestation failed after 3 attempts for %s — "
                    "PR will be created without proof (Action will block merge)",
                    submission_id,
                )
    else:
        logger.warning("No certificate provided — skipping on-chain attestation")

    # Root cause of a failed/absent attestation, carried into abort_reason. An
    # already-on-chain cert (idempotent re-drive) counts as attested, so the
    # downstream merge/publish reason (e.g. publish_failed) is reported instead of
    # a misleading attest_failed — keeping the round deferrable while GitHub recovers.
    if certificate is None:
        _attest_reason = "no_certificate"
    elif not tx_hash and not _attest_already_onchain:
        _attest_reason = "attest_failed"
    else:
        _attest_reason = ""

    # Mirror the ADOPT decision onto the miner's OWN signed fork PR. This is the
    # SINGLE gated path onto main — the legacy create_champion_pr() (a second,
    # leader-pushed champion/<round> branch) is intentionally NOT called: a
    # parallel leader-controlled path would defeat "the on-chain cert is the sole
    # merge authority". The leader posts a report comment, then merges via its
    # OWN on-chain cert re-verification (merge_miner_pr_when_certified), never by
    # trusting a fork-spoofable GitHub status check.
    _pr_number = getattr(submission, "pr_number", None)
    if not _pr_number:
        logger.error(
            "Adopt for %s has no pr_number (not a fork-PR submission) — attest %s, nothing to merge",
            submission_id, tx_hash or "skipped",
        )
        return MergeResult(
            False, _attest_reason or "no_pr_number",
            "attest" if _attest_reason else "merge", "no PR to merge",
        )

    # Private submissions carry their repo + per-submission token (passed through
    # the finalize request); public submissions have neither.
    _is_private = bool(getattr(submission, "is_private", False))
    _private_repo = getattr(submission, "private_repo", None) or getattr(submission, "private_repo_full", None)
    _repo_token = getattr(submission, "repo_token", None)
    _c_owner_repo, _c_tok = (
        ((_private_repo.split("/", 1)[0], _private_repo.split("/", 1)[1]), _repo_token)
        if _is_private and _private_repo and "/" in _private_repo and _repo_token
        else (None, None)
    )

    comment_on_pr(
        _pr_number,
        f"### ✅ Adopted as champion\n\n"
        f"- round: `{round_id}`\n- submission: `{submission_id}`\n"
        f"- on-chain attest tx: `{tx_hash or 'pending'}`\n\n"
        f"The leader will publish to canonical main after its own on-chain cert re-verification.",
        owner_repo=_c_owner_repo,
        token=_c_tok,
    )

    # MERGE AUTHORITY = the leader's OWN web3 cert check (re-resolve head, refuse
    # .github/** diffs, assert quorum cert binds keccak(head_sha)), NOT a GitHub
    # status check. Public: cross-fork squash-merge pinned to the resolved head.
    # Private: clone the miner's tree at the certified head and push it to
    # canonical main (GitHub can't cross-repo merge a private PR).
    # A successful attest (tx_hash != None ⟺ certify() receipt status==1 for this
    # commit_hash) is itself on-chain proof the cert binds this SHA with quorum —
    # hand it to the merge gate so it skips the registry re-read that 429s. None
    # when attest was skipped/failed ⇒ the gate does its authoritative read.
    _attest_confirmed = commit_hash if tx_hash else None
    if _is_private:
        if not (_private_repo and _repo_token):
            logger.error(
                "Adopt for %s is private but missing private_repo/token — refusing", submission_id,
            )
            return MergeResult(False, "private_missing_token", "merge", "private submission missing repo/token")
        merged = publish_private_champion_when_certified(
            _pr_number,
            commit_hash,
            round_id,
            private_repo=_private_repo,
            repo_token=_repo_token,
            attest_confirmed_sha=_attest_confirmed,
        )
    else:
        merged = merge_miner_pr_when_certified(
            _pr_number,
            commit_hash,
            round_id=round_id,
            attest_confirmed_sha=_attest_confirmed,
        )
    if merged:
        # The winner is on main now → every OTHER open submission PR replaces the
        # same solver.py from an older main and is conflicting/un-adoptable until
        # rebased. Close them (with a rebase instruction) so miners resubmit on the
        # new base. Best-effort: never let cleanup affect the adoption result.
        try:
            close_stale_submission_prs(_pr_number, champion_label=f"PR #{_pr_number}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("close-stale failed after champion merge: %s", exc)
    _ok = (bool(tx_hash) or _attest_already_onchain) and bool(merged)
    if _ok:
        # Record where main landed so the reconciler can later detect an orphaned merge.
        _result = MergeResult(True, main_sha=_canonical_main_head_sha())
    elif _attest_reason:                     # attest is the ROOT failure
        _result = MergeResult(
            False, _attest_reason, "attest",
            "on-chain attestation was skipped or failed",
        )
    elif not bool(merged):                   # attest ok → merge gate's specific reason
        _result = merged
    else:
        _result = MergeResult(False, "adopt_failed", "merge")
    logger.info(
        "Champion adoption: attest=%s merge=%s reason=%s pr=#%s round=%s",
        tx_hash or "skipped", bool(merged), _result.code or "-", _pr_number, round_id,
    )
    return _result


# ── Relayer-delegated finalization (third-party leader) ──────────────────────
# When the leader is a third party we don't control, champion FINALIZATION (the
# on-chain attest + the squash-merge) must run on the TRUSTED relayer that holds
# RELAYER_PRIVATE_KEY + SOLVER_REPO_TOKEN — not on the leader. The leader calls
# the relayer's POST /v1/finalize-champion (which re-verifies the validator quorum
# independently) and gates its local adoption on the boolean reply. This mirrors
# the on_champion_adopted_pr signature so it drops straight into the #326 gate.


def _relayer_ready(base: str, *, timeout: float = 5.0) -> bool:
    """Readiness probe for the finalize health-gate: ``GET {base}/health`` → True on a
    2xx. NEVER raises (any error => not ready => the caller defers). The relayer is
    commonly briefly unreachable right after an update.sh recreate — the api can come up
    before the relayer's DNS/port is ready, which is exactly what orphaned the
    2026-07-17 merge."""
    import requests

    try:
        r = requests.get(base.rstrip("/") + "/health", timeout=timeout)
        return 200 <= int(getattr(r, "status_code", 0) or 0) < 300
    except Exception:
        return False


def on_champion_adopted_via_relayer(
    submission: Any,
    round_id: str | None = None,
    *,
    certificate: Any = None,
) -> "MergeResult":
    """Ask the trusted relayer to finalize a certified champion; return its verdict.

    POSTs the certificate + submission identity to ``{RELAYER_URL}/v1/finalize-champion``.
    The relayer independently re-verifies the validator quorum, then attests on-chain
    and squash-merges the miner's PR using ITS OWN keys (the leader never holds them).

    FAIL-CLOSED: any missing config, non-git submission, network error, timeout,
    non-200, or a ``merge_ok != true`` reply returns False — the #326 adoption gate
    then aborts the round (``merge_failed``) and the champion is left unchanged. We
    never adopt on an unconfirmed merge.

    Matches ``on_champion_adopted_pr``'s signature so it slots into the same
    ``EpochManager.on_champion_adopted`` callback.
    """
    import requests

    from minotaur_subnet.consensus.leader_wrapper import (
        compute_champion_finalize_hash,
        sign_wrapper,
    )

    relayer_url = os.environ.get("RELAYER_URL", "").strip()
    if not relayer_url:
        logger.error("on_champion_adopted_via_relayer: RELAYER_URL unset — cannot finalize")
        return MergeResult(False, "relayer_url_unset")

    commit_hash = getattr(submission, "commit_hash", "") or ""
    submission_id = getattr(submission, "submission_id", "") or ""
    if not commit_hash or commit_hash in ("builtin", ""):
        logger.info(
            "Skipping relayer finalization for non-git submission: %s", submission_id,
        )
        return MergeResult(False, "non_git_submission")

    if certificate is None:
        logger.error(
            "on_champion_adopted_via_relayer: no certificate for %s — refusing", submission_id,
        )
        return MergeResult(False, "no_certificate")

    validator_key = os.environ.get("VALIDATOR_PRIVATE_KEY", "").strip()
    if not validator_key:
        logger.error(
            "on_champion_adopted_via_relayer: VALIDATOR_PRIVATE_KEY unset — cannot sign wrapper",
        )
        return MergeResult(False, "no_validator_key")

    rid = str(round_id or "")
    candidate_submission_id = (
        getattr(certificate, "candidate_submission_id", None) or submission_id or ""
    )
    champion_chain_id = int(
        os.environ.get("CHAMPION_CONSENSUS_CHAIN_ID", "964").strip() or "964"
    )

    # Anti-spam wrapper: bind round_id + candidate_submission_id (the relayer
    # recomputes the SAME hash via compute_champion_finalize_hash — keep in sync).
    finalize_hash = compute_champion_finalize_hash(rid, candidate_submission_id)
    wrapper, wrapper_sig = sign_wrapper(
        validator_key,
        plan_hash=finalize_hash,
        submission_nonce=int(time.time()),
        chain_id=champion_chain_id,
    )

    # Private submissions: forward the repo + per-submission token so the relayer
    # can clone the miner's private tree and publish it to canonical main. The
    # token is read from the in-memory store (never persisted) and is NOT part of
    # the wrapper hash, so it doesn't affect the anti-spam signature.
    _is_private = bool(getattr(submission, "is_private", False))
    _submission = {
        "submission_id": submission_id,
        "commit_hash": commit_hash,
        "pr_number": getattr(submission, "pr_number", None),
    }
    if _is_private:
        _repo_token = getattr(submission, "repo_token", None)
        if not _repo_token:
            try:
                from minotaur_subnet.api.routes.submissions.state import get_store
                _repo_token = get_store().get_repo_token(submission_id)
            except Exception as exc:  # store unavailable in this process
                logger.error("relayer-finalize: cannot read private token for %s: %s", submission_id, exc)
                _repo_token = None
        if not _repo_token:
            logger.error(
                "relayer-finalize: private submission %s has no token — FAIL-CLOSED", submission_id,
            )
            return MergeResult(False, "private_missing_token")
        _submission["is_private"] = True
        _submission["private_repo"] = (
            getattr(submission, "private_repo_full", None)
            or getattr(submission, "private_repo", None)
        )
        _submission["repo_token"] = _repo_token

    body = {
        "certificate": certificate.to_dict(),
        "submission": _submission,
        "round_id": rid,
        "wrapper": {
            "plan_hash": wrapper.plan_hash,
            "submission_nonce": wrapper.submission_nonce,
            "timestamp": wrapper.timestamp,
            "chain_id": wrapper.chain_id,
        },
        "wrapper_signature": wrapper_sig,
    }

    base = relayer_url.rstrip("/")

    # Health-gate: probe the relayer's /health BEFORE POSTing the finalize. If it isn't
    # ready — commonly a brief window right after an update.sh recreate where the api
    # comes up before the relayer's DNS/port is ready, which is exactly what orphaned
    # the 2026-07-17 merge — return stage="client" so the #326 merge-gate DEFERS (not
    # aborts). The coordinator's re-drive cadence IS the retry, so we never block the
    # event loop with an in-line sleep. Disable with RELAYER_HEALTH_GATE=0.
    if (os.environ.get("RELAYER_HEALTH_GATE", "1").strip() or "1") != "0":
        if not _relayer_ready(base):
            logger.warning(
                "on_champion_adopted_via_relayer: relayer at %s not ready (/health) — "
                "stage=client so the merge-gate DEFERS; the coordinator re-drives once "
                "the relayer is reachable (not aborting a possibly-landed finalize).",
                base,
            )
            return MergeResult(False, "relayer_unready", "client", "relayer /health not ready")

    # Prefer the structured v2 contract; fall back to v1 for a relayer still on an
    # older image (the api + relayer recreate seconds apart during an update, so a
    # brief version skew is possible). Generous timeout: the on-chain attest (up to
    # 3 retries) + squash-merge can take a while.
    try:
        resp = requests.post(base + "/v2/finalize-champion", json=body, timeout=180)
        if resp.status_code == 404:
            resp = requests.post(base + "/v1/finalize-champion", json=body, timeout=180)
    except Exception as exc:
        logger.error(
            "on_champion_adopted_via_relayer: POST %s failed (%s) — FAIL-CLOSED (no adopt)",
            base, exc,
        )
        return MergeResult(False, "relayer_unreachable", "client", str(exc))

    if resp.status_code != 200:
        logger.error(
            "on_champion_adopted_via_relayer: relayer HTTP %s for round=%s — FAIL-CLOSED",
            resp.status_code, rid,
        )
        return MergeResult(False, f"relayer_http_{resp.status_code}", "client", "non-200 from relayer")

    try:
        payload = resp.json()
    except Exception as exc:
        logger.error(
            "on_champion_adopted_via_relayer: bad JSON reply (%s) — FAIL-CLOSED", exc,
        )
        return MergeResult(False, "relayer_bad_reply", "client", str(exc))

    # v2 reply: {"ok", "reason": {"code","stage","detail"}}. v1 fallback: {"merge_ok"}.
    if "ok" in payload:
        ok = bool(payload.get("ok"))
        r = payload.get("reason") or {}
        code = str((r.get("code") if isinstance(r, dict) else r) or ("" if ok else "merge_refused"))
        stage = str(r.get("stage", "")) if isinstance(r, dict) else ""
        detail = str(r.get("detail", "")) if isinstance(r, dict) else ""
    else:
        ok = bool(payload.get("merge_ok"))
        code, stage, detail = ("" if ok else "merge_refused"), "", ""
    logger.info(
        "Champion finalization via relayer: round=%s submission=%s ok=%s reason=%s",
        rid, submission_id, ok, code or "-",
    )
    # On success, record where ``main`` landed so the reconciler can later detect an
    # orphaned merge. Prefer a relayer-provided ``main_sha`` if present; else read
    # canonical main HEAD ourselves (works for private champions — canonical commit).
    _main_sha = str(payload.get("main_sha") or "") if isinstance(payload, dict) else ""
    if not _main_sha and ok:
        _main_sha = _canonical_main_head_sha()
    return MergeResult(ok, code, stage, detail, main_sha=_main_sha)


# ── Legacy compat ────────────────────────────────────────────────────────────
# Keep the old function name as an alias so existing imports don't break.
# It delegates to the new PR-based flow.

def merge_champion_to_main(
    submission: Any,
    round_id: str | None = None,
    **kwargs: Any,
) -> bool:
    """Legacy alias — delegates to on_champion_adopted_pr."""
    return bool(on_champion_adopted_pr(
        submission,
        round_id,
        certificate=kwargs.get("certificate"),
    ))
