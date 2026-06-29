"""Unit tests for the emergency force-sync ('reattach') path.

When a follower has moved past (or pruned) the standing champion's round, the normal
adopt-if-behind sync refuses to re-install it (it's older-or-equal to the follower's
current round) — so the champion re-attest 404s the bare cert and the follower stays on
burn. The operator force-sync sets force=True to bypass that staleness guard and
re-install the round, so the follower can re-adopt the champion. Pure store logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.round_store import RoundStore, RoundStatus
from minotaur_subnet.api.routes.submissions.round_manager import (
    _adopt_leader_round_if_behind,
)
from minotaur_subnet.api.routes.submissions.state import set_round_store
from minotaur_subnet.api.routes.submissions.models import CloseRoundRequest


class TestForceAdopt:
    def test_normal_sync_refuses_older_round(self):
        rs = RoundStore()
        rs.ensure_open_round(opened_epoch=29712653)  # follower is AHEAD
        set_round_store(rs)
        try:
            adopted = _adopt_leader_round_if_behind(
                "round-e29712558-n1", status=RoundStatus.CLOSED,
                force=False, close_epoch=29712558,
            )
            assert adopted is False
            assert rs.get_round("round-e29712558-n1") is None
        finally:
            set_round_store(None)

    def test_force_adopts_older_round(self):
        rs = RoundStore()
        rs.ensure_open_round(opened_epoch=29712653)  # follower is AHEAD
        set_round_store(rs)
        try:
            adopted = _adopt_leader_round_if_behind(
                "round-e29712558-n1", status=RoundStatus.CLOSED,
                force=True, close_epoch=29712558,
            )
            assert adopted is True
            installed = rs.get_round("round-e29712558-n1")
            assert installed is not None
            assert installed.status == RoundStatus.CLOSED
        finally:
            set_round_store(None)

    def test_force_noop_when_already_present(self):
        # Already holds the round -> force is a no-op; the handler resolves it normally.
        rs = RoundStore()
        cur = rs.ensure_open_round(opened_epoch=29712558)
        set_round_store(rs)
        try:
            adopted = _adopt_leader_round_if_behind(
                cur.round_id, status=RoundStatus.CLOSED,
                force=True, close_epoch=29712558,
            )
            assert adopted is False
        finally:
            set_round_store(None)


class TestCloseRequestForce:
    def test_force_default_false(self):
        assert CloseRoundRequest(close_epoch=5).force is False

    def test_force_settable(self):
        assert CloseRoundRequest(close_epoch=5, force=True).force is True
