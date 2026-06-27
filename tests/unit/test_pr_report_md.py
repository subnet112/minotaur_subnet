"""Tests for the PR benchmark-report markdown: the builder's champion join +
revert-trace passthrough, the renderer (champion column, Δ ordering, trace
<details>), the relayer enrichment, the manager wiring, and the orchestrator's
revert-trace capture helpers."""

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.routes.submissions.report import (
    build_submission_report,
    render_report_md,
)
from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.relayer import solver_repo as sr


def _sub(per_intent, *, score=0.7, status="scored"):
    return SimpleNamespace(
        submission_id="sub_x",
        status=SimpleNamespace(value=status),
        benchmark_score=score,
        benchmark_details={"per_intent": per_intent},
        screening={},
    )


_TRACE = {
    "summary": "reverted at step 2/2: Too little received",
    "total_gas": 120000,
    "interactions": [
        {"index": 0, "target": "0xTok", "fn": "approve(address,uint256)", "status": "ok", "gas_used": 46000},
        {"index": 1, "target": "0xRtr", "fn": "exactInputSingle(...)", "status": "reverted",
         "revert_reason": 'Error("Too little received")', "gas_used": 0},
    ],
}


# ── builder: champion join + revert_trace passthrough ────────────────────────

def test_build_joins_champion_per_case_and_delta():
    sub = _sub([
        {"intent_id": "a", "score": 0.9, "on_chain_score": 9800},
        {"intent_id": "b", "score": 0.1, "on_chain_score": 0},
    ])
    champ = {"per_intent": [
        {"intent_id": "a", "score": 0.95, "on_chain_score": 9900},
        {"intent_id": "b", "score": 0.80, "on_chain_score": 9000},
    ]}
    rep = build_submission_report(sub, champion_score=0.85, threshold=0.3,
                                  dethrone_margin=0.05, reason="x", champion_details=champ)
    by = {c["case"]: c for c in rep["per_case"]}
    assert by["a"]["champion"]["js"] == 0.95
    assert by["b"]["delta"] == round(0.1 - 0.80, 6)


def test_build_without_champion_details_has_no_champion_key():
    sub = _sub([{"intent_id": "a", "score": 0.9}])
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason=None)
    assert "champion" not in rep["per_case"][0]


def test_build_passes_revert_trace_through():
    sub = _sub([{"intent_id": "b", "score": 0.1, "revert_trace": _TRACE,
                 "revert_reason": "boom"}])
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason=None)
    assert rep["per_case"][0]["your"]["revert_trace"] == _TRACE


# ── renderer ─────────────────────────────────────────────────────────────────

def test_render_champion_columns_and_delta_ordering():
    sub = _sub([
        {"intent_id": "a", "score": 0.9, "on_chain_score": 9800},
        {"intent_id": "b", "score": 0.1, "on_chain_score": 0, "revert_reason": 'Error("x")'},
    ])
    champ = {"per_intent": [
        {"intent_id": "a", "score": 0.95}, {"intent_id": "b", "score": 0.80},
    ]}
    rep = build_submission_report(sub, champion_score=0.85, threshold=0.3,
                                  dethrone_margin=0.05, reason="did not beat the champion",
                                  champion_details=champ)
    md = render_report_md(rep, submission_id="sub_x")
    assert "| Case | You | Champion | Δ | On-chain (bps) | Result |" in md
    # Biggest regression (b, Δ=-0.70) is listed before a (Δ=-0.05).
    assert md.index("`b`") < md.index("`a`")
    assert "did not beat the champion" in md
    assert 'Error("x")' in md  # revert reason shown, and the row is ❌


def test_render_revert_trace_details_block():
    sub = _sub([{"intent_id": "b", "score": 0.1, "revert_reason": "boom", "revert_trace": _TRACE}])
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason="r")
    md = render_report_md(rep)
    assert "<details><summary>🔬" in md and "</details>" in md
    assert "exactInputSingle(...)" in md
    assert "reverted: Error(\"Too little received\")" in md


def test_render_revert_reads_as_fail_not_pass():
    # passed (js>=threshold) but reverted on-chain → ❌, not ✅.
    sub = _sub([{"intent_id": "b", "score": 0.5, "revert_reason": "boom"}])
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason="r")
    row = [ln for ln in render_report_md(rep).splitlines() if "`b`" in ln][0]
    assert "❌" in row and "✅" not in row


