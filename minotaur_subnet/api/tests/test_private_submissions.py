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


if __name__ == "__main__":
    unittest.main()
