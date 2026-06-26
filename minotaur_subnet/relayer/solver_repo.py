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
            "gas": 500_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        })

        signed = w3.eth.account.sign_transaction(tx, relayer_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            logger.error(
                "ChampionRegistry.certify() reverted: tx=%s",
                tx_hash.hex(),
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


def _github_api_headers() -> dict[str, str]:
    """Build GitHub API headers using the PR token."""
    token = os.environ.get(
        "SOLVER_REPO_PR_TOKEN",
        os.environ.get("SOLVER_REPO_TOKEN", ""),
    ).strip()
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

def _github_api_request(method: str, url: str, payload: dict | None = None) -> tuple[int, dict | None]:
    """Issue a GitHub API request. Returns (status, json|None); never raises."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_github_api_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed github host
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        logger.warning("GitHub %s %s -> %s: %s", method, url, exc.code, exc.reason)
        return exc.code, None
    except Exception as exc:  # network / json
        logger.warning("GitHub %s %s failed: %s", method, url, exc)
        return 0, None


def comment_on_pr(pr_number: int, body: str) -> bool:
    """Post a comment on a solver-repo PR (used for the scoring report)."""
    owner_repo = _parse_github_owner_repo()
    if owner_repo is None or not pr_number:
        return False
    owner, repo = owner_repo
    status, _ = _github_api_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        {"body": body},
    )
    return status in (200, 201)


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


def _render_reject_body(
    submission: Any,
    reason: str,
    champion_score: float | None,
    dethrone_margin: float | None,
    champion_details: dict | None = None,
) -> str:
    """PR-comment body for a rejection: the full scored benchmark report when the
    submission was benchmarked (with the champion per-case comparison and any
    revert traces), else the concise reason. Never raises."""
    fallback = f"### ❌ Submission rejected\n\n{reason}"
    try:
        from minotaur_subnet.api.routes.submissions.report import (
            build_submission_report,
            render_report_md,
        )
        from minotaur_subnet.epoch.adopt_rule import PER_APP_MIN_SCORE
        from minotaur_subnet.epoch.manager import DETHRONE_MARGIN

        report = build_submission_report(
            submission,
            champion_score=champion_score,
            threshold=PER_APP_MIN_SCORE,
            dethrone_margin=(dethrone_margin if dethrone_margin is not None else DETHRONE_MARGIN),
            reason=reason,
            champion_details=champion_details,
        )
        if not report:
            return fallback
        # Only enrich when there's real benchmark detail to show; a screening or
        # otherwise-empty report falls back to the concise message.
        agg = report.get("aggregate") or {}
        if not (report.get("per_case") or agg.get("your_score") is not None):
            return fallback
        md = render_report_md(report, submission_id=getattr(submission, "submission_id", None))
        return md or fallback
    except Exception as exc:
        logger.warning("PR rejection report render failed: %s", exc)
        return fallback


def on_champion_rejected_pr(
    submission: Any,
    reason: str,
    report_md: str | None = None,
    *,
    champion_score: float | None = None,
    dethrone_margin: float | None = None,
    champion_details: dict | None = None,
) -> bool:
    """REJECT path: comment the reason + scored report on the miner's PR, close
    it, and GC the candidate image. Mirrors the off-chain quorum's reject
    decision onto the PR. Usable while adoption is frozen — pure miner feedback,
    no chain writes.

    When ``report_md`` isn't supplied, builds the full per-case benchmark report
    (your score vs the champion per case, the dethrone gap, every case
    worst-first, and per-step revert traces) from the submission, given
    ``champion_score`` / ``dethrone_margin`` / ``champion_details``."""
    pr_number = getattr(submission, "pr_number", None)
    if not pr_number:
        logger.info(
            "Champion reject for %s has no pr_number — skipping PR close",
            getattr(submission, "submission_id", "?"),
        )
        return False
    body = report_md or _render_reject_body(
        submission, reason, champion_score, dethrone_margin, champion_details,
    )
    commented = comment_on_pr(pr_number, body)
    closed = close_pr(pr_number)
    gced = delete_candidate_image(pr_number)
    logger.info(
        "Champion reject PR#%s: comment=%s close=%s gc=%s",
        pr_number, commented, closed, gced,
    )
    return closed


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
        w3 = Web3(Web3.HTTPProvider(rpc))
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


def _onchain_cert_binds(head_sha: str, round_id: str | None) -> bool:
    """Leader's OWN authority check: does a quorum cert on-chain bind this head SHA?

    Asserts exists AND commitHash == keccak(utf8(lowercase head_sha)) AND
    approvalCount >= getQuorumRequired(). Tries getLatestChampion() first (the
    common case — the leader attests immediately before merging), then falls back
    to getChampion(round_id) if latest has already moved past this round. The PR
    body/comments are NEVER an input — only the on-chain record.
    """
    reg = _read_champion_registry()
    if reg is None:
        logger.error("merge gate: ChampionRegistry unreadable — refusing merge")
        return False
    target = _str_to_bytes32(head_sha.strip().lower())  # == keccak(utf8(head_sha))
    try:
        quorum = int(reg.functions.getQuorumRequired().call())
        if quorum < 1:
            logger.error("merge gate: on-chain quorum %s < 1 — refusing (fail-closed)", quorum)
            return False

        def _binds(rec: Any) -> bool:
            # ChampionRecord = (roundId, candidateSubmissionId, candidateImageId,
            # commitHash[3], effectiveEpoch, certifiedAt, approvalCount[6], exists[7])
            return bool(rec[7]) and rec[3] == target and int(rec[6]) >= quorum

        if _binds(reg.functions.getLatestChampion().call()):
            return True
        if round_id:
            return _binds(reg.functions.getChampion(_str_to_bytes32(round_id)).call())
        return False
    except Exception as exc:
        logger.error("merge gate: on-chain cert read failed: %s — refusing merge", exc)
        return False


def merge_miner_pr_when_certified(
    pr_number: int,
    expected_head_sha: str,
    *,
    round_id: str | None = None,
) -> bool:
    """Squash-merge the miner's fork PR ONLY after the leader's OWN on-chain check.

    Never polls a GitHub status check (fork-spoofable). Steps:
      1. Re-resolve the LIVE head SHA via resolve_pr (TOCTOU); abort if it drifted
         off the miner-signed SHA.
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
        return False
    owner, repo = owner_repo

    # 1) TOCTOU — re-resolve the authoritative live head SHA.
    try:
        resolved = resolve_pr(int(pr_number))
    except PRResolutionError as exc:
        logger.error("merge gate: PR #%s unresolvable (closed/forced/bad base?): %s", pr_number, exc)
        return False
    live_head = (resolved.get("head_sha") or "").strip().lower()
    if not live_head:
        logger.error("merge gate: PR #%s has no resolvable head SHA", pr_number)
        return False
    if expected_head_sha and live_head != expected_head_sha.strip().lower():
        logger.error(
            "merge gate: PR #%s head drifted (%s) off the certified/signed SHA (%s) — refusing",
            pr_number, live_head, expected_head_sha.strip().lower(),
        )
        return False

    # 2) CI-disarm guard.
    if _pr_touches_ci(owner, repo, int(pr_number)):
        logger.error("merge gate: PR #%s diff touches .github/** — refusing (CI-disarm guard)", pr_number)
        return False

    # 3) The authority: on-chain quorum cert must bind this exact head SHA.
    if not _onchain_cert_binds(live_head, round_id):
        logger.error(
            "merge gate: no on-chain quorum cert binds head %s (round %s) — refusing merge",
            live_head, round_id,
        )
        return False

    # 4) Squash-merge pinned to the resolved head (GitHub rejects on drift).
    st, body = _github_api_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{int(pr_number)}/merge",
        {"merge_method": "squash", "sha": live_head},
    )
    if st == 200:
        logger.info("merge gate: PR #%s squash-merged (head %s on-chain-certified)", pr_number, live_head)
        return True
    logger.error("merge gate: PR #%s merge failed: HTTP %s %s", pr_number, st, body)
    return False


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


def on_champion_adopted_pr(
    submission: Any,
    round_id: str | None = None,
    *,
    certificate: Any = None,
) -> bool:
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
        return False

    # Step 1: On-chain attestation (retry up to 3 times)
    tx_hash = None
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
            logger.error(
                "On-chain attestation failed after 3 attempts for %s — "
                "PR will be created without proof (Action will block merge)",
                submission_id,
            )
    else:
        logger.warning("No certificate provided — skipping on-chain attestation")

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
        return False

    comment_on_pr(
        _pr_number,
        f"### ✅ Adopted as champion\n\n"
        f"- round: `{round_id}`\n- submission: `{submission_id}`\n"
        f"- on-chain attest tx: `{tx_hash or 'pending'}`\n\n"
        f"The leader will squash-merge after its own on-chain cert re-verification.",
    )

    # MERGE AUTHORITY = the leader's OWN web3 cert check (re-resolve head, refuse
    # .github/** diffs, assert quorum cert binds keccak(head_sha)), NOT a GitHub
    # status check. Pins the squash merge to the resolved head SHA.
    merged = merge_miner_pr_when_certified(
        _pr_number,
        commit_hash,
        round_id=round_id,
    )
    logger.info(
        "Champion adoption: attest=%s merge=%s pr=#%s round=%s",
        tx_hash or "skipped", merged, _pr_number, round_id,
    )
    return bool(tx_hash) and merged


# ── Legacy compat ────────────────────────────────────────────────────────────
# Keep the old function name as an alias so existing imports don't break.
# It delegates to the new PR-based flow.

def merge_champion_to_main(
    submission: Any,
    round_id: str | None = None,
    **kwargs: Any,
) -> bool:
    """Legacy alias — delegates to on_champion_adopted_pr."""
    return on_champion_adopted_pr(
        submission,
        round_id,
        certificate=kwargs.get("certificate"),
    )