def test_render_pipe_escaping_and_empty():
    assert render_report_md(None) == ""
    sub = _sub([{"intent_id": "swap-a|b", "score": 0.5}])
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason="r")
    assert "swap-a\\|b" in render_report_md(rep)


def test_render_adopted_header():
    sub = _sub([{"intent_id": "a", "score": 0.9}], status="adopted")
    rep = build_submission_report(sub, champion_score=0.5, threshold=0.3,
                                  dethrone_margin=0.05, reason=None)
    assert "✅ Adopted as champion" in render_report_md(rep)


def test_build_and_render_won_header():
    # A scored finalist that BEAT the champion: won=True overrides the
    # score-derived outcome → "won", and renders the 🏆 win header (not ❌).
    sub = _sub([{"intent_id": "a", "score": 0.9, "on_chain_score": 9900}], score=0.9)
    rep = build_submission_report(sub, champion_score=0.5, threshold=0.3,
                                  dethrone_margin=0.05, reason="selected as finalist",
                                  won=True)
    assert rep["outcome"] == "won"
    md = render_report_md(rep, submission_id="sub_x")
    assert "🏆 Beat the champion — selected as finalist" in md
    assert "❌ Submission rejected" not in md
    # The win header is not double-suffixed with the reason.
    assert "selected as finalist — selected as finalist" not in md


# ── relayer enrichment ───────────────────────────────────────────────────────

def test_on_champion_rejected_pr_body_has_scores_and_trace():
    sub = _sub([{"intent_id": "b", "score": 0.1, "revert_reason": "boom", "revert_trace": _TRACE}])
    sub.pr_number = 11
    champ = {"per_intent": [{"intent_id": "b", "score": 0.8}]}
    captured = {}
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: captured.update(n=n, body=b) or True), \
         patch.object(sr, "close_pr", lambda n: True), \
         patch.object(sr, "delete_candidate_image", lambda n: True):
        sr.on_champion_rejected_pr(sub, "did not beat the champion",
                                   champion_score=0.85, dethrone_margin=0.05,
                                   champion_details=champ)
    body = captured["body"]
    assert "Your score" in body and "Champion" in body
    assert "<details><summary>🔬" in body


def test_on_champion_rejected_pr_falls_back_when_not_benchmarked():
    sub = SimpleNamespace(submission_id="s", pr_number=11,
                          status=SimpleNamespace(value="rejected"),
                          benchmark_score=None, benchmark_details=None, screening={})
    captured = {}
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: captured.update(body=b) or True), \
         patch.object(sr, "close_pr", lambda n: True), \
         patch.object(sr, "delete_candidate_image", lambda n: True):
        sr.on_champion_rejected_pr(sub, "screening boom")
    assert captured["body"] == "### ❌ Submission rejected\n\nscreening boom"


def test_on_champion_finalist_pr_comments_and_keeps_pr_open():
    # WIN path: posts the scored report rendered as a win, and does NOT close the
    # PR or GC the image (the PR stays open for the cert-gated merge).
    sub = _sub([{"intent_id": "a", "score": 0.9, "on_chain_score": 9900}], score=0.9)
    sub.pr_number = 22
    champ = {"per_intent": [{"intent_id": "a", "score": 0.5}]}
    captured = {}
    calls = {"close": 0, "gc": 0}
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: captured.update(n=n, body=b) or True), \
         patch.object(sr, "close_pr", lambda n: calls.__setitem__("close", calls["close"] + 1) or True), \
         patch.object(sr, "delete_candidate_image",
                      lambda n: calls.__setitem__("gc", calls["gc"] + 1) or True):
        result = sr.on_champion_finalist_pr(sub, "selected as finalist",
                                            champion_score=0.5, dethrone_margin=0.05,
                                            champion_details=champ)
    assert result is True
    assert captured["n"] == 22
    assert "🏆 Beat the champion" in captured["body"]
    assert "Your score" in captured["body"]
    assert calls == {"close": 0, "gc": 0}  # PR stays open; image not GC'd


