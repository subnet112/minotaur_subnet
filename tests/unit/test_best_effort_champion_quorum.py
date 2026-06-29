"""Best-effort champion quorum (replaces the shadow approach).

Leader certs fast at the floor (1) and never deadlocks; a monitor-only post-cert
harvest records which validators approved the certified champion vs which are
missing (n-of-target), with collector=None so it can never reach the live cert.
Followers self-adopt champion weights ONLY when opted in + they verified it
themselves (provenance via RoundState.self_verified). Tests the leader monitor
helpers/harvest + the follower provenance + gate.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.api.routes.submissions.champion_consensus import (  # noqa: E402
    best_effort_champion_quorum_enabled,
    best_effort_champion_quorum_target_bps,
    _quorum_required_at,
    _run_best_effort_champion_quorum,
)
from minotaur_subnet.epoch.manager import _follower_weight_adopt_enabled  # noqa: E402
from minotaur_subnet.harness.round_store import RoundState, RoundStore  # noqa: E402
from minotaur_subnet.api.routes.submissions.round_manager import (  # noqa: E402
    _activate_round_sync_payload,
)
from minotaur_subnet.api.routes.submissions.models import ActivateRoundRequest  # noqa: E402


# ── leader monitor gate ─────────────────────────────────────────────────────

def test_enabled_default_on(monkeypatch):
    monkeypatch.delenv("BEST_EFFORT_CHAMPION_QUORUM", raising=False)
    monkeypatch.delenv("SHADOW_CHAMPION_QUORUM", raising=False)
    assert best_effort_champion_quorum_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "Off"])
def test_enabled_off_values(monkeypatch, v):
    monkeypatch.setenv("BEST_EFFORT_CHAMPION_QUORUM", v)
    assert best_effort_champion_quorum_enabled() is False


def test_enabled_legacy_alias(monkeypatch):
    monkeypatch.delenv("BEST_EFFORT_CHAMPION_QUORUM", raising=False)
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM", "0")  # legacy name still honoured
    assert best_effort_champion_quorum_enabled() is False


def test_target_bps_default_override_alias(monkeypatch):
    monkeypatch.delenv("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS", raising=False)
    monkeypatch.delenv("SHADOW_CHAMPION_QUORUM_BPS", raising=False)
    assert best_effort_champion_quorum_target_bps() == 6000
    monkeypatch.setenv("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS", "8000")
    assert best_effort_champion_quorum_target_bps() == 8000
    monkeypatch.setenv("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS", "garbage")
    assert best_effort_champion_quorum_target_bps() == 6000  # defensive fallback
    monkeypatch.delenv("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS", raising=False)
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM_BPS", "5100")  # legacy alias
    assert best_effort_champion_quorum_target_bps() == 5100


@pytest.mark.parametrize("n,bps,exp", [
    (5, 6000, 3), (5, 2000, 1), (0, 6000, 1), (5, 10000, 5), (7, 6000, 5),
])
def test_quorum_required_at(n, bps, exp):
    assert _quorum_required_at(n, bps) == exp


# ── leader post-cert harvest ─────────────────────────────────────────────────

def _ap(vid):
    return SimpleNamespace(validator_id=vid)


def _peer(vid):
    return SimpleNamespace(validator_id=vid)


@pytest.mark.asyncio
async def test_harvest_records_approved_missing_and_collector_none(monkeypatch):
    from minotaur_subnet.api.server_context import ctx
    ctx.last_best_effort_champion_quorum = {}
    monkeypatch.delenv("BEST_EFFORT_CHAMPION_QUORUM_TARGET_BPS", raising=False)
    monkeypatch.delenv("SHADOW_CHAMPION_QUORUM_BPS", raising=False)

    proposal = SimpleNamespace(round_id="r1", candidate_submission_id="sub_x",
                               candidate_image_id="sha256:img")
    leader_result = SimpleNamespace(approvals=[_ap("0xLEAD")], reached=True)
    cm = SimpleNamespace(
        protocol_config=SimpleNamespace(on_chain_validator_count=5),
        validators=["0xLEAD", "0xA", "0xB", "0xC", "0xD"],
        quorum_required=1,
    )
    pn = MagicMock()
    pn.peers = [_peer("0xA"), _peer("0xB"), _peer("0xC")]
    pn.broadcast_champion_proposal = AsyncMock(return_value=[_ap("0xA")])  # only A approves
    rs = SimpleNamespace(close_epoch=10, decision_deadline_epoch=50, committee_block=8500000)

    await _run_best_effort_champion_quorum(proposal, leader_result, cm, pn, rs)

    # CRITICAL no-leak: harvest must broadcast with collector=None + a long timeout.
    assert pn.broadcast_champion_proposal.call_args.kwargs["collector"] is None
    assert pn.broadcast_champion_proposal.call_args.kwargs["request_timeout"] is not None

    rec = ctx.last_best_effort_champion_quorum
    assert rec["collected"] == 2                       # leader + A
    assert set(rec["approved"]) == {"0xLEAD", "0xA"}
    assert set(rec["missing"]) == {"0xB", "0xC"}       # peers that did NOT approve
    assert rec["validator_count"] == 5
    assert rec["target_required"] == 3                 # 60% of 5
    assert rec["would_reach_at_target"] is False       # 2 < 3
    assert rec["live_reached"] is True
    assert rec["live_quorum_required"] == 1            # floor — leader self-certs


@pytest.mark.asyncio
async def test_harvest_floor_warning_when_quorum_gt_1(monkeypatch, caplog):
    from minotaur_subnet.api.server_context import ctx
    ctx.last_best_effort_champion_quorum = {}
    proposal = SimpleNamespace(round_id="r", candidate_submission_id="s", candidate_image_id="i")
    leader_result = SimpleNamespace(approvals=[_ap("0xLEAD")], reached=True)
    cm = SimpleNamespace(protocol_config=SimpleNamespace(on_chain_validator_count=6),
                         validators=["0xLEAD"], quorum_required=2)  # >1 => floor breached
    pn = MagicMock()
    pn.peers = []
    pn.broadcast_champion_proposal = AsyncMock(return_value=[])
    rs = SimpleNamespace(close_epoch=1, decision_deadline_epoch=2, committee_block=1)

    import logging
    with caplog.at_level(logging.WARNING):
        await _run_best_effort_champion_quorum(proposal, leader_result, cm, pn, rs)

    assert ctx.last_best_effort_champion_quorum["live_quorum_required"] == 2
    assert any("floor breached" in r.message or "quorum_required=2" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_harvest_swallows_broadcast_error(monkeypatch):
    from minotaur_subnet.api.server_context import ctx
    ctx.last_best_effort_champion_quorum = {}
    proposal = SimpleNamespace(round_id="r2", candidate_submission_id="s", candidate_image_id="i")
    leader_result = SimpleNamespace(approvals=[_ap("0xLEAD")], reached=True)
    cm = SimpleNamespace(protocol_config=SimpleNamespace(on_chain_validator_count=5),
                         validators=["0xLEAD"], quorum_required=1)
    pn = MagicMock()
    pn.peers = [_peer("0xA")]
    pn.broadcast_champion_proposal = AsyncMock(side_effect=RuntimeError("peers down"))
    rs = SimpleNamespace(close_epoch=1, decision_deadline_epoch=2, committee_block=1)

    await _run_best_effort_champion_quorum(proposal, leader_result, cm, pn, rs)  # must not raise

    rec = ctx.last_best_effort_champion_quorum
    assert rec["collected"] == 1            # leader only
    assert set(rec["missing"]) == {"0xA"}   # the unreachable peer
    assert rec["would_reach_at_target"] is False


# ── follower provenance + gate ───────────────────────────────────────────────

def test_follower_weight_adopt_default_on(monkeypatch):
    # Default ON — third-party validators won't set env vars; shipping the code enables it.
    monkeypatch.delenv("FOLLOWER_CHAMPION_WEIGHT_ADOPT", raising=False)
    assert _follower_weight_adopt_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "Off"])
def test_follower_weight_adopt_off_values(monkeypatch, v):
    monkeypatch.setenv("FOLLOWER_CHAMPION_WEIGHT_ADOPT", v)
    assert _follower_weight_adopt_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on"])
def test_follower_weight_adopt_on_values(monkeypatch, v):
    monkeypatch.setenv("FOLLOWER_CHAMPION_WEIGHT_ADOPT", v)
    assert _follower_weight_adopt_enabled() is True


def test_round_state_self_verified_roundtrips():
    st = RoundState(round_id="r", self_verified=True, self_verified_submission_id="sub_A")
    rt = RoundState.from_dict(st.to_dict())
    assert rt.self_verified is True
    assert rt.self_verified_submission_id == "sub_A"  # candidate-bound
    # default is False / None (no accidental verification)
    blank = RoundState.from_dict(RoundState(round_id="r2").to_dict())
    assert blank.self_verified is False
    assert blank.self_verified_submission_id is None


# ── #4 champion_changed plumbing (follower refuses a leader-rejected champion) ──

def test_activate_sync_payload_carries_champion_changed():
    body = ActivateRoundRequest(round_id="r", activation_epoch=5)
    # explicit leader outcome is carried (True adopts, False => follower refuses)
    assert _activate_round_sync_payload(body, True)["champion_changed"] is True
    assert _activate_round_sync_payload(body, False)["champion_changed"] is False
    # None + body has none => field OMITTED (old-follower-safe; never strands)
    assert "champion_changed" not in _activate_round_sync_payload(body, None)
    # falls back to the body value when not supplied explicitly
    body2 = ActivateRoundRequest(round_id="r", activation_epoch=5, champion_changed=False)
    assert _activate_round_sync_payload(body2)["champion_changed"] is False


def test_activate_round_request_parses_champion_changed():
    # absent => None (mixed-version: follower keeps legacy adopt, never stranded)
    assert ActivateRoundRequest(round_id="r", activation_epoch=1).champion_changed is None
    assert ActivateRoundRequest(
        round_id="r", activation_epoch=1, champion_changed=False,
    ).champion_changed is False


def test_mark_self_verified_binds_candidate_and_persists(tmp_path):
    path = tmp_path / "rounds.json"
    store = RoundStore(persist_path=path)
    store.ensure_open_round(opened_epoch=1)
    rid = store.get_current_round().round_id
    assert store.get_round(rid).self_verified_submission_id is None
    store.mark_self_verified(rid, "sub_A")
    assert store.get_round(rid).self_verified is True
    assert store.get_round(rid).self_verified_submission_id == "sub_A"
    # survives a reload from disk (separate store on the same path)
    reloaded = RoundStore(persist_path=path)
    assert reloaded.get_round(rid).self_verified_submission_id == "sub_A"
