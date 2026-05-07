"""Unit tests for the solver round store."""

from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.harness.round_store import (
    ChampionApproval,
    ChampionCertificate,
    ChampionSnapshot,
    RoundStatus,
    RoundStore,
)


def test_ensure_open_round_bootstraps_first_round():
    store = RoundStore()

    current = store.ensure_open_round(opened_epoch=7)

    assert current.round_id == "round-e7-n1"
    assert current.status == RoundStatus.OPEN
    assert current.opened_epoch == 7
    assert current.accepting_submissions() is True


def test_close_current_round_marks_closed():
    store = RoundStore()
    opened = store.ensure_open_round(opened_epoch=3)

    closed = store.close_current_round(close_epoch=4)

    assert closed.round_id == opened.round_id
    assert closed.status == RoundStatus.CLOSED
    assert closed.close_epoch == 4
    assert closed.accepting_submissions() is False


def test_set_active_champion_syncs_open_round_incumbent():
    store = RoundStore()
    current = store.ensure_open_round(opened_epoch=9)

    champion = ChampionSnapshot(
        submission_id="sub_champion",
        image_id="sha256:" + "a" * 64,
        solver_name="solver-x",
        hotkey="5Gminer",
    )
    store.set_active_champion(champion, sync_open_round=True)

    refreshed = store.get_current_round()
    assert refreshed is not None
    assert refreshed.round_id == current.round_id
    assert refreshed.incumbent_submission_id == "sub_champion"
    assert refreshed.incumbent_image_id == "sha256:" + "a" * 64
    assert refreshed.incumbent_hotkey == "5Gminer"


def test_round_store_persists_and_loads():
    with tempfile.TemporaryDirectory() as tmpdir:
        persist_path = Path(tmpdir) / "solver_rounds.json"
        store1 = RoundStore(persist_path=persist_path)
        store1.ensure_open_round(opened_epoch=11)
        store1.set_active_champion(
            ChampionSnapshot(
                submission_id="sub_active",
                image_id="sha256:" + "b" * 64,
                solver_name="solver-y",
                hotkey="5Ghotkey",
                activated_round_id="round-e10-n1",
                activated_epoch=10,
                activated_at=123.0,
            ),
            sync_open_round=True,
        )

        store2 = RoundStore(persist_path=persist_path)
        current = store2.get_current_round()
        champion = store2.get_active_champion()

        assert current is not None
        assert current.round_id == "round-e11-n1"
        assert champion.submission_id == "sub_active"
        assert champion.image_id == "sha256:" + "b" * 64


def test_set_round_status_and_activate_round():
    store = RoundStore()
    current = store.ensure_open_round(opened_epoch=5)
    closed = store.close_current_round(close_epoch=5)
    replaying = store.set_round_status(closed.round_id, RoundStatus.REPLAYING)
    activated = store.activate_round(closed.round_id, effective_epoch=6)

    assert replaying.status == RoundStatus.REPLAYING
    assert activated.status == RoundStatus.ACTIVATED
    assert activated.effective_epoch == 6
    assert store.get_round(current.round_id).status == RoundStatus.ACTIVATED


def test_set_finalist_and_certificate():
    store = RoundStore()
    current = store.ensure_open_round(opened_epoch=8)
    store.close_current_round(
        close_epoch=8,
        committee_hash="committee-1",
        benchmark_pack_hash="pack-1",
        quorum_required=2,
    )
    finalist = store.set_round_finalist(
        current.round_id,
        submission_id="sub_finalist",
        image_id="sha256:" + "c" * 64,
        benchmark_score=0.91,
        shadow_case_log_hash="shadow-1",
    )
    certified = store.certify_round(
        current.round_id,
        ChampionCertificate(
            round_id=current.round_id,
            committee_hash="committee-1",
            candidate_submission_id="sub_finalist",
            candidate_image_id="sha256:" + "c" * 64,
            incumbent_image_id="sha256:" + "a" * 64,
            benchmark_pack_hash="pack-1",
            shadow_case_log_hash="shadow-1",
            effective_epoch=9,
            quorum_required=2,
            approvals=[
                ChampionApproval(
                    validator_id="0x1",
                    round_id=current.round_id,
                    candidate_submission_id="sub_finalist",
                    candidate_image_id="sha256:" + "c" * 64,
                    effective_epoch=9,
                    signature="sig1",
                ),
                ChampionApproval(
                    validator_id="0x2",
                    round_id=current.round_id,
                    candidate_submission_id="sub_finalist",
                    candidate_image_id="sha256:" + "c" * 64,
                    effective_epoch=9,
                    signature="sig2",
                ),
            ],
        ),
    )

    assert finalist.status == RoundStatus.CERTIFYING
    assert finalist.finalist_submission_id == "sub_finalist"
    assert certified.status == RoundStatus.CERTIFIED
    assert certified.certificate is not None
    assert certified.certificate.candidate_submission_id == "sub_finalist"
    assert len(certified.certificate.approvals) == 2
