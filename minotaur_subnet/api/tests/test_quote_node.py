"""Tests for quote-node (trust-leader) mode helpers — the gating predicate and
the loaded-vs-champion sync check that makes 'always the current champion'
verifiable."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from minotaur_subnet.api import quote_node


class TestQuoteNodeMode(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get(quote_node.LEADER_API_URL_ENV)
        os.environ.pop(quote_node.LEADER_API_URL_ENV, None)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(quote_node.LEADER_API_URL_ENV, None)
        else:
            os.environ[quote_node.LEADER_API_URL_ENV] = self._prev

    def test_disabled_by_default(self):
        self.assertFalse(quote_node.is_quote_node())
        self.assertIsNone(quote_node.leader_api_url())

    def test_enabled_and_trimmed(self):
        os.environ[quote_node.LEADER_API_URL_ENV] = "https://api.minotaursubnet.com/"
        self.assertTrue(quote_node.is_quote_node())
        # trailing slash trimmed so callers append /v1/... uniformly
        self.assertEqual(quote_node.leader_api_url(), "https://api.minotaursubnet.com")

    def test_blank_is_disabled(self):
        os.environ[quote_node.LEADER_API_URL_ENV] = "   "
        self.assertFalse(quote_node.is_quote_node())


class TestChampionStatus(unittest.TestCase):
    @staticmethod
    def _champ(sub="sub_1", digest="repo@sha256:abc", image_id=None):
        return SimpleNamespace(
            submission_id=sub, image_digest=digest, image_id=image_id,
            activated_round_id="round_1",
        )

    def test_synced_when_loaded_matches_digest(self):
        st = quote_node.champion_status("repo@sha256:abc", self._champ(digest="other@sha256:abc"))
        # representation-independent: compares the sha256 suffix
        self.assertTrue(st["synced"])
        self.assertEqual(st["active_submission_id"], "sub_1")
        self.assertEqual(st["loaded_image"], "repo@sha256:abc")

    def test_not_synced_on_split(self):
        # champion record advanced (sha256:NEW) but the running solver is sha256:OLD
        st = quote_node.champion_status("repo@sha256:OLD", self._champ(digest="repo@sha256:NEW"))
        self.assertFalse(st["synced"])

    def test_unknown_when_no_champion(self):
        st = quote_node.champion_status("repo@sha256:abc", None)
        self.assertIsNone(st["synced"])

    def test_unknown_when_no_loaded_image(self):
        st = quote_node.champion_status(None, self._champ())
        self.assertIsNone(st["synced"])

    def test_falls_back_to_image_id_when_no_digest(self):
        champ = self._champ(digest=None, image_id="sha256:localid")
        st = quote_node.champion_status("sha256:localid", champ)
        self.assertTrue(st["synced"])


if __name__ == "__main__":
    unittest.main()
