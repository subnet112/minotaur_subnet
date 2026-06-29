"""Shadow champion-quorum (observe-only): followers do real benchmark/verify/sign
work, the leader computes a 6000bps shadow verdict over the collected approvals —
WITHOUT ever touching the live cert. Tests the gate helpers, the ceil-div, and the
post-cert harvest (incl. the critical collector=None no-leak property + swallow-all).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.api.routes.submissions.champion_consensus import (  # noqa: E402
    shadow_champion_quorum_enabled,
    shadow_champion_quorum_bps,
    _shadow_quorum_required,
    _run_shadow_champion_quorum,
)


# ── gate ──────────────────────────────────────────────────────────────────────

def test_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SHADOW_CHAMPION_QUORUM", raising=False)
    assert shadow_champion_quorum_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "FALSE", "Off"])
def test_enabled_off_values(monkeypatch, v):
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM", v)
    assert shadow_champion_quorum_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "on", "yes", "anything"])
def test_enabled_on_values(monkeypatch, v):
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM", v)
    assert shadow_champion_quorum_enabled() is True


def test_bps_default_and_override(monkeypatch):
    monkeypatch.delenv("SHADOW_CHAMPION_QUORUM_BPS", raising=False)
    assert shadow_champion_quorum_bps() == 6000
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM_BPS", "5100")
    assert shadow_champion_quorum_bps() == 5100
    monkeypatch.setenv("SHADOW_CHAMPION_QUORUM_BPS", "garbage")
    assert shadow_champion_quorum_bps() == 6000  # defensive fallback


@pytest.mark.parametrize("n,bps,exp", [
    (5, 6000, 3),   # 60% of 5 -> 3
    (5, 2000, 1),   # matches the live quorum formula
    (0, 6000, 1),   # floor at 1
    (5, 10000, 5),  # unanimity
    (5, 5001, 3),   # ceil-div boundary
    (7, 6000, 5),   # 60% of 7 -> 5
])
def test_shadow_quorum_required(n, bps, exp):
    assert _shadow_quorum_required(n, bps) == exp


# ── post-cert harvest ─────────────────────────────────────────────────────────

def _ap(vid):
    return SimpleNamespace(validator_id=vid)


@pytest.mark.asyncio
async def test_harvest_populates_health_and_passes_collector_none(monkeypatch):
    from minotaur_subnet.api.server_context import ctx
    ctx.last_shadow_champion_quorum = {}
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
    pn.broadcast_champion_proposal = AsyncMock(return_value=[_ap("0xA"), _ap("0xB")])
    rs = SimpleNamespace(close_epoch=10, decision_deadline_epoch=50, committee_block=8500000)

    await _run_shadow_champion_quorum(proposal, leader_result, cm, pn, rs)

    # CRITICAL no-leak property: the shadow broadcast must use collector=None.
    assert pn.broadcast_champion_proposal.call_args.kwargs["collector"] is None
    assert pn.broadcast_champion_proposal.call_args.kwargs["request_timeout"] is not None

    rec = ctx.last_shadow_champion_quorum
    assert rec["collected"] == 3                 # leader + 2 followers, deduped
    assert rec["validator_count"] == 5
    assert rec["shadow_bps"] == 6000
    assert rec["shadow_quorum_required"] == 3     # 60% of 5
    assert rec["reached"] is True                 # 3 >= 3
    assert rec["live_reached"] is True
    assert rec["agree_with_live"] is True
    assert set(rec["signers"]) == {"0xLEAD", "0xA", "0xB"}


@pytest.mark.asyncio
async def test_harvest_dedups_duplicate_signers(monkeypatch):
    from minotaur_subnet.api.server_context import ctx
    ctx.last_shadow_champion_quorum = {}
    proposal = SimpleNamespace(round_id="r", candidate_submission_id="s", candidate_image_id="i")
    leader_result = SimpleNamespace(approvals=[_ap("0xLEAD")], reached=True)
    cm = SimpleNamespace(protocol_config=SimpleNamespace(on_chain_validator_count=5),
                         validators=["0xLEAD"], quorum_required=1)
    pn = MagicMock()
    # duplicate of the leader + duplicate follower must collapse to unique signers
    pn.broadcast_champion_proposal = AsyncMock(return_value=[_ap("0xlead"), _ap("0xA"), _ap("0xA")])
    rs = SimpleNamespace(close_epoch=1, decision_deadline_epoch=2, committee_block=1)
    await _run_shadow_champion_quorum(proposal, leader_result, cm, pn, rs)
    assert ctx.last_shadow_champion_quorum["collected"] == 2  # leader + 0xA


@pytest.mark.asyncio
async def test_harvest_swallows_broadcast_error(monkeypatch):
    # A failed/timed-out broadcast must NOT raise and must record leader-only.
    from minotaur_subnet.api.server_context import ctx
    ctx.last_shadow_champion_quorum = {}
    proposal = SimpleNamespace(round_id="r2", candidate_submission_id="s", candidate_image_id="i")
    leader_result = SimpleNamespace(approvals=[_ap("0xLEAD")], reached=True)
    cm = SimpleNamespace(protocol_config=SimpleNamespace(on_chain_validator_count=5),
                         validators=["0xLEAD"], quorum_required=1)
    pn = MagicMock()
    pn.broadcast_champion_proposal = AsyncMock(side_effect=RuntimeError("peers down"))
    rs = SimpleNamespace(close_epoch=1, decision_deadline_epoch=2, committee_block=1)

    await _run_shadow_champion_quorum(proposal, leader_result, cm, pn, rs)  # must not raise

    rec = ctx.last_shadow_champion_quorum
    assert rec["collected"] == 1            # leader only
    assert rec["reached"] is False          # 1 < 3
    assert rec["live_reached"] is True
    assert rec["agree_with_live"] is False  # shadow disagrees with live
