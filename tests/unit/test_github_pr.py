"""Unit tests for the PR-based submission resolver (github_pr)."""

import pytest

from minotaur_subnet.api.routes.submissions import github_pr as gp

OWNER, REPO = gp.DEFAULT_SOLVER_REPO
HEAD = "a" * 40
FORK = "https://github.com/miner/minotaur-solver.git"


def _pr(*, state="open", base=f"{OWNER}/{REPO}", head_sha=HEAD, clone_url=FORK,
        head_repo=True, fork_owner="miner"):
    repo = {"clone_url": clone_url, "owner": {"login": fork_owner}} if head_repo else None
    head = {"sha": head_sha, "repo": repo}
    return {"state": state, "base": {"repo": {"full_name": base}}, "head": head}


def _fetch(pr):
    return lambda owner, repo, n: pr


def test_canonical_repo_default(monkeypatch):
    monkeypatch.delenv("SOLVER_REPO_URL", raising=False)
    assert gp.canonical_solver_repo() == (OWNER, REPO)


def test_canonical_repo_from_env(monkeypatch):
    monkeypatch.setenv("SOLVER_REPO_URL", "https://github.com/acme/solver.git")
    assert gp.canonical_solver_repo() == ("acme", "solver")
    monkeypatch.setenv("SOLVER_REPO_URL", "git@github.com:acme/solver")
    assert gp.canonical_solver_repo() == ("acme", "solver")


def test_resolve_happy_path():
    out = gp.resolve_pr(7, fetch=_fetch(_pr()))
    assert out == {
        "clone_url": FORK, "head_sha": HEAD, "state": "open",
        "base": f"{OWNER}/{REPO}", "fork_owner": "miner",
    }


def test_resolve_normalizes_head_sha_case():
    out = gp.resolve_pr(7, fetch=_fetch(_pr(head_sha=HEAD.upper())))
    assert out["head_sha"] == HEAD  # lowercased


def test_resolve_rejects_closed_pr():
    with pytest.raises(gp.PRResolutionError, match="not open"):
        gp.resolve_pr(7, fetch=_fetch(_pr(state="closed")))


def test_resolve_rejects_non_canonical_base():
    with pytest.raises(gp.PRResolutionError, match="not the canonical"):
        gp.resolve_pr(7, fetch=_fetch(_pr(base="attacker/evil")))


def test_resolve_rejects_malformed_head_sha():
    with pytest.raises(gp.PRResolutionError, match="malformed"):
        gp.resolve_pr(7, fetch=_fetch(_pr(head_sha="deadbeef")))  # too short


def test_resolve_rejects_deleted_fork():
    # head.repo is null when the fork was deleted but the PR still resolves
    with pytest.raises(gp.PRResolutionError, match="fork deleted"):
        gp.resolve_pr(7, fetch=_fetch(_pr(head_repo=False)))


def test_resolve_rejects_non_github_clone_url():
    with pytest.raises(gp.PRResolutionError, match="non-github"):
        gp.resolve_pr(7, fetch=_fetch(_pr(clone_url="https://evil.example/x.git")))


def test_resolve_wraps_fetch_errors():
    def boom(owner, repo, n):
        raise RuntimeError("404 not found")

    with pytest.raises(gp.PRResolutionError, match="could not fetch"):
        gp.resolve_pr(7, fetch=boom)
