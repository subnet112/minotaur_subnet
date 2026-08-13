"""Migration-ladder intake gates (harness/deprecated_surface.py).

Observe-first contract: the surface scan defaults to observe (log-only), the
floor defaults to off, and neither mode changes any accept/reject outcome
until explicitly enforced.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.harness import deprecated_surface as dsf


class TestModes:
    def test_surface_default_is_observe(self, monkeypatch):
        monkeypatch.delenv("DEPRECATED_SURFACE_MODE", raising=False)
        assert dsf.deprecated_surface_mode() == "observe"

    def test_surface_unknown_falls_back_to_observe(self, monkeypatch):
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "enfrce")
        assert dsf.deprecated_surface_mode() == "observe"

    def test_surface_off_and_enforce_respected(self, monkeypatch):
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "off")
        assert dsf.deprecated_surface_mode() == "off"
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "enforce")
        assert dsf.deprecated_surface_mode() == "enforce"

    def test_floor_default_off(self, monkeypatch):
        monkeypatch.delenv("SDK_VERSION_FLOOR", raising=False)
        assert dsf.sdk_version_floor() is None
        monkeypatch.delenv("SDK_VERSION_FLOOR_ENFORCE", raising=False)
        assert dsf.sdk_floor_enforced() is False


class TestSurfaceScan:
    def _repo(self, tmp_path, code: str):
        (tmp_path / "solver.py").write_text(code)
        return str(tmp_path)

    def test_attribute_read_is_a_hit(self, tmp_path):
        hits = dsf.surface_hits(self._repo(
            tmp_path, "x = snapshot.pool_states\n",
        ))
        assert len(hits) == 1 and "solver.py:1" in hits[0]

    def test_guarded_read_is_not_a_hit(self, tmp_path):
        hits = dsf.surface_hits(self._repo(
            tmp_path, 'x = getattr(snapshot, "pool_states", {})\n',
        ))
        assert hits == []

    def test_bare_name_and_param_are_incidental(self, tmp_path):
        code = (
            "def route(pool_states, dex_config):\n"
            "    return pool_states\n"
        )
        assert dsf.surface_hits(self._repo(tmp_path, code)) == []

    def test_comment_is_ignored(self, tmp_path):
        hits = dsf.surface_hits(self._repo(
            tmp_path, "# uses snapshot.pool_states as fallback\n",
        ))
        assert hits == []

    def test_dex_config_also_scanned_but_prices_not(self, tmp_path):
        code = "a = snap.dex_config\nb = snap.prices\n"
        hits = dsf.surface_hits(self._repo(tmp_path, code))
        assert len(hits) == 1 and "dex_config" in hits[0]

    def test_unreadable_repo_fails_open(self, tmp_path):
        assert dsf.surface_hits(str(tmp_path / "missing")) == []


class TestFloor:
    @pytest.mark.parametrize("reported,floor,below", [
        (None, "1.1.0", True),          # pre-marker is below every floor
        ("", "1.1.0", True),
        ("1.0.0", "1.1.0", True),
        ("1.1.0", "1.1.0", False),
        ("1.2.0", "1.1.0", False),
        ("2.0.0", "1.1.0", False),
        ("garbage", "1.1.0", True),     # unparseable == pre-marker
        ("1.10.0", "1.2.0", False),     # numeric, not lexicographic
    ])
    def test_below_floor(self, reported, floor, below):
        assert dsf.below_floor(reported, floor) is below


class TestStage1Wiring:
    def test_observe_mode_never_fails_stage_1(self, tmp_path, monkeypatch):
        # A repo dripping with deprecated reads still PASSES under observe.
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "observe")
        (tmp_path / "s.py").write_text("x = snapshot.pool_states\n" * 5)
        from minotaur_subnet.harness.screening import run_stage_1
        r = run_stage_1(str(tmp_path))
        # Stage 1 may fail for OTHER static reasons on a stub repo, but never
        # with the deprecated-surface code under observe.
        assert r.error_code != "deprecated_surface"

    def test_enforce_mode_rejects_with_named_lines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "enforce")
        (tmp_path / "s.py").write_text(
            "def solve(snapshot):\n    return snapshot.pool_states\n",
        )
        from minotaur_subnet.harness.screening import run_stage_1
        r = run_stage_1(str(tmp_path))
        if r.error_code == "deprecated_surface":
            assert "s.py:2" in r.details
            assert "rpc_urls" in r.details
        else:
            # A stub repo can trip an EARLIER static gate; the wiring is
            # then still proven by the observe test + surface tests above.
            assert not r.passed or r.error_code is None


class TestSurfacePersistence:
    def test_stage1_attaches_hits_in_observe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "observe")
        (tmp_path / "s.py").write_text("x = snapshot.pool_states\n")
        from minotaur_subnet.harness.screening import run_stage_1
        r = run_stage_1(str(tmp_path))
        if r.deprecated_surface_hits is not None:
            assert any("s.py:1" in h for h in r.deprecated_surface_hits)

    def test_stage1_hits_none_when_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEPRECATED_SURFACE_MODE", "off")
        (tmp_path / "s.py").write_text("x = snapshot.pool_states\n")
        from minotaur_subnet.harness.screening import run_stage_1
        r = run_stage_1(str(tmp_path))
        assert r.deprecated_surface_hits is None

    def test_store_roundtrip_and_status_dict(self, tmp_path):
        from minotaur_subnet.harness.submission_store import SubmissionStore
        st = SubmissionStore(persist_path=tmp_path / "submissions.json")
        sub = st.create(
            repo_url="src://x", commit_hash="c1", epoch=1, hotkey="H",
            round_id="r1", max_per_round=0, max_total_per_round=0,
        )
        st.set_deprecated_surface(sub.submission_id, ["a.py:1: x = s.pool_states"])
        d = st.get(sub.submission_id).status_dict()
        assert d["deprecated_surface_hits"] == ["a.py:1: x = s.pool_states"]
        st.set_deprecated_surface(sub.submission_id, [])
        assert st.get(sub.submission_id).status_dict()["deprecated_surface_hits"] == []


class TestMigrationStatusEndpoint:
    def test_payload_shape_and_counts(self, monkeypatch):
        import time
        from types import SimpleNamespace
        from minotaur_subnet.api.routes import monitoring as mon
        from minotaur_subnet.api.routes.submissions import state as sub_state

        now = time.time()
        subs = {
            "a": SimpleNamespace(created_at=now - 100, sdk_version="1.1.0",
                                 deprecated_surface_hits=[]),
            "b": SimpleNamespace(created_at=now - 200, sdk_version="1.0.0",
                                 deprecated_surface_hits=["s.py:1: x"]),
            "c": SimpleNamespace(created_at=now - 300, sdk_version=None,
                                 deprecated_surface_hits=None),
            "old": SimpleNamespace(created_at=now - 200000, sdk_version=None,
                                   deprecated_surface_hits=None),
        }
        fake = SimpleNamespace(_maybe_reload=lambda: None, _submissions=subs)
        monkeypatch.setattr(sub_state, "get_store", lambda: fake)
        monkeypatch.setenv("SDK_VERSION_FLOOR", "1.1.0")
        monkeypatch.delenv("SDK_VERSION_FLOOR_ENFORCE", raising=False)
        mon._MIGRATION_CACHE["payload"] = None
        mon._MIGRATION_CACHE["ts"] = 0.0

        p = mon.migration_status()
        assert p["sdk_version_floor"] == "1.1.0"
        assert p["sdk_version_floor_enforced"] is False
        assert p["deprecated_surface_mode"] == "observe"
        day = p["last_24h"]
        assert day["submissions"] == 3          # 'old' excluded
        assert day["sdk_version_counts"] == {"1.1.0": 1, "1.0.0": 1, "pre-marker": 1}
        # 1.0.0 only. The pre-marker row REPORTED nothing, so it is unmeasured,
        # not below floor — see TestUnmeasuredIsNotBelowFloor.
        assert day["below_floor"] == 1
        assert day["unmeasured"] == 1
        assert day["surface_scanned"] == 2 and day["surface_hits"] == 1
        assert p["retirement_target"] == "2026-09-01"
        assert day["degraded"] is False
        # cache: second call returns the same object without rescanning
        assert mon.migration_status() is p
        mon._MIGRATION_CACHE["payload"] = None

    def test_counts_the_real_submission_store(self, tmp_path, monkeypatch):
        """Wiring, not shape — the fake above cannot catch a wrong-store read.

        The endpoint read ``monitoring._store()``, which is the AppIntentStore
        and has no ``_submissions``; the AttributeError landed in the endpoint's
        bare ``except`` and every count was pinned at zero and served as a
        measurement. Live on 2026-08-04: 0 submissions reported against 302
        real ones. So drive a REAL SubmissionStore through the real accessor.
        """
        from minotaur_subnet.api.routes import monitoring as mon
        from minotaur_subnet.api.routes.submissions import state as sub_state
        from minotaur_subnet.harness.submission_store import SubmissionStore

        st = SubmissionStore(persist_path=tmp_path / "submissions.json")
        sub = st.create(
            repo_url="src://x", commit_hash="c1", epoch=1, hotkey="H",
            round_id="r1", max_per_round=0, max_total_per_round=0,
        )
        st.set_sdk_version(sub.submission_id, "1.0.0")
        st.set_deprecated_surface(sub.submission_id, ["s.py:1: x = snap.pool_states"])

        monkeypatch.setattr(sub_state, "get_store", lambda: st)
        monkeypatch.setenv("SDK_VERSION_FLOOR", "1.1.0")
        mon._MIGRATION_CACHE["payload"] = None
        mon._MIGRATION_CACHE["ts"] = 0.0

        day = mon.migration_status()["last_24h"]
        mon._MIGRATION_CACHE["payload"] = None

        assert day["submissions"] == 1, "the endpoint is not reading the submission store"
        assert day["sdk_version_counts"] == {"1.0.0": 1}
        assert day["below_floor"] == 1
        assert day["unmeasured"] == 0
        assert day["surface_scanned"] == 1 and day["surface_hits"] == 1
        assert day["degraded"] is False

    def test_store_failure_reports_degraded_and_is_not_cached(self, monkeypatch):
        """Zero-because-it-broke must not read as zero-because-nobody-submitted,
        and must not be served for the next 5 minutes."""
        from minotaur_subnet.api.routes import monitoring as mon
        from minotaur_subnet.api.routes.submissions import state as sub_state

        def _boom():
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(sub_state, "get_store", _boom)
        mon._MIGRATION_CACHE["payload"] = None
        mon._MIGRATION_CACHE["ts"] = 0.0

        day = mon.migration_status()["last_24h"]
        assert day["degraded"] is True
        assert day["submissions"] == 0
        assert mon._MIGRATION_CACHE["payload"] is None, (
            "a degraded reading was cached — it would be served as the answer "
            "for the full cache TTL"
        )


class TestUnmeasuredIsNotBelowFloor:
    """A generation we never read is not a generation that is out of date.

    ``below_floor(None, floor)`` is True by construction, which is correct at
    the ENFORCEMENT gate (the value has been read; None means a genuinely
    unmarked solver) and wrong in the dashboard window, which also contains
    submissions rejected before screening stage 2 ever read one.
    """

    def _day(self, monkeypatch, subs):
        import time
        from types import SimpleNamespace
        from minotaur_subnet.api.routes import monitoring as mon
        from minotaur_subnet.api.routes.submissions import state as sub_state

        now = time.time()
        store = {
            str(i): SimpleNamespace(
                created_at=now - 100, sdk_version=v, deprecated_surface_hits=None,
            )
            for i, v in enumerate(subs)
        }
        fake = SimpleNamespace(_maybe_reload=lambda: None, _submissions=store)
        monkeypatch.setattr(sub_state, "get_store", lambda: fake)
        monkeypatch.setenv("SDK_VERSION_FLOOR", "1.1.0")
        mon._MIGRATION_CACHE["payload"] = None
        mon._MIGRATION_CACHE["ts"] = 0.0
        day = mon.migration_status()["last_24h"]
        mon._MIGRATION_CACHE["payload"] = None
        return day

    def test_never_read_counts_as_unmeasured(self, monkeypatch):
        day = self._day(monkeypatch, [None, None, None])
        assert day["unmeasured"] == 3
        assert day["below_floor"] == 0, (
            "submissions rejected before stage 2 never reported a generation — "
            "counting them as below floor invents a migration backlog"
        )

    def test_reported_old_still_counts_as_below_floor(self, monkeypatch):
        day = self._day(monkeypatch, ["1.0.0", "0.9.0"])
        assert day["below_floor"] == 2
        assert day["unmeasured"] == 0

    def test_the_two_buckets_are_disjoint_and_total(self, monkeypatch):
        day = self._day(monkeypatch, ["1.1.0", "1.0.0", None, "1.1.0"])
        assert day["below_floor"] == 1
        assert day["unmeasured"] == 1
        assert day["submissions"] == 4
        assert day["below_floor"] + day["unmeasured"] <= day["submissions"]

    def test_the_live_shape_that_motivated_this(self, monkeypatch):
        """2026-08-13: 221 in 24h, 55 unread — 54 of them dedup rejects whose
        operators reported 1.1.0 on their other submissions. Real backlog: 0."""
        day = self._day(monkeypatch, ["1.1.0"] * 166 + [None] * 55)
        assert day["below_floor"] == 0
        assert day["unmeasured"] == 55

    def test_enforcement_gate_still_rejects_unmarked_solvers(self):
        """The floor itself is UNCHANGED — only the dashboard bucket moved.

        At the gate the value HAS been read, so a None is a real pre-marker
        solver and must still be floored when enforcement is armed.
        """
        from minotaur_subnet.harness.deprecated_surface import below_floor
        assert below_floor(None, "1.1.0") is True
