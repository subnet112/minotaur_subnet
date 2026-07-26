"""Structural fingerprint + observe-only structural dedup.

Pins the two properties that make it safe: a salted constant collapses to ONE
structural identity (catches the sybil), while a real logic change diverges it
(fork-and-improve keeps its own slot).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from minotaur_subnet.harness.structural_fingerprint import (
    repo_structural_fingerprint,
    structural_python_bytes,
)
from minotaur_subnet.harness.code_fingerprint import source_fingerprint
from minotaur_subnet.harness.rotation import structural_dedup_clusters


def _fp(src: str) -> bytes:
    return structural_python_bytes(src)


BASE = '''
BUILD_SALT = "aaaaaaaa"
class SwapSolver:
    def route(self, amount):
        x = amount * 2
        if x > 100:
            return x - 1
        return x
'''


class TestStructuralInvariance:
    def test_salted_constant_same_structure(self):
        # The sybil move: identical code, one salted build-constant per copy.
        a = BASE
        b = BASE.replace('"aaaaaaaa"', '"bbbbbbbb"')
        c = BASE.replace('"aaaaaaaa"', '"cccccccc"')
        # Structural fp collapses all three...
        assert _fp(a) == _fp(b) == _fp(c)
        # ...while the CONTENT fp (which keeps constant values) splits them —
        # this is exactly the gap the structural fp closes.
        assert source_fingerprint(a) != source_fingerprint(b)

    def test_salted_number_same_structure(self):
        a = BASE
        b = BASE.replace('BUILD_SALT = "aaaaaaaa"', "BUILD_SALT = 12345")
        # different constant TYPE (str vs int) → different structure (correct:
        # a type change is a real change). Same type, different value → same:
        b2 = BASE.replace('BUILD_SALT = "aaaaaaaa"', 'BUILD_SALT = "zzzzzzzz"')
        assert _fp(a) == _fp(b2)
        assert _fp(a) != _fp(b)

    def test_docstring_and_comment_invariant(self):
        a = BASE
        b = '"""a docstring."""\n' + BASE + "\n# a comment\n"
        assert _fp(a) == _fp(b)

    def test_real_logic_change_diverges(self):
        # A genuine improvement: extra branch / different call → distinct fp.
        improved = BASE.replace(
            "        return x", "        y = self.optimize(x)\n        return y"
        )
        assert _fp(BASE) != _fp(improved)

    def test_renamed_identifiers_currently_diverge(self):
        # v1 keeps identifiers (documented conservative choice) — renaming a
        # function is NOT yet caught. Pins the known scope limit.
        renamed = BASE.replace("def route", "def compute")
        assert _fp(BASE) != _fp(renamed)

    def test_deterministic(self):
        assert _fp(BASE) == _fp(BASE)

    def test_unparseable_falls_back_to_raw(self):
        bad = "def (:\n  broken"
        assert structural_python_bytes(bad) == bad.encode("utf-8", "surrogateescape")


class TestRepoFingerprint:
    def test_py_only_data_files_ignored(self, tmp_path):
        (tmp_path / "solver.py").write_text(BASE)
        (tmp_path / "replay.json").write_text('{"nonce": 1}')
        fp1 = repo_structural_fingerprint(str(tmp_path))
        # A salted data file must NOT mint a new structural identity.
        (tmp_path / "replay.json").write_text('{"nonce": 999999}')
        fp2 = repo_structural_fingerprint(str(tmp_path))
        assert fp1 == fp2 and fp1 is not None

    def test_salted_constant_repo_collapses(self, tmp_path):
        d1 = tmp_path / "m1"; d1.mkdir(); (d1 / "s.py").write_text(BASE)
        d2 = tmp_path / "m2"; d2.mkdir()
        (d2 / "s.py").write_text(BASE.replace('"aaaaaaaa"', '"salted!!"'))
        assert repo_structural_fingerprint(str(d1)) == repo_structural_fingerprint(str(d2))

    def test_no_python_returns_none(self, tmp_path):
        (tmp_path / "data.json").write_text("{}")
        assert repo_structural_fingerprint(str(tmp_path)) is None


def _sub(sid, hk, fp):
    return SimpleNamespace(submission_id=sid, hotkey=hk, structural_fingerprint=fp)


class TestStructuralDedupClusters:
    # actor_of maps hotkey → coldkey/actor
    def _actor_of(self, m):
        f = lambda hk: m.get(hk, hk)  # noqa: E731
        f.source = "test"
        return f

    def test_cross_actor_same_fp_clusters(self):
        # 3 distinct actors (coldkeys), one structural fp — the live sybil.
        subs = [_sub("a", "hk1", "FP"), _sub("b", "hk2", "FP"), _sub("c", "hk3", "FP")]
        actor_of = self._actor_of({"hk1": "ck1", "hk2": "ck2", "hk3": "ck3"})
        clusters = structural_dedup_clusters(subs, actor_of)
        assert len(clusters) == 1
        assert {s.submission_id for s in clusters[0]} == {"a", "b", "c"}

    def test_same_actor_not_clustered(self):
        # One actor's own resubmissions — actor-keying already dedups these.
        subs = [_sub("a", "hk1", "FP"), _sub("b", "hk2", "FP")]
        actor_of = self._actor_of({"hk1": "ck1", "hk2": "ck1"})  # same coldkey
        assert structural_dedup_clusters(subs, actor_of) == []

    def test_distinct_fp_not_clustered(self):
        subs = [_sub("a", "hk1", "FP1"), _sub("b", "hk2", "FP2")]
        actor_of = self._actor_of({"hk1": "ck1", "hk2": "ck2"})
        assert structural_dedup_clusters(subs, actor_of) == []

    def test_missing_fp_ignored(self):
        # No structural fp (unparseable / pre-metric) never manufactures a cluster.
        subs = [_sub("a", "hk1", None), _sub("b", "hk2", None)]
        actor_of = self._actor_of({"hk1": "ck1", "hk2": "ck2"})
        assert structural_dedup_clusters(subs, actor_of) == []

    def test_no_actor_map_uses_hotkey(self):
        subs = [_sub("a", "hk1", "FP"), _sub("b", "hk2", "FP")]
        clusters = structural_dedup_clusters(subs, None)
        assert len(clusters) == 1
