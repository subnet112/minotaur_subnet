"""Unit tests for the P5 PR-lifecycle helpers (comment / close / GC / reject)."""

from types import SimpleNamespace
from unittest.mock import patch

from minotaur_subnet.relayer import solver_repo as sr

OWNER_REPO = ("subnet112", "minotaur-solver")


def _patch_env(monkeypatch):
    monkeypatch.setenv("SOLVER_REPO_URL", "https://github.com/subnet112/minotaur-solver")
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", "ghcr.io/subnet112/minotaur-solver")


def test_comment_on_pr_posts_to_issues_endpoint(monkeypatch):
    _patch_env(monkeypatch)
    calls = []

    def fake_req(method, url, payload=None):
        calls.append((method, url, payload))
        return 201, {"id": 1}

    with patch.object(sr, "_github_api_request", fake_req):
        assert sr.comment_on_pr(7, "hello") is True
    method, url, payload = calls[0]
    assert method == "POST"
    assert url.endswith("/repos/subnet112/minotaur-solver/issues/7/comments")
    assert payload == {"body": "hello"}


def test_comment_on_pr_no_pr_number(monkeypatch):
    _patch_env(monkeypatch)
    with patch.object(sr, "_github_api_request", lambda *a, **k: (201, None)):
        assert sr.comment_on_pr(0, "x") is False  # falsy pr_number -> no-op


def test_close_pr_patches_state_closed(monkeypatch):
    _patch_env(monkeypatch)
    calls = []

    def fake_req(method, url, payload=None):
        calls.append((method, url, payload))
        return 200, {"state": "closed"}

    with patch.object(sr, "_github_api_request", fake_req):
        assert sr.close_pr(7) is True
    method, url, payload = calls[0]
    assert method == "PATCH"
    assert url.endswith("/repos/subnet112/minotaur-solver/pulls/7")
    assert payload == {"state": "closed"}


def test_delete_candidate_image_finds_tag_and_deletes(monkeypatch):
    _patch_env(monkeypatch)
    versions = [
        {"id": 100, "metadata": {"container": {"tags": ["pr-3", "other"]}}},
        {"id": 200, "metadata": {"container": {"tags": ["pr-7"]}}},
    ]
    seen = []

    def fake_req(method, url, payload=None):
        seen.append((method, url))
        if method == "GET":
            return 200, versions
        if method == "DELETE":
            return 204, None
        return 0, None

    with patch.object(sr, "_github_api_request", fake_req):
        assert sr.delete_candidate_image(7) is True
    # It listed versions then deleted version 200 (the one tagged pr-7).
    assert any(m == "GET" and "/versions" in u for m, u in seen)
    assert any(m == "DELETE" and u.endswith("/versions/200") for m, u in seen)


def test_delete_candidate_image_no_matching_tag(monkeypatch):
    _patch_env(monkeypatch)
    versions = [{"id": 100, "metadata": {"container": {"tags": ["pr-3"]}}}]
    with patch.object(sr, "_github_api_request", lambda m, u, p=None: (200, versions)):
        assert sr.delete_candidate_image(7) is False  # pr-7 absent -> nothing deleted


def test_on_champion_rejected_pr_comments_and_gcs_but_never_closes(monkeypatch):
    # Policy: a failure NEVER closes the PR — only a successful merge closes one
    # (GitHub auto-closes a squash-merged PR). The reject path comments feedback +
    # GCs the candidate image, leaving the PR OPEN so the miner can iterate.
    _patch_env(monkeypatch)
    sub = SimpleNamespace(submission_id="sub_1", pr_number=7)
    order = []
    with patch.object(sr, "comment_on_pr", lambda n, b: order.append(("comment", n)) or True), \
         patch.object(sr, "close_pr", lambda n: order.append(("close", n)) or True), \
         patch.object(sr, "delete_candidate_image", lambda n: order.append(("gc", n)) or True):
        assert sr.on_champion_rejected_pr(sub, "too slow") is True
    assert order == [("comment", 7), ("gc", 7)]
    assert ("close", 7) not in order  # the PR is left OPEN on a reject


def test_on_champion_rejected_pr_no_pr_number_noop(monkeypatch):
    _patch_env(monkeypatch)
    sub = SimpleNamespace(submission_id="sub_1", pr_number=None)
    with patch.object(sr, "close_pr", lambda n: True):
        assert sr.on_champion_rejected_pr(sub, "x") is False
