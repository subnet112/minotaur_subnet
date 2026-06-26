"""Resolve a minotaur-solver PR number to its fork clone_url + head SHA.

Part of the PR-based submission fold: a miner submits by opening a PR on the
PUBLIC ``subnet112/minotaur-solver`` repo and notifying the leader with
``{pr_number, head_sha}``. The leader resolves the PR here — the base host is
FIXED to the canonical solver repo (no miner-supplied ``repo_url``, so the old
arbitrary-repo SSRF surface is gone) and we read the authoritative head SHA + the
fork clone URL to build from. The caller then checks the resolved head SHA equals
the miner-signed ``head_sha`` (force-push / TOCTOU guard).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_SOLVER_REPO = ("subnet112", "minotaur-solver")
_HEX = set("0123456789abcdef")


class PRResolutionError(Exception):
    """A PR could not be resolved to a safe, open, canonical-base PR."""


def canonical_solver_repo() -> tuple[str, str]:
    """The ``(owner, repo)`` a submission PR must target — fixed to the solver repo.

    Derived from ``SOLVER_REPO_URL`` when set, else the SN112 default. This is the
    ONLY repo a submission can reference, which removes the arbitrary-``repo_url``
    SSRF surface the old ``{repo_url, commit_hash}`` submission had.
    """
    url = (os.environ.get("SOLVER_REPO_URL") or "").strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            path = url[len(prefix):].removesuffix(".git")
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
    return DEFAULT_SOLVER_REPO


def _github_headers() -> dict[str, str]:
    token = (
        os.environ.get("SOLVER_REPO_PR_TOKEN")
        or os.environ.get("SOLVER_REPO_TOKEN")
        or ""
    ).strip()
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fetch_pr(owner: str, repo: str, pr_number: int, *, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers=_github_headers(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        return json.loads(resp.read().decode("utf-8"))


def resolve_pr(pr_number: int, *, fetch=_fetch_pr) -> dict:
    """Resolve a canonical-solver-repo PR to ``{clone_url, head_sha, state, base}``.

    Validates: the PR is OPEN; its base repo is the canonical solver repo (a miner
    cannot point the base at a repo we do not control); the head fork ``clone_url``
    is an ``https://github.com/`` URL (rejects a deleted fork, where ``head.repo``
    is null, and any non-github host). Raises :class:`PRResolutionError` otherwise.
    ``fetch`` is injectable for tests.
    """
    owner, repo = canonical_solver_repo()
    try:
        data = fetch(owner, repo, pr_number)
    except PRResolutionError:
        raise
    except Exception as exc:  # network / 404 / json
        raise PRResolutionError(f"could not fetch PR #{pr_number}: {exc}") from exc

    state = (data.get("state") or "").lower()
    if state != "open":
        raise PRResolutionError(f"PR #{pr_number} is not open (state={state!r})")

    base_full = ((data.get("base") or {}).get("repo") or {}).get("full_name") or ""
    if base_full.lower() != f"{owner}/{repo}".lower():
        raise PRResolutionError(
            f"PR #{pr_number} base is {base_full!r}, not the canonical {owner}/{repo}"
        )

    head = data.get("head") or {}
    head_sha = (head.get("sha") or "").strip().lower()
    if len(head_sha) != 40 or any(c not in _HEX for c in head_sha):
        raise PRResolutionError(f"PR #{pr_number} head sha is malformed: {head_sha!r}")

    head_repo = head.get("repo") or {}
    clone_url = (head_repo.get("clone_url") or "").strip()
    if not clone_url.startswith("https://github.com/"):
        raise PRResolutionError(
            f"PR #{pr_number} head clone_url missing/non-github (fork deleted?): {clone_url!r}"
        )
    # The fork owner's GitHub login — the account that hosts the head commit. Checked
    # against the registered GitHub-account↔hotkey binding so a miner can't submit a
    # PR from an account they don't own (see api/services/miner_identity).
    fork_owner = ((head_repo.get("owner") or {}).get("login") or "").strip()

    return {
        "clone_url": clone_url,
        "head_sha": head_sha,
        "state": state,
        "base": base_full,
        "fork_owner": fork_owner,
    }


def _fetch_gist(gist_id: str, *, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        headers=_github_headers(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        return json.loads(resp.read().decode("utf-8"))


def resolve_gist(gist_id: str, *, fetch=_fetch_gist) -> tuple[str, str]:
    """Resolve a public gist to ``(owner_login, first_file_content)``.

    The owner login comes from GitHub's authoritative response, so a registrant
    can't claim a gist they don't own (only the owning account can host a gist under
    it) — that ownership is what proves control of the GitHub account. ``fetch`` is
    injectable for tests. Raises :class:`PRResolutionError`.
    """
    gid = (gist_id or "").strip()
    if not gid or not gid.isalnum() or len(gid) > 64:
        raise PRResolutionError(f"malformed gist id: {gist_id!r}")
    try:
        data = fetch(gid)
    except Exception as exc:  # network / 404 / json
        raise PRResolutionError(f"could not fetch gist {gid}: {exc}") from exc

    owner_login = ((data.get("owner") or {}).get("login") or "").strip()
    if not owner_login:
        raise PRResolutionError(f"gist {gid} has no owner login")
    files = data.get("files") or {}
    first = next(iter(files.values()), None) or {}
    content = (first.get("content") or "").strip()
    if not content:
        raise PRResolutionError(f"gist {gid} has no readable file content")
    return owner_login, content
