"""Followers self-heal missed champion broadcasts by pulling from the leader.

Round lifecycle sync is one-shot push (broadcast_json has no retry): a follower
unreachable for seconds during close/certify/activate silently desyncs and used
to stay on the no-champion fallback until an operator fired the re-attest lever
(observed fleet-wide 2026-07-02). The ChampionPullReconcile loop closes that
gap: each follower compares champions with the leader and, on divergence, pulls
the /solver/champion/sync-bundle (the same force chain the re-attest pushes)
and applies it through the same local sync functions.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from minotaur_subnet.api.routes.submissions import champion_consensus as cc
from minotaur_subnet.api.routes.submissions import round_manager as rm
from minotaur_subnet.api.routes.submissions import state as sub_state
from minotaur_subnet.api.routes.submissions.champion_reconcile import (
    ChampionPullReconcile,
)

LEADER_CHAMPION = {
    "submission_id": "sub_new",
    "activated_round_id": "round-e2-n1",
}
BUNDLE = {
    "submission_id": "sub_new",
    "round_id": "round-e2-n1",
    "close": {"round_id": "round-e2-n1", "close_epoch": 5, "force": True},
    "certify": {"round_id": "round-e2-n1", "effective_epoch": 7, "force": True},
    "activate": {"round_id": "round-e2-n1", "activation_epoch": 7, "champion_changed": True},
}


def _reconciler(http, *, follower=True, leader="http://leader:8080", api_key="k"):
    return ChampionPullReconcile(
        leader_api_url=lambda: leader,
        is_follower=lambda: follower,
        api_key=api_key,
        http_get_json=http,
    )


def _fake_http(champion=LEADER_CHAMPION, bundle=BUNDLE):
    calls = []

    async def get_json(url, headers=None):
        calls.append((url, headers))
        if url.endswith("/v1/solver/champion"):
            return champion
        if url.endswith("/v1/solver/champion/sync-bundle"):
            return bundle
        raise AssertionError(f"unexpected URL {url}")

    get_json.calls = calls
    return get_json


def _patch_local_champion(monkeypatch, submission_id, round_id):
    champ = (
        SimpleNamespace(submission_id=submission_id, activated_round_id=round_id)
        if submission_id
        else None
    )
    monkeypatch.setattr(
        sub_state, "get_round_store",
        lambda: SimpleNamespace(get_active_champion=lambda: champ),
    )


def _patch_appliers(monkeypatch):
    applied = []

    def close(body):
        applied.append(("close", body))

    async def certify(body):
        applied.append(("certify", body))

    async def activate(body):
        applied.append(("activate", body))
        return {"champion_changed": True}

    monkeypatch.setattr(rm, "_sync_close_solver_round_state", close)
    monkeypatch.setattr(cc, "_sync_certified_round_state", certify)
    monkeypatch.setattr(rm, "_activate_solver_round_state", activate)
    return applied


@pytest.mark.asyncio
async def test_heals_on_divergence(monkeypatch):
    """Stale local champion -> pulls the bundle and applies close/certify/activate."""
    _patch_local_champion(monkeypatch, "sub_old", "round-e1-n1")
    applied = _patch_appliers(monkeypatch)
    http = _fake_http()

    assert await _reconciler(http).reconcile_once() is True
    assert [phase for phase, _ in applied] == ["close", "certify", "activate"]
    close_body = applied[0][1]
    assert close_body.round_id == "round-e2-n1" and close_body.force is True
    assert applied[1][1].force is True
    assert applied[2][1].activation_epoch == 7
    # The bundle GET is authenticated with the shared internal key.
    bundle_call = [c for c in http.calls if c[0].endswith("sync-bundle")]
    assert bundle_call[0][1] == {"x-solver-round-internal-key": "k"}


@pytest.mark.asyncio
async def test_no_local_champion_still_heals(monkeypatch):
    """A follower with NO champion at all (fresh /data) converges too."""
    _patch_local_champion(monkeypatch, None, None)
    applied = _patch_appliers(monkeypatch)

    assert await _reconciler(_fake_http()).reconcile_once() is True
    assert len(applied) == 3


@pytest.mark.asyncio
async def test_in_sync_is_a_cheap_noop(monkeypatch):
    """Matching champion -> one status GET, no bundle fetch, no applies."""
    _patch_local_champion(monkeypatch, "sub_new", "round-e2-n1")
    applied = _patch_appliers(monkeypatch)
    http = _fake_http()

    assert await _reconciler(http).reconcile_once() is False
    assert applied == []
    assert len(http.calls) == 1  # only /v1/solver/champion


@pytest.mark.asyncio
async def test_gates(monkeypatch):
    """No-ops: on the leader, with no leader URL, or leader without a champion."""
    _patch_local_champion(monkeypatch, "sub_old", "round-e1-n1")
    applied = _patch_appliers(monkeypatch)

    assert await _reconciler(_fake_http(), follower=False).reconcile_once() is False
    assert await _reconciler(_fake_http(), leader=None).reconcile_once() is False
    no_champ = _fake_http(champion={"submission_id": None, "activated_round_id": None})
    assert await _reconciler(no_champ).reconcile_once() is False
    assert applied == []


def test_reattest_chain_payloads_sets_force(monkeypatch):
    """Push and pull share one chain builder: force on close+certify, activate shape."""
    monkeypatch.setattr(rm, "_close_round_sync_payload", lambda s: {"round_id": s.round_id})
    monkeypatch.setattr(rm, "_certify_round_sync_payload", lambda s: {"round_id": s.round_id})
    state = SimpleNamespace(round_id="round-e2-n1", effective_epoch=7, close_epoch=5)

    chain = rm._reattest_chain_payloads(state)
    assert chain["close"] == {"round_id": "round-e2-n1", "force": True}
    assert chain["certify"] == {"round_id": "round-e2-n1", "force": True}
    assert chain["activate"] == {
        "round_id": "round-e2-n1",
        "activation_epoch": 7,
        "champion_changed": True,
    }
