"""STRUCTURAL_DEDUP_MODE=reject — one queue seat per (operator, structure).

The successor to the retired ``enforce`` slate collapse, redesigned after the
2026-07-29 measurement: the salt-invariant fingerprint cannot separate a copy
from an improvement (a champion fork with a real -123-node factorization win
kept the champion's exact fingerprint), so population-keyed enforcement
starved fork-and-improve. The reject is therefore keyed on the OPERATOR:

  * SAME operator, same structure, live in queue  -> reject at intake
    (pre-build), duplicate named.
  * DIFFERENT operators, same structure           -> never touched here.
  * No coldkey attribution                        -> stand down (never guess).
"""

from __future__ import annotations

import pytest

from minotaur_subnet.api.routes.submissions.screening_pipeline import (
    structural_duplicates_same_operator,
)
from minotaur_subnet.harness.actor import ActorResolver
from minotaur_subnet.harness.structural_fingerprint import structural_dedup_mode
from minotaur_subnet.harness.submission_store import OUTCOME_STRUCTURAL_DUPLICATE

# O = solo operator (CK_O); A1/A2 = one fleet (CK_A); X = unmapped.
RESOLVER = ActorResolver.from_maps(
    {"O": "CK_O", "A1": "CK_A", "A2": "CK_A", "B": "CK_B"}, source="test",
)


class TestMode:
    def test_reject_is_a_valid_mode(self, monkeypatch):
        monkeypatch.setenv("STRUCTURAL_DEDUP_MODE", "reject")
        assert structural_dedup_mode() == "reject"

    def test_unknown_still_fails_safe_to_off(self, monkeypatch):
        monkeypatch.setenv("STRUCTURAL_DEDUP_MODE", "rejct")
        assert structural_dedup_mode() == "off"


class TestSameOperatorDuplicates:
    def test_same_hotkey_duplicate_is_flagged(self):
        live = [("O", "sub_orig", "queued")]
        assert structural_duplicates_same_operator(
            live, hotkey="O", resolver=RESOLVER,
        ) == ["sub_orig"]

    def test_fleet_sibling_duplicate_is_flagged(self):
        # A2 re-rolls the structure A1 already queues — same coldkey, one seat.
        live = [("A1", "sub_a1", "scored")]
        assert structural_duplicates_same_operator(
            live, hotkey="A2", resolver=RESOLVER,
        ) == ["sub_a1"]

    def test_cross_operator_match_is_never_flagged(self):
        # The 2026-07-29 lesson: another operator queuing the same structure
        # is (potential) fork-and-improve, not a duplicate of OURS.
        live = [("O", "sub_o", "waitlisted"), ("B", "sub_b", "scored")]
        assert structural_duplicates_same_operator(
            live, hotkey="A1", resolver=RESOLVER,
        ) == []

    def test_unmapped_submitter_stands_down(self):
        # No coldkey attribution for the NEW submission -> indeterminate ->
        # never rejects, even against a same-hotkey live sibling.
        live = [("X", "sub_x", "waitlisted")]
        assert structural_duplicates_same_operator(
            live, hotkey="X", resolver=RESOLVER,
        ) == []

    def test_unmapped_sibling_never_matches(self):
        live = [("X", "sub_x", "waitlisted"), ("O", "sub_o", "queued")]
        assert structural_duplicates_same_operator(
            live, hotkey="O", resolver=RESOLVER,
        ) == ["sub_o"]

    def test_outcome_code_is_registered(self):
        assert OUTCOME_STRUCTURAL_DUPLICATE == "structural_duplicate"


class TestStoreLiveScan:
    @pytest.fixture
    def store(self, tmp_path):
        from minotaur_subnet.harness.submission_store import (
            SubmissionStore,
            SubmissionStatus,
        )

        st = SubmissionStore(persist_path=tmp_path / "submissions.json")
        for tag, hk, status, fp in [
            ("wait1", "O", SubmissionStatus.WAITLISTED, "fpA"),
            ("live1", "O", SubmissionStatus.QUEUED, "fpA"),
            ("live2", "A1", SubmissionStatus.SCORED, "fpA"),
            ("dead", "O", SubmissionStatus.REJECTED, "fpA"),
            ("champ", "O", SubmissionStatus.ADOPTED, "fpA"),
            ("other", "B", SubmissionStatus.WAITLISTED, "fpB"),
            ("benched", "B", SubmissionStatus.SCORED, "fpC"),
        ]:
            sub = st.create(
                repo_url="src://x", commit_hash="c-" + tag, epoch=1,
                hotkey=hk, round_id="r1", max_per_round=0,
                max_total_per_round=0,
            )
            st.update_status(sub.submission_id, status)
            st.set_structural_fingerprint(sub.submission_id, fp)
        return st

    def test_live_statuses_in_terminal_and_adopted_out(self, store):
        rows = store.structural_fingerprint_live("fpA")
        hotkeys = sorted(hk for hk, _sid, _st in rows)
        # Only the QUEUED row holds a seat. REJECTED, ADOPTED (champion
        # iteration!), WAITLISTED and SCORED are all excluded.
        assert hotkeys == ["O"]

    def test_waitlisted_is_not_live(self, store):
        """A WAITLISTED row must not hold a structure.

        Rotation is round-scoped and treats waitlisted as TERMINAL, and
        nothing ever transitions a row out of it — so counting it live made
        "resubmit next round" (rotation) and "wait for <id> to resolve"
        (dedup) contradict, permanently locking a once-waitlisted miner out
        of resubmitting that solver.
        """
        statuses = {st for _hk, _sid, st in store.structural_fingerprint_live("fpA")}
        assert "waitlisted" not in statuses
        # fpB's ONLY row is waitlisted -> the structure is fully freed.
        assert store.structural_fingerprint_live("fpB") == []

    def test_scored_is_not_live(self, store):
        """A SCORED row must not hold a structure either.

        It has already been benchmarked: it consumed its queue seat and
        released it, and what it still contends for is ADOPTION — which is
        excluded above for exactly this reason. Nothing transitions a row out
        of SCORED, so the block lifted only when age/cap pruning happened to
        drop it; the same file already lists SCORED among the terminal
        end-states safe to prune.

        Measured on gimly/UID 118 over 8 rounds: a 2-rejected / 1-benched
        cycle, every successful bench costing the miner the next two rounds.
        """
        statuses = {st for _hk, _sid, st in store.structural_fingerprint_live("fpA")}
        assert "scored" not in statuses
        # fpC's ONLY row is scored -> the structure is fully freed, so the
        # miner can resubmit the solver they just had benched.
        assert store.structural_fingerprint_live("fpC") == []

    def test_only_seat_holding_statuses_are_live(self, store):
        """The live set IS the seat-holding set — no status may drift in.

        Guards the boundary directly: WAITLISTED and SCORED both used to sit
        here and both were the same defect, found in the same audit and fixed
        one at a time.
        """
        rows = store.structural_fingerprint_live("fpA")
        assert {st for _hk, _sid, st in rows} <= {
            "queued", "screening_stage_1", "screening_stage_2",
            "screening_stage_3", "pending_selection", "benchmarking",
        }

    def test_exclude_self(self, store):
        rows = store.structural_fingerprint_live("fpA")
        some_id = rows[0][1]
        rows2 = store.structural_fingerprint_live(
            "fpA", exclude_submission_id=some_id,
        )
        assert some_id not in [sid for _hk, sid, _st in rows2]
        assert len(rows2) == len(rows) - 1

    def test_empty_fingerprint_matches_nothing(self, store):
        assert store.structural_fingerprint_live("") == []
