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


@pytest.fixture(autouse=True)
def _instant_cert_retry(monkeypatch):
    """Zero the merge-gate cert-read backoff so retry tests don't real-sleep."""
    monkeypatch.setattr(sr, "_CERT_READ_BACKOFF_S", 0.0)


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


# ── cert-read retry/backoff resilience (429 churn fix) ────────────────────────

def test_cert_read_retries_then_succeeds_on_transient(monkeypatch):
    """Registry unreadable (RPC down/429) on the first attempts, then recovers →
    the gate must retry and honor the certified win, not fail-close."""
    _env(monkeypatch)
    target = sr._str_to_bytes32(SHA)
    good = type("R", (), {"functions": _FakeFns(4, _record(target, 4, True))})()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return None if calls["n"] < 3 else good  # transient twice, then good

    monkeypatch.setattr(sr, "_read_champion_registry", flaky)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 4)
    assert sr._onchain_cert_binds(SHA, "round-1") is True
    assert calls["n"] == 3  # retried past the two transient failures


def test_cert_read_retries_on_rpc_exception(monkeypatch):
    """A 429-style exception from the .call() is transient → retried, not fatal."""
    _env(monkeypatch)
    target = sr._str_to_bytes32(SHA)

    class _FlakyFns:
        def __init__(self): self.n = 0
        def getQuorumRequired(self):
            self.n += 1
            if self.n < 3:
                raise Exception("429 Client Error: Too Many Requests")
            return _Call(4)
        def getLatestChampion(self): return _Call(_record(target, 4, True))
        def getChampion(self, rid): return _Call(None)

    fake = type("R", (), {"functions": _FlakyFns()})()
    monkeypatch.setattr(sr, "_read_champion_registry", lambda: fake)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 4)
    assert sr._onchain_cert_binds(SHA, "round-1") is True


def test_cert_definitive_negative_does_not_retry(monkeypatch):
    """A successful read whose cert simply doesn't bind is DEFINITIVE — refuse on
    the first read, never burn retries (security: no waiting out a real reject)."""
    _env(monkeypatch)
    target = sr._str_to_bytes32(OTHER)  # commit mismatch
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        return type("R", (), {"functions": _FakeFns(4, _record(target, 4, True))})()

    monkeypatch.setattr(sr, "_read_champion_registry", once)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 4)
    assert sr._onchain_cert_binds(SHA, "round-1") is False
    assert calls["n"] == 1  # no retry on a definitive negative


def test_cert_read_exhausts_retries_then_fail_closed(monkeypatch):
    """Persistent transient failure across all attempts → fail-closed (refuse)."""
    _env(monkeypatch)
    calls = {"n": 0}

    def always_down():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sr, "_read_champion_registry", always_down)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 3)
    assert sr._onchain_cert_binds(SHA, "round-1") is False
    assert calls["n"] == 3  # exhausted all attempts before refusing


def test_cert_refuses_fast_on_unconfigured_env(monkeypatch):
    """A missing registry/RPC env is a persistent config error → refuse WITHOUT
    burning the retry budget (never even reach _read_champion_registry)."""
    _env(monkeypatch)
    monkeypatch.delenv("CHAMPION_REGISTRY_964", raising=False)
    calls = {"n": 0}

    def _should_not_run():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sr, "_read_champion_registry", _should_not_run)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 4)
    assert sr._onchain_cert_binds(SHA, "round-1") is False
    assert calls["n"] == 0  # config error refused before any read/retry


# ── attest-confirmed fast path (receipt-based, zero-RPC) ──────────────────────

def test_cert_attest_confirmed_skips_registry_read(monkeypatch):
    """A status=1 attest for this EXACT sha short-circuits — no registry read at
    all, so there is nothing to 429."""
    _env(monkeypatch)

    def _should_not_run():
        raise AssertionError("registry read must not happen on the attest fast path")

    monkeypatch.setattr(sr, "_read_champion_registry", _should_not_run)
    assert sr._onchain_cert_binds(SHA, "round-1", attest_confirmed_sha=SHA) is True
    assert sr._onchain_cert_binds(SHA.upper(), "round-1", attest_confirmed_sha=SHA) is True  # case-insensitive


def test_cert_attest_confirmed_mismatch_falls_through_to_read(monkeypatch):
    """attest_confirmed for a DIFFERENT sha must NOT short-circuit the checked
    head — the gate still runs its authoritative read (which here refuses)."""
    _env(monkeypatch); _registry(monkeypatch, latest_commit=OTHER)  # on-chain binds OTHER, not SHA
    # head=SHA, attest_confirmed=OTHER (≠SHA) → no short-circuit → read → SHA unbound → False
    assert sr._onchain_cert_binds(SHA, "round-1", attest_confirmed_sha=OTHER) is False


def test_cert_no_attest_confirmed_still_reads(monkeypatch):
    """Absent attest confirmation, behavior is unchanged: authoritative read."""
    _env(monkeypatch); _registry(monkeypatch)
    assert sr._onchain_cert_binds(SHA, "round-1", attest_confirmed_sha=None) is True


def test_cert_attest_confirmed_case_skew_does_not_falsely_bind(monkeypatch):
    """_str_to_bytes32 is case-sensitive for a 40-char SHA, so an UPPERCASE
    attest_confirmed_sha encodes to a DIFFERENT on-chain commitHash than the
    lowercased head target — it must NOT short-circuit (that would claim a bind
    the chain doesn't have). Falls through to the read, which here refuses."""
    _env(monkeypatch)
    monkeypatch.setattr(sr, "_read_champion_registry", lambda: None)  # read down → refuse
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 1)
    assert sr._onchain_cert_binds(SHA, "round-1", attest_confirmed_sha=SHA.upper()) is False


def test_cert_trust_receipt_disabled_forces_read(monkeypatch):
    """MERGE_GATE_TRUST_ATTEST_RECEIPT off → fast path disabled, always reads."""
    _env(monkeypatch)
    monkeypatch.setattr(sr, "_TRUST_ATTEST_RECEIPT", False)
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sr, "_read_champion_registry", counted)
    monkeypatch.setattr(sr, "_CERT_READ_ATTEMPTS", 1)
    assert sr._onchain_cert_binds(SHA, "round-1", attest_confirmed_sha=SHA) is False
    assert calls["n"] == 1  # did the authoritative read, not the fast path


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
