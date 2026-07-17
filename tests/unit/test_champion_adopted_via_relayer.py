"""Tests for the leader client ``solver_repo.on_champion_adopted_via_relayer``.

The leader POSTs the certificate to ``{RELAYER_URL}/v1/finalize-champion`` and
returns the relayer's boolean ``merge_ok`` verdict. The #326 adoption gate gates
on that bool, so this function MUST fail-closed (return False) on any error so a
champion is never adopted on an unconfirmed merge.

We monkeypatch ``requests.post`` so no network is touched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness.round_store import ChampionApproval, ChampionCertificate
from minotaur_subnet.relayer.solver_repo import (
    _relayer_ready,
    on_champion_adopted_via_relayer,
)

ROUND_ID = "round-client-001"
SUBMISSION_ID = "sub_xyz789"
COMMIT_HASH = "a" * 40


def _submission(commit_hash: str = COMMIT_HASH):
    return types.SimpleNamespace(
        submission_id=SUBMISSION_ID,
        commit_hash=commit_hash,
        pr_number=7,
    )


def _certificate() -> ChampionCertificate:
    return ChampionCertificate(
        round_id=ROUND_ID,
        candidate_submission_id=SUBMISSION_ID,
        quorum_required=2,
        approvals=[
            ChampionApproval(
                validator_id="0x" + "11" * 20,
                round_id=ROUND_ID,
                candidate_submission_id=SUBMISSION_ID,
                commit_hash=COMMIT_HASH,
                signature="0x" + "ab" * 65,
            ),
        ],
    )


def _env() -> dict:
    return {
        "RELAYER_URL": "http://relayer:8091",
        "VALIDATOR_PRIVATE_KEY": Account.create().key.hex(),
        "CHAMPION_CONSENSUS_CHAIN_ID": "964",
    }


def _resp(status: int, payload: dict | None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if payload is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = payload
    return r


@pytest.fixture(autouse=True)
def _relayer_health_ok():
    """Default the finalize health-gate to READY so the POST-path tests exercise the
    POST. Individual tests override ``requests.get`` to simulate a down relayer."""
    with patch("requests.get", MagicMock(return_value=_resp(200, {"status": "ok"}))):
        yield


def test_merge_ok_true_returns_true():
    # v2 reply shape: {ok, reason: null, ...}
    post = MagicMock(return_value=_resp(200, {"ok": True, "reason": None, "round_id": ROUND_ID}))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert result
    assert result.reason == ""  # success carries no reason
    post.assert_called_once()
    # The client prefers the structured v2 endpoint.
    _, kwargs = post.call_args
    assert post.call_args.args[0].endswith("/v2/finalize-champion")
    body = kwargs["json"]
    assert body["round_id"] == ROUND_ID
    assert body["submission"]["submission_id"] == SUBMISSION_ID
    assert body["submission"]["commit_hash"] == COMMIT_HASH
    assert body["certificate"]["round_id"] == ROUND_ID
    assert "wrapper" in body and "wrapper_signature" in body


def test_merge_ok_false_returns_false():
    # v2 reply: structured reason {code, stage, detail} → propagated to the caller.
    post = MagicMock(return_value=_resp(200, {
        "ok": False,
        "reason": {"code": "quorum_not_reached", "stage": "quorum", "detail": "quorum not reached: 1/2"},
    }))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result
    assert result.reason == "quorum_not_reached"  # code propagated (.reason aliases .code)
    assert result.stage == "quorum"


def test_v2_404_falls_back_to_v1():
    # A relayer still on an older image (no /v2) → client retries /v1 and honors it.
    def _post(url, **_kw):
        if url.endswith("/v2/finalize-champion"):
            return _resp(404, {"error": "not found"})
        return _resp(200, {"merge_ok": True})  # v1 minimal shape
    post = MagicMock(side_effect=_post)
    with patch.dict("os.environ", _env(), clear=False), patch("requests.post", post):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert result  # adopted via the v1 fallback
    assert post.call_count == 2  # tried v2, then v1
    assert post.call_args_list[0].args[0].endswith("/v2/finalize-champion")
    assert post.call_args_list[1].args[0].endswith("/v1/finalize-champion")


def test_post_raises_returns_false_fail_closed():
    post = MagicMock(side_effect=TimeoutError("relayer unreachable"))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result


def test_non_200_returns_false_fail_closed():
    post = MagicMock(return_value=_resp(503, {"merge_ok": True}))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result


def test_bad_json_returns_false_fail_closed():
    post = MagicMock(return_value=_resp(200, None))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result


def test_non_git_submission_returns_false_without_posting():
    post = MagicMock(return_value=_resp(200, {"merge_ok": True}))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(commit_hash=""), ROUND_ID, certificate=_certificate(),
        )
    assert not result
    post.assert_not_called()


# ── Relayer health-gate (Part 3a): defer, not abort, when the relayer isn't ready ──
def test_relayer_ready_true_on_200():
    with patch("requests.get", MagicMock(return_value=_resp(200, {"status": "ok"}))):
        assert _relayer_ready("http://relayer:8091") is True


def test_relayer_ready_false_on_exception():
    with patch("requests.get", MagicMock(side_effect=ConnectionError("no route"))):
        assert _relayer_ready("http://relayer:8091") is False


def test_relayer_ready_false_on_non_2xx():
    with patch("requests.get", MagicMock(return_value=_resp(503, {"status": "starting"}))):
        assert _relayer_ready("http://relayer:8091") is False


def test_health_gate_defers_without_posting_when_relayer_down():
    # /health unreachable => stage="client" (the #326 merge-gate DEFERS, not aborts) and
    # we NEVER POST a finalize the relayer might half-apply (the 2026-07-17 orphan).
    post = MagicMock(return_value=_resp(200, {"ok": True, "reason": None}))
    with patch.dict("os.environ", _env(), clear=False), \
            patch("requests.get", MagicMock(side_effect=TimeoutError("relayer down"))), \
            patch("requests.post", post):
        result = on_champion_adopted_via_relayer(_submission(), ROUND_ID, certificate=_certificate())
    assert not result
    assert result.reason == "relayer_unready"
    assert result.stage == "client"  # => Part-1 merge-gate DEFERS
    post.assert_not_called()


def test_health_gate_can_be_disabled():
    # RELAYER_HEALTH_GATE=0 skips the gate: the finalize POSTs even if /health is down.
    env = _env()
    env["RELAYER_HEALTH_GATE"] = "0"
    post = MagicMock(return_value=_resp(200, {"ok": True, "reason": None}))
    with patch.dict("os.environ", env, clear=False), \
            patch("requests.get", MagicMock(side_effect=TimeoutError("relayer down"))), \
            patch("requests.post", post):
        result = on_champion_adopted_via_relayer(_submission(), ROUND_ID, certificate=_certificate())
    assert result  # adopted — gate disabled, POST proceeded
    post.assert_called_once()


def test_no_relayer_url_returns_false_without_posting():
    env = _env()
    env["RELAYER_URL"] = ""
    post = MagicMock(return_value=_resp(200, {"merge_ok": True}))
    with patch.dict("os.environ", env, clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result
    post.assert_not_called()
