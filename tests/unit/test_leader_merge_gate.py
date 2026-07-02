"""Unit tests for the leader-authority champion merge gate (fork-PR redesign).

MERGE AUTHORITY is the leader's OWN on-chain verification (NOT a GitHub status
check, which a fork can spoof). These tests pin the gate's refusals + the one
path that merges, and the startup admin-token hard-fail.
See project_champion_merge_fork_pr_redesign_2026_06_20.
"""

from unittest.mock import patch

import pytest

from minotaur_subnet.relayer import solver_repo as sr

SHA = "a" * 40
OTHER = "b" * 40


def _env(monkeypatch):
    monkeypatch.setenv("SOLVER_REPO_URL", "https://github.com/subnet112/minotaur-solver")
    monkeypatch.setenv("CHAMPION_REGISTRY_964", "0x33105027d03e76bf1F3679C0CB9b2688da383fb3")
    monkeypatch.setenv("BITTENSOR_EVM_RPC_URL", "https://lite.chain.opentensor.ai")


class _FakeFns:
    """Mimics web3 contract.functions.<name>().call()."""
    def __init__(self, quorum, latest, by_round=None):
        self._quorum, self._latest, self._by_round = quorum, latest, (by_round or {})
    def getQuorumRequired(self):
        return _Call(self._quorum)
    def getLatestChampion(self):
        return _Call(self._latest)
    def getChampion(self, rid):
        return _Call(self._by_round.get(rid))


class _Call:
    def __init__(self, v): self._v = v
    def call(self): return self._v


def _record(commit_b32, approvals, exists):
    # (roundId, candSubId, candImageId, commitHash[3], effEpoch, certAt, approvalCount[6], exists[7])
    return (b"\x00" * 32, b"\x00" * 32, b"\x00" * 32, commit_b32, 0, 0, approvals, exists)


def _registry(monkeypatch, quorum=4, *, head=SHA, approvals=4, exists=True, latest_commit=None):
    target = sr._str_to_bytes32((latest_commit or head).strip().lower())
    fake = type("R", (), {"functions": _FakeFns(quorum, _record(target, approvals, exists))})()
    monkeypatch.setattr(sr, "_read_champion_registry", lambda: fake)


# ── _onchain_cert_binds ──────────────────────────────────────────────────────

def test_cert_binds_when_latest_matches(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch)
    assert sr._onchain_cert_binds(SHA, "round-1") is True


def test_cert_refuses_on_commit_mismatch(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch, latest_commit=OTHER)
    assert sr._onchain_cert_binds(SHA, "round-1") is False


def test_cert_refuses_below_quorum(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch, quorum=4, approvals=3)
    assert sr._onchain_cert_binds(SHA, "round-1") is False


def test_cert_refuses_when_not_exists(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch, exists=False)
    assert sr._onchain_cert_binds(SHA, "round-1") is False


def test_cert_refuses_when_registry_unreadable(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(sr, "_read_champion_registry", lambda: None)
    assert sr._onchain_cert_binds(SHA, "round-1") is False


def test_cert_refuses_when_quorum_zero(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch, quorum=0)
    assert sr._onchain_cert_binds(SHA, "round-1") is False


# ── merge_miner_pr_when_certified ────────────────────────────────────────────

def _resolved(head):
    return {"clone_url": "https://github.com/x/y", "head_sha": head, "state": "open", "base": "main"}


def test_merge_drift_publishes_certified_tree(monkeypatch):
    """A post-certification force-push no longer voids the win: the gate falls
    back to publishing the CERTIFIED tree directly and closes the drifted PR."""
    _env(monkeypatch); _registry(monkeypatch)
    published, commented, closed = [], [], []
    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", lambda n: _resolved(OTHER)), \
         patch.object(sr, "_publish_certified_tree_to_canonical",
                      lambda *a, **kw: published.append((a, kw)) or True), \
         patch.object(sr, "comment_on_pr", lambda n, body, **kw: commented.append(n) or True), \
         patch.object(sr, "close_pr", lambda n: closed.append(n) or True):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is True
    (args, kwargs), = published
    assert args[2] == SHA  # published the CERTIFIED sha, not the drifted head
    assert kwargs["source_token"] is None
    assert commented == [7] and closed == [7]  # drifted PR closed UNMERGED


