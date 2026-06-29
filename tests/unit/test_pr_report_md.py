"""Tests for the PR benchmark-report markdown: the renderer (aggregate scalars +
same-pin per-order ``relative`` summary), the relayer enrichment, the manager
wiring, and the orchestrator's revert-trace capture helpers.

The cross-fork per-order surfaces (the ``per_case`` champion head-to-head and the
observe-only ``shadow_relative`` block, plus the per-step revert-trace rendering
that lived inside the per_case table) were removed — per-order detail is now the
same-pin ``relative`` count block alone."""

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.api.routes.submissions.report import (
    build_submission_report,
    render_report_md,
)
from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.relayer import solver_repo as sr


def _sub(per_intent, *, score=0.7, status="scored", relative=None):
    details = {"per_intent": per_intent}
    if relative is not None:
        details["relative"] = relative
    return SimpleNamespace(
        submission_id="sub_x",
        status=SimpleNamespace(value=status),
        benchmark_score=score,
        benchmark_details=details,
        screening={},
    )


# ── renderer ─────────────────────────────────────────────────────────────────

def test_render_drops_aggregate_scalars():
    # The legacy aggregate "Your score / Champion" line is removed — under raw-output
    # scoring the JS `score` is a [0,1] validity sentinel, so a scalar is meaningless.
    assert render_report_md(None) == ""
    sub = _sub([{"intent_id": "a", "score": 0.9}], score=0.7)
    rep = build_submission_report(sub, champion_score=0.85, threshold=0.3,
                                  dethrone_margin=0.05, reason="did not beat the champion")
    md = render_report_md(rep, submission_id="sub_x")
    assert "**Your score:**" not in md and "**Champion:**" not in md
    assert "did not beat the champion" in md  # reason still shown in the header


def test_render_relative_summary_and_pipe_escaping():
    # The same-pin relative block renders a per-order summary; the verdict cell is
    # markdown-escaped (pipes).
    rel = {"better": 2, "worse": 0, "matched": 1, "new": 0, "verdict": "a|b"}
    sub = _sub([{"intent_id": "a", "score": 0.9}], relative=rel)
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason="r")
    md = render_report_md(rep)
    assert "Per-order vs champion (same-pin)" in md
    assert "2 better" in md and "1 matched" in md
    assert "a\\|b" in md  # verdict cell escaped


def test_render_note_when_no_relative_block():
    sub = _sub([{"intent_id": "a", "score": 0.9}])  # no stored relative block
    rep = build_submission_report(sub, champion_score=None, threshold=0.3,
                                  dethrone_margin=0.05, reason="r")
    md = render_report_md(rep)
    assert "`relative` block" in md  # points the miner at the status endpoint


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

def test_on_champion_rejected_pr_body_has_scores():
    sub = _sub([{"intent_id": "b", "score": 0.1, "revert_reason": "boom"}])
    sub.pr_number = 11
    captured = {}
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: captured.update(n=n, body=b) or True), \
         patch.object(sr, "close_pr", lambda n: True), \
         patch.object(sr, "delete_candidate_image", lambda n: True):
        sr.on_champion_rejected_pr(sub, "did not beat the champion",
                                   champion_score=0.85, dethrone_margin=0.05)
    body = captured["body"]
    assert "Your score" not in body and "Champion" not in body  # aggregate line dropped
    assert "did not beat the champion" in body


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
    captured = {}
    calls = {"close": 0, "gc": 0}
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: captured.update(n=n, body=b) or True), \
         patch.object(sr, "close_pr", lambda n: calls.__setitem__("close", calls["close"] + 1) or True), \
         patch.object(sr, "delete_candidate_image",
                      lambda n: calls.__setitem__("gc", calls["gc"] + 1) or True):
        result = sr.on_champion_finalist_pr(sub, "selected as finalist",
                                            champion_score=0.5, dethrone_margin=0.05)
    assert result is True
    assert captured["n"] == 22
    assert "🏆 Beat the champion" in captured["body"]
    assert "Your score" not in captured["body"]  # aggregate line dropped
    assert calls == {"close": 0, "gc": 0}  # PR stays open; image not GC'd


def test_on_champion_finalist_pr_skips_without_pr_number():
    sub = _sub([{"intent_id": "a", "score": 0.9}], score=0.9)  # no pr_number
    with patch.object(sr, "comment_on_pr", lambda n, b, owner_repo=None, token=None: (_ for _ in ()).throw(AssertionError("posted"))):
        assert sr.on_champion_finalist_pr(sub, "selected as finalist") is False


# ── manager wiring (call the method unbound with a fake self) ─────────────────

def test_manager_forwards_champion_context():
    from minotaur_subnet.epoch.manager import EpochManager
    rec = {}

    # The cross-fork champion_details surface was removed; the manager forwards
    # only the scalar champion context the callback declares.
    def cb(submission, reason, *, champion_score=None, dethrone_margin=None):
        rec.update(champion_score=champion_score, dethrone_margin=dethrone_margin)

    fake_self = SimpleNamespace(
        _on_champion_rejected=cb,
        _champion=SimpleNamespace(benchmark_score=0.85, submission_id="champ1"),
        _dethrone_margin=0.05,
        _sub_store=SimpleNamespace(get=lambda sid: None),
        _is_leader=lambda: True,
    )
    EpochManager._notify_champion_rejected(fake_self, SimpleNamespace(pr_number=11), "lost")
    assert rec == {"champion_score": 0.85, "dethrone_margin": 0.05}


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


def test_manager_refetches_submission_so_report_has_relative_counts():
    """The notify path re-reads the submission from the store, so the report
    carries the same-pin relative counts persisted earlier in the eval pass —
    the object handed in is stale (predates the persist merge)."""
    from minotaur_subnet.epoch.manager import EpochManager
    rec = {}

    def cb(submission, reason, *, champion_score=None, dethrone_margin=None):
        rec["details"] = getattr(submission, "benchmark_details", None)

    stale = SimpleNamespace(pr_number=11, submission_id="sub_x", benchmark_details={})
    fresh = SimpleNamespace(
        pr_number=11, submission_id="sub_x",
        benchmark_details={"relative": {"better": 4, "worse": 0, "verdict": "dethrone"}},
    )
    fake_self = SimpleNamespace(
        _on_champion_finalist=cb,
        _champion=SimpleNamespace(benchmark_score=0.9, submission_id="champ1"),
        _dethrone_margin=0.05,
        _sub_store=SimpleNamespace(get=lambda sid: fresh if sid == "sub_x" else None),
        _is_leader=lambda: True,
    )
    EpochManager._notify_champion_finalist(fake_self, stale, "selected as finalist")
    # callback saw the FRESH submission (with the relative block), not the stale {}.
    assert rec["details"] == {"relative": {"better": 4, "worse": 0, "verdict": "dethrone"}}


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
