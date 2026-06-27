"""Tests for the private-repo submission path.

Covers the self-contained units of the feature: request validation, PR
resolution against the miner's declared private repo, the per-submission token
store (reload-safe, never persisted, purged on terminal), the token clone
credential, and the private PR-comment target selection.
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from minotaur_subnet.api.routes.submissions.github_pr import (
    PRResolutionError,
    resolve_pr,
)
from minotaur_subnet.api.routes.submissions.models import SubmitRequest
from minotaur_subnet.api.routes.submissions.screening_pipeline import _token_basic_auth
from minotaur_subnet.harness.submission_store import SubmissionStore

_HEAD = "a" * 40


def _base(**over):
    d = dict(
        pr_number=7,
        head_sha=_HEAD,
        epoch=0,
        hotkey="5Gxxxxxxxxxx",
        signature="sig",
    )
    d.update(over)
    return d


class TestSubmitRequestValidation(unittest.TestCase):
    def test_public_submission_has_no_private_fields(self):
        req = SubmitRequest(**_base())
        self.assertFalse(req.is_private)
        self.assertIsNone(req.private_repo)

    def test_private_pair_accepted(self):
        req = SubmitRequest(**_base(private_repo="me/solver", repo_token="ghp_x"))
        self.assertTrue(req.is_private)
        self.assertEqual(req.private_repo, "me/solver")

    def test_repo_without_token_rejected(self):
        with self.assertRaises(ValidationError):
            SubmitRequest(**_base(private_repo="me/solver"))

    def test_token_without_repo_rejected(self):
        with self.assertRaises(ValidationError):
            SubmitRequest(**_base(repo_token="ghp_x"))

    def test_malformed_repo_rejected(self):
        with self.assertRaises(ValidationError):
            SubmitRequest(**_base(private_repo="not-a-repo", repo_token="ghp_x"))


class TestResolvePrPrivate(unittest.TestCase):
    def _pr(self, base_full: str):
        return {
            "state": "open",
            "base": {"repo": {"full_name": base_full}},
            "head": {
                "sha": _HEAD,
                "repo": {"clone_url": "https://github.com/me/solver.git"},
            },
        }

    def test_private_base_matches_declared_repo(self):
        out = resolve_pr(
            7, fetch=lambda o, r, n: self._pr("me/solver"), owner_repo=("me", "solver"),
        )
        self.assertEqual(out["head_sha"], _HEAD)
        self.assertEqual(out["base"], "me/solver")

    def test_private_base_mismatch_rejected(self):
        # A PR whose base isn't the declared private repo is refused.
        with self.assertRaises(PRResolutionError):
            resolve_pr(
                7,
                fetch=lambda o, r, n: self._pr("someone-else/solver"),
                owner_repo=("me", "solver"),
            )

    def test_public_path_still_requires_canonical_base(self):
        # No owner_repo → canonical base enforcement is unchanged.
        with self.assertRaises(PRResolutionError):
            resolve_pr(7, fetch=lambda o, r, n: self._pr("me/solver"))


class TestTokenCloneAuth(unittest.TestCase):
    def test_github_token_basic_auth(self):
        auth = _token_basic_auth("https://github.com/me/solver.git", "ghp_abc")
        self.assertEqual(
            base64.b64decode(auth).decode(), "x-access-token:ghp_abc",
        )

    def test_non_github_host_refused(self):
        self.assertIsNone(_token_basic_auth("https://evil.example/me/solver.git", "ghp_abc"))


class TestStoreTokenHandling(unittest.TestCase):
    def _store(self, persist=False):
        if persist:
            self._tmp = TemporaryDirectory()
            return SubmissionStore(persist_path=Path(self._tmp.name) / "subs.json")
        return SubmissionStore()

    def test_token_retrievable_and_flags_set(self):
        store = self._store()
        sub = store.create(
            "https://github.com/me/solver.git", _HEAD, epoch=0, hotkey="hk",
            round_id="r1", pr_number=7,
            is_private=True, private_repo_full="me/solver", repo_token="ghp_x",
        )
        self.assertTrue(sub.is_private)
        self.assertEqual(sub.private_repo_full, "me/solver")
        self.assertEqual(store.get_repo_token(sub.submission_id), "ghp_x")

    def test_token_never_serialized(self):
        store = self._store()
        sub = store.create(
            "https://github.com/me/solver.git", _HEAD, epoch=0, hotkey="hk",
            round_id="r1", pr_number=7,
            is_private=True, private_repo_full="me/solver", repo_token="ghp_secret",
        )
        self.assertNotIn("ghp_secret", str(sub.to_dict()))
        self.assertNotIn("repo_token", sub.to_dict())

    def test_token_survives_reload_then_purged_on_reject(self):
        store = self._store(persist=True)
        sub = store.create(
            "https://github.com/me/solver.git", _HEAD, epoch=0, hotkey="hk",
            round_id="r1", pr_number=7,
            is_private=True, private_repo_full="me/solver", repo_token="ghp_x",
        )
        sid = sub.submission_id
        # A write triggers the reload-on-write guard, which rebuilds _submissions
        # from disk; the token (held in the side map) must survive that.
        store.update_status(sid, store.get(sid).status)
        self.assertEqual(store.get_repo_token(sid), "ghp_x")
        # Rejection is terminal → token purged.
        store.reject(sid, "nope")
        self.assertIsNone(store.get_repo_token(sid))

    def test_private_flags_round_trip_through_persistence(self):
        store = self._store(persist=True)
        sub = store.create(
            "https://github.com/me/solver.git", _HEAD, epoch=0, hotkey="hk",
            round_id="r1", pr_number=7,
            is_private=True, private_repo_full="me/solver", repo_token="ghp_x",
        )
        sid = sub.submission_id
        reloaded = SubmissionStore(persist_path=store._persist_path)
        self.assertTrue(reloaded.get(sid).is_private)
        self.assertEqual(reloaded.get(sid).private_repo_full, "me/solver")
        # Token is NOT persisted, so a fresh process has no copy.
        self.assertIsNone(reloaded.get_repo_token(sid))


class TestCommentTarget(unittest.TestCase):
    def test_private_target_uses_repo_and_token(self):
        from types import SimpleNamespace

        from minotaur_subnet.relayer.solver_repo import _pr_comment_target

        sub = SimpleNamespace(is_private=True, private_repo_full="me/solver")
        owner_repo, token = _pr_comment_target(sub, "ghp_x")
        self.assertEqual(owner_repo, ("me", "solver"))
        self.assertEqual(token, "ghp_x")

    def test_public_target_falls_back(self):
        from types import SimpleNamespace

        from minotaur_subnet.relayer.solver_repo import _pr_comment_target

        sub = SimpleNamespace(is_private=False, private_repo_full=None)
        owner_repo, token = _pr_comment_target(sub, None)
        self.assertIsNone(owner_repo)
        self.assertIsNone(token)


class _FakeGitHub:
    """Records GitHub Git Data API calls and answers them, so we can exercise
    publish_private_champion_when_certified without touching the network.

    Routes by (METHOD, url-substring). Captures created blobs/tree/commit/PR and
    the final squash-merge so tests can assert what was published.
    """

    def __init__(self):
        self.calls = []          # (method, url, payload)
        self.created_blobs = []   # canonical blob create payloads
        self.created_tree = None  # canonical tree payload
        self.merged = False
        self.deleted_branch = False

    def __call__(self, method, url, payload=None, *, token=None):
        self.calls.append((method, url, payload))
        # --- private repo reads (recursive tree + blobs) ---
        if method == "GET" and "/git/trees/" in url and "me/solver" in url:
            return 200, {"truncated": False, "tree": [
                {"type": "blob", "path": "solver.py", "mode": "100644", "sha": "psolver"},
                {"type": "blob", "path": ".github/workflows/evil.yml", "mode": "100644", "sha": "pevil"},
            ]}
        if method == "GET" and "/git/blobs/" in url and "me/solver" in url:
            return 200, {"content": "Y29kZQ==", "encoding": "base64"}
        # --- canonical reads ---
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return 200, {"object": {"sha": "MAINSHA"}}
        if method == "GET" and "/git/commits/MAINSHA" in url:
            return 200, {"tree": {"sha": "MAINTREE"}}
        if method == "GET" and "/git/trees/MAINTREE" in url:
            return 200, {"truncated": False, "tree": [
                {"type": "blob", "path": ".github/workflows/ci.yml", "mode": "100644", "sha": "ghci"},
                {"type": "blob", "path": "solver.py", "mode": "100644", "sha": "oldsolver"},
            ]}
        # --- canonical writes ---
        if method == "POST" and url.endswith("/git/blobs"):
            self.created_blobs.append(payload)
            return 201, {"sha": "newblob" + str(len(self.created_blobs))}
        if method == "POST" and url.endswith("/git/trees"):
            self.created_tree = payload
            return 201, {"sha": "NEWTREE"}
        if method == "POST" and url.endswith("/git/commits"):
            return 201, {"sha": "NEWCOMMIT"}
        if method == "POST" and url.endswith("/git/refs"):
            return 201, {}
        if method == "POST" and url.endswith("/pulls"):
            return 201, {"number": 999}
        if method == "PUT" and url.endswith("/merge"):
            self.merged = True
            return 200, {}
        if method == "DELETE" and "/git/refs/heads/" in url:
            self.deleted_branch = True
            return 204, None
        raise AssertionError(f"unexpected GitHub call: {method} {url}")


class TestPublishPrivateChampion(unittest.TestCase):
    """publish_private_champion_when_certified — the relayer-side canonical publish."""

    def _run(self, fake, *, cert_binds=True, resolved_head=_HEAD):
        import minotaur_subnet.relayer.solver_repo as sr
        from unittest import mock

        with mock.patch.dict(
            "os.environ",
            {"SOLVER_REPO_URL": "https://github.com/subnet112/minotaur-solver",
             "SOLVER_REPO_TOKEN": "ghp_canon"},
        ), mock.patch.object(sr, "_github_api_request", fake), \
             mock.patch.object(sr, "_onchain_cert_binds", lambda h, r: cert_binds), \
             mock.patch(
                 "minotaur_subnet.api.routes.submissions.github_pr.resolve_pr",
                 lambda n, **kw: {"head_sha": resolved_head, "clone_url":
                                  "https://github.com/me/solver.git", "state": "open",
                                  "base": "me/solver"},
             ):
            return sr.publish_private_champion_when_certified(
                3, _HEAD, "r1", private_repo="me/solver", repo_token="ghp_miner",
            )

    def test_happy_path_publishes_and_merges(self):
        fake = _FakeGitHub()
        ok = self._run(fake)
        self.assertTrue(ok)
        self.assertTrue(fake.merged)
        # The new tree = the private solver.py + canonical's OWN .github (preserved),
        # and the private .github/** is EXCLUDED (CI-disarm).
        paths = {e["path"]: e["sha"] for e in fake.created_tree["tree"]}
        self.assertIn("solver.py", paths)
        self.assertEqual(paths["solver.py"], "newblob1")          # recreated from private
        self.assertEqual(paths[".github/workflows/ci.yml"], "ghci")  # canonical's, verbatim
        self.assertNotIn(".github/workflows/evil.yml", paths)     # private CI dropped
        # Exactly one blob recreated (solver.py) — the private .github blob was skipped.
        self.assertEqual(len(fake.created_blobs), 1)

    def test_fail_closed_when_cert_does_not_bind(self):
        fake = _FakeGitHub()
        ok = self._run(fake, cert_binds=False)
        self.assertFalse(ok)
        self.assertFalse(fake.merged)

    def test_fail_closed_on_head_drift(self):
        fake = _FakeGitHub()
        ok = self._run(fake, resolved_head="b" * 40)  # live head != certified head
        self.assertFalse(ok)
        self.assertFalse(fake.merged)


if __name__ == "__main__":
    unittest.main()