def test_merge_drift_refuses_when_cert_not_bound(monkeypatch):
    """Drift fallback keeps the on-chain authority: no cert binding the
    CERTIFIED sha -> no publish, fail closed."""
    _env(monkeypatch); _registry(monkeypatch, latest_commit=OTHER)
    published = []
    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", lambda n: _resolved(OTHER)), \
         patch.object(sr, "_publish_certified_tree_to_canonical",
                      lambda *a, **kw: published.append(a) or True):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is False
    assert published == []


def test_merge_refuses_when_pr_touches_ci(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch)
    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", lambda n: _resolved(SHA)), \
         patch.object(sr, "_pr_touches_ci", lambda o, r, n: True):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is False


def test_merge_refuses_when_not_certified(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch, latest_commit=OTHER)
    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", lambda n: _resolved(SHA)), \
         patch.object(sr, "_pr_touches_ci", lambda o, r, n: False):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is False


def test_merge_succeeds_and_pins_sha(monkeypatch):
    _env(monkeypatch); _registry(monkeypatch)
    calls = []

    def fake_req(method, url, payload=None):
        calls.append((method, url, payload))
        return 200, {"merged": True}

    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", lambda n: _resolved(SHA)), \
         patch.object(sr, "_pr_touches_ci", lambda o, r, n: False), \
         patch.object(sr, "_github_api_request", fake_req):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is True
    method, url, payload = calls[-1]
    assert method == "PUT" and url.endswith("/pulls/7/merge")
    assert payload == {"merge_method": "squash", "sha": SHA}  # squash + pinned to head


def test_merge_unresolvable_pr_falls_back_to_certified_publish(monkeypatch):
    """A PR closed post-certification is the same grief as a drifted head: the
    certified tree publishes anyway."""
    from minotaur_subnet.api.routes.submissions.github_pr import PRResolutionError
    _env(monkeypatch); _registry(monkeypatch)

    def boom(n):
        raise PRResolutionError("closed")

    published = []
    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", boom), \
         patch.object(sr, "_publish_certified_tree_to_canonical",
                      lambda *a, **kw: published.append(a) or True), \
         patch.object(sr, "comment_on_pr", lambda n, body, **kw: True), \
         patch.object(sr, "close_pr", lambda n: True):
        assert sr.merge_miner_pr_when_certified(7, SHA, round_id="r") is True
    assert published and published[0][2] == SHA


def test_merge_refuses_when_pr_unresolvable_and_no_certified_sha(monkeypatch):
    """No live head AND no certified sha -> nothing to merge or publish."""
    from minotaur_subnet.api.routes.submissions.github_pr import PRResolutionError
    _env(monkeypatch); _registry(monkeypatch)

    def boom(n):
        raise PRResolutionError("closed")

    with patch("minotaur_subnet.api.routes.submissions.github_pr.resolve_pr", boom):
        assert sr.merge_miner_pr_when_certified(7, "", round_id="r") is False


# ── assert_solver_repo_token_not_admin ───────────────────────────────────────

def test_token_assert_raises_when_pat_unset(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("SOLVER_REPO_PR_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_ADMIN_SOLVER_REPO_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SOLVER_REPO_PR_TOKEN is unset"):
        sr.assert_solver_repo_token_not_admin()


def test_token_assert_raises_when_admin(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("SOLVER_REPO_PR_TOKEN", "tok")
    monkeypatch.delenv("ALLOW_ADMIN_SOLVER_REPO_TOKEN", raising=False)

    def fake_req(method, url, payload=None):
        if url.endswith("/user"):
            return 200, {"login": "stalkervmr"}
        return 200, {"permission": "admin"}

    with patch.object(sr, "_github_api_request", fake_req):
        with pytest.raises(RuntimeError, match="is repo ADMIN"):
            sr.assert_solver_repo_token_not_admin()


def test_token_assert_passes_when_write(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("SOLVER_REPO_PR_TOKEN", "tok")
    monkeypatch.delenv("ALLOW_ADMIN_SOLVER_REPO_TOKEN", raising=False)

    def fake_req(method, url, payload=None):
        if url.endswith("/user"):
            return 200, {"login": "minotaur-merge-bot"}
        return 200, {"permission": "write"}

    with patch.object(sr, "_github_api_request", fake_req):
        sr.assert_solver_repo_token_not_admin()  # must not raise


def test_token_assert_bypass_flag(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("SOLVER_REPO_PR_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_ADMIN_SOLVER_REPO_TOKEN", "1")
    sr.assert_solver_repo_token_not_admin()  # bypassed, no raise
