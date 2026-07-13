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
from minotaur_subnet.relayer.solver_repo import on_champion_adopted_via_relayer

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


def test_merge_ok_true_returns_true():
    post = MagicMock(return_value=_resp(200, {"merge_ok": True, "round_id": ROUND_ID}))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert result
    assert result.reason == ""  # success carries no reason
    post.assert_called_once()
    # Sanity: the POST went to the finalize endpoint with the cert + submission.
    _, kwargs = post.call_args
    assert post.call_args.args[0].endswith("/v1/finalize-champion")
    body = kwargs["json"]
    assert body["round_id"] == ROUND_ID
    assert body["submission"]["submission_id"] == SUBMISSION_ID
    assert body["submission"]["commit_hash"] == COMMIT_HASH
    assert body["certificate"]["round_id"] == ROUND_ID
    assert "wrapper" in body and "wrapper_signature" in body


def test_merge_ok_false_returns_false():
    post = MagicMock(return_value=_resp(200, {"merge_ok": False, "reason": "quorum not reached"}))
    with patch.dict("os.environ", _env(), clear=False), patch(
        "requests.post", post,
    ):
        result = on_champion_adopted_via_relayer(
            _submission(), ROUND_ID, certificate=_certificate(),
        )
    assert not result
    assert result.reason == "quorum not reached"  # relayer's reason propagated to the caller


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