def test_on_champion_finalist_pr_skips_without_pr_number():
    sub = _sub([{"intent_id": "a", "score": 0.9}], score=0.9)  # no pr_number
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: (_ for _ in ()).throw(AssertionError("posted"))):
        assert sr.on_champion_finalist_pr(sub, "selected as finalist") is False


# ── manager wiring (call the method unbound with a fake self) ─────────────────

def test_manager_forwards_champion_context():
    from minotaur_subnet.epoch.manager import EpochManager
    rec = {}

    def cb(submission, reason, *, champion_score=None, dethrone_margin=None, champion_details=None):
        rec.update(champion_score=champion_score, dethrone_margin=dethrone_margin,
                   champion_details=champion_details)

    champ_sub = SimpleNamespace(benchmark_details={"per_intent": [{"intent_id": "b", "score": 0.8}]})
    fake_self = SimpleNamespace(
        _on_champion_rejected=cb,
        _champion=SimpleNamespace(benchmark_score=0.85, submission_id="champ1"),
        _dethrone_margin=0.05,
        _sub_store=SimpleNamespace(get=lambda sid: champ_sub),
        _is_leader=lambda: True,
    )
    EpochManager._notify_champion_rejected(fake_self, SimpleNamespace(pr_number=11), "lost")
    assert rec == {"champion_score": 0.85, "dethrone_margin": 0.05,
                   "champion_details": champ_sub.benchmark_details}


def test_manager_legacy_two_arg_callback_still_works():
    from minotaur_subnet.epoch.manager import EpochManager
    seen = {}
    fake_self = SimpleNamespace(
        _on_champion_rejected=lambda submission, reason: seen.update(ok=True),
        _champion=SimpleNamespace(benchmark_score=0.85, submission_id="c"),
        _dethrone_margin=0.05,
        _sub_store=SimpleNamespace(get=lambda sid: None),
        _is_leader=None,  # ungated
    )
    EpochManager._notify_champion_rejected(fake_self, SimpleNamespace(pr_number=11), "lost")
    assert seen.get("ok") is True


def test_manager_leader_gate_blocks_follower():
    from minotaur_subnet.epoch.manager import EpochManager
    seen = {}
    fake_self = SimpleNamespace(
        _on_champion_rejected=lambda submission, reason, **kw: seen.update(posted=True),
        _champion=SimpleNamespace(benchmark_score=0.85, submission_id="c"),
        _dethrone_margin=0.05,
        _sub_store=SimpleNamespace(get=lambda sid: None),
        _is_leader=lambda: False,  # this node is NOT the leader
    )
    EpochManager._notify_champion_rejected(fake_self, SimpleNamespace(pr_number=11), "lost")
    assert seen == {}  # follower posts nothing


# ── orchestrator: revert-trace capture helpers ───────────────────────────────

class _FakeAnvil:
    def simulate_with_trace(self, plan, token_balances=None):
        return {"summary": "ok", "interactions": [], "total_gas": 0}


class _FakeMulti:
    def __init__(self, inner):
        self._inner = inner

    def _get_simulator(self, plan):
        return self._inner


def test_capture_revert_trace_direct_and_multichain():
    assert orch._capture_revert_trace(_FakeAnvil(), object(), {})["summary"] == "ok"
    assert orch._capture_revert_trace(_FakeMulti(_FakeAnvil()), object(), {})["summary"] == "ok"


def test_capture_revert_trace_missing_method_returns_none():
    assert orch._capture_revert_trace(object(), object(), {}) is None


def test_capture_revert_trace_never_raises():
    class Boom:
        def simulate_with_trace(self, plan, token_balances=None):
            raise RuntimeError("rpc down")
    assert orch._capture_revert_trace(Boom(), object(), {}) is None


def test_revert_trace_budget_env(monkeypatch):
    monkeypatch.delenv("BENCHMARK_REVERT_TRACE_MAX", raising=False)
    assert orch._revert_trace_budget() == 10
    monkeypatch.setenv("BENCHMARK_REVERT_TRACE_MAX", "0")
    assert orch._revert_trace_budget() == 0
    monkeypatch.setenv("BENCHMARK_REVERT_TRACE_MAX", "garbage")
    assert orch._revert_trace_budget() == 10
