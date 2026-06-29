"""Unit tests for the PR-based submission resolver (github_pr)."""

import pytest

from minotaur_subnet.api.routes.submissions import github_pr as gp

OWNER, REPO = gp.DEFAULT_SOLVER_REPO
HEAD = "a" * 40
FORK = "https://github.com/miner/minotaur-solver.git"


def _pr(*, state="open", base=f"{OWNER}/{REPO}", head_sha=HEAD, clone_url=FORK, head_repo=True,
        mergeable=True, mergeable_state="clean", draft=False):
    head = {"sha": head_sha, "repo": ({"clone_url": clone_url} if head_repo else None)}
    return {"state": state, "base": {"repo": {"full_name": base}}, "head": head,
            "mergeable": mergeable, "mergeable_state": mergeable_state, "draft": draft}


def _fetch(pr):
    return lambda owner, repo, n: pr


def test_github_owner_from_url():
    # Owner extracted + lowercased from the clone/repo URL forms; None for non-github.
    assert gp.github_owner_from_url("https://github.com/Miner/fork.git") == "miner"
    assert gp.github_owner_from_url("https://github.com/Alice/solver") == "alice"
    assert gp.github_owner_from_url("git@github.com:Bob/repo.git") == "bob"
    assert gp.github_owner_from_url("ssh://git@github.com/Carol/r") == "carol"
    assert gp.github_owner_from_url("source://inline") is None
    assert gp.github_owner_from_url("https://gitlab.com/x/y") is None
    assert gp.github_owner_from_url("") is None
    assert gp.github_owner_from_url(None) is None


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
    assert out == {"clone_url": FORK, "head_sha": HEAD, "state": "open", "base": f"{OWNER}/{REPO}"}


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


# ── assess_pr_mergeability (fail-fast submit gate) ───────────────────────────


def _no_sleep(_):  # don't actually sleep on the mergeable=null retry in tests
    pass


def test_mergeable_clean_ok():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(mergeable=True, mergeable_state="clean")), _sleep=_no_sleep,
    )
    assert ok and reason is None


def test_mergeable_conflicts_rejected():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(mergeable=False, mergeable_state="dirty")), _sleep=_no_sleep,
    )
    assert not ok and "conflicts" in reason.lower()


def test_dirty_state_rejected_even_when_mergeable_null():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(mergeable=None, mergeable_state="dirty")), _sleep=_no_sleep,
    )
    assert not ok and "conflicts" in reason.lower()


def test_behind_main_rejected():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(mergeable=True, mergeable_state="behind")), _sleep=_no_sleep,
    )
    assert not ok and "behind" in reason.lower()


def test_draft_rejected():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(draft=True)), _sleep=_no_sleep,
    )
    assert not ok and "draft" in reason.lower()


def test_null_then_clean_retries_once_and_passes():
    seq = [
        _pr(mergeable=None, mergeable_state="unknown"),  # GitHub still computing
        _pr(mergeable=True, mergeable_state="clean"),     # computed on re-fetch
    ]
    calls = {"n": 0}

    def fetch(owner, repo, n):
        out = seq[calls["n"]]
        calls["n"] += 1
        return out

    ok, reason = gp.assess_pr_mergeability(7, fetch=fetch, _sleep=_no_sleep)
    assert ok and reason is None
    assert calls["n"] == 2  # retried exactly once


def test_persistent_null_does_not_block():
    ok, reason = gp.assess_pr_mergeability(
        7, fetch=_fetch(_pr(mergeable=None, mergeable_state="unknown")), _sleep=_no_sleep,
    )
    assert ok and reason is None  # transient/uncertain → never hard-block


def test_fetch_error_does_not_block():
    def boom(owner, repo, n):
        raise RuntimeError("github 502")

    ok, reason = gp.assess_pr_mergeability(7, fetch=boom, _sleep=_no_sleep)
    assert ok and reason is None  # the merge gate is the backstop, not this check


def test_blocked_and_unstable_are_allowed():
    # blocked (required reviews/checks) + unstable (failing non-required checks)
    # are NOT merge-blockers for our leader-authority squash-merge → allow.
    for st in ("blocked", "unstable", "clean", "has_hooks"):
        ok, _ = gp.assess_pr_mergeability(
            7, fetch=_fetch(_pr(mergeable=True, mergeable_state=st)), _sleep=_no_sleep,
        )
        assert ok, st
