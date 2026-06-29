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

import functools
import json
import logging
import os
import time
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


def github_owner_from_url(url: str | None) -> str | None:
    """Extract the GitHub account (owner login) from a clone/repo URL, lowercased.

    ``https://github.com/<owner>/<repo>(.git)`` (and the ``git@``/``ssh://`` forms)
    → ``owner``. Returns None for a non-github or inline (``source://``) URL.
    Lowercased because GitHub logins are case-insensitive — so ``Alice`` and
    ``alice`` dedup as one account. Used to key the per-(account, round) cap on the
    resolved PR head-repo owner.
    """
    u = (url or "").strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if u.startswith(prefix):
            path = u[len(prefix):].removesuffix(".git")
            parts = path.split("/")
            if parts and parts[0]:
                return parts[0].lower()
    return None


def _github_headers(token: str | None = None) -> dict[str, str]:
    """Auth headers for the GitHub API.

    ``token`` (the private path's per-submission PAT) wins; otherwise we fall
    back to the validator's canonical-repo token from the environment.
    """
    tok = (
        token
        or os.environ.get("SOLVER_REPO_PR_TOKEN")
        or os.environ.get("SOLVER_REPO_TOKEN")
        or ""
    ).strip()
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _fetch_pr(
    owner: str, repo: str, pr_number: int, *, timeout: float = 15.0, token: str | None = None,
) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers=_github_headers(token),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        return json.loads(resp.read().decode("utf-8"))


def resolve_pr(pr_number: int, *, fetch=_fetch_pr, owner_repo=None, token=None) -> dict:
    """Resolve a solver-repo PR to ``{clone_url, head_sha, state, base}``.

    Public path (``owner_repo=None``): the base MUST be the canonical solver repo
    (a miner cannot point the base at a repo we do not control), authed with the
    validator's environment token.

    Private path (``owner_repo=(owner, repo)`` + ``token``): the base MUST equal
    the miner's declared private repo, authed with the per-submission PAT.

    Always validates the PR is OPEN and the head ``clone_url`` is an
    ``https://github.com/`` URL (rejects a deleted fork / non-github host).
    Raises :class:`PRResolutionError` otherwise. ``fetch`` is injectable for tests.
    """
    owner, repo = owner_repo or canonical_solver_repo()
    # Thread the per-submission token into the default fetcher without changing
    # the (owner, repo, pr_number) call shape that injected test doubles rely on.
    if token is not None and fetch is _fetch_pr:
        fetch = functools.partial(_fetch_pr, token=token)
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
    expected_base = f"{owner}/{repo}"
    if base_full.lower() != expected_base.lower():
        kind = "declared private repo" if owner_repo else "canonical"
        raise PRResolutionError(
            f"PR #{pr_number} base is {base_full!r}, not the {kind} {expected_base}"
        )

    head = data.get("head") or {}
    head_sha = (head.get("sha") or "").strip().lower()
    if len(head_sha) != 40 or any(c not in _HEX for c in head_sha):
        raise PRResolutionError(f"PR #{pr_number} head sha is malformed: {head_sha!r}")

    clone_url = ((head.get("repo") or {}).get("clone_url") or "").strip()
    if not clone_url.startswith("https://github.com/"):
        raise PRResolutionError(
            f"PR #{pr_number} head clone_url missing/non-github (fork deleted?): {clone_url!r}"
        )

    return {"clone_url": clone_url, "head_sha": head_sha, "state": state, "base": base_full}


def assess_pr_mergeability(
    pr_number: int, *, fetch=_fetch_pr, _sleep=time.sleep, owner_repo=None, token=None,
) -> tuple[bool, str | None]:
    """Check whether a PR can actually be merged — for fail-fast feedback at submit.

    Returns ``(ok, reason)``. ``ok`` is ``False`` with a miner-facing message when
    the PR can't be merged — merge conflicts (usually a stale base after a newer
    champion landed on main), behind main, or a draft — so we don't spend a
    benchmark on a PR the leader's merge gate would later reject anyway.

    GitHub computes ``mergeable`` ASYNCHRONOUSLY, so a freshly-pushed PR often
    returns ``mergeable=null`` on the first read; we re-fetch once before deciding.
    A persistent ``null``/``unknown`` (or any GitHub error) is treated as OK — we
    never hard-block a submission on a transient/uncertain signal; the leader's
    on-chain-certified merge gate remains the authoritative backstop.
    ``fetch``/``_sleep`` are injectable for tests.
    """
    owner, repo = owner_repo or canonical_solver_repo()
    if token is not None and fetch is _fetch_pr:
        fetch = functools.partial(_fetch_pr, token=token)
    for attempt in range(2):
        try:
            data = fetch(owner, repo, pr_number)
        except Exception:
            return True, None  # transient GitHub error → don't block; merge gate backstops
        if data.get("draft"):
            return False, (
                f"PR #{pr_number} is a draft — mark it 'Ready for review', then resubmit."
            )
        mergeable = data.get("mergeable")
        mstate = (data.get("mergeable_state") or "").lower()
        if mergeable is False or mstate == "dirty":
            return False, (
                f"PR #{pr_number} has merge conflicts with main — a newer champion likely "
                f"landed on main. Rebase your branch onto the current main and resubmit so "
                f"we benchmark exactly what would be merged."
            )
        if mstate == "behind":
            return False, (
                f"PR #{pr_number} is behind main (the champion advanced since you opened it). "
                f"Rebase onto the current main and resubmit — a stale PR can't be merged as-is."
            )
        if mergeable is None and attempt == 0:
            # GitHub is still computing mergeability — give it a moment, then re-fetch once.
            _sleep(2.0)
            continue
        return True, None
    return True, None
