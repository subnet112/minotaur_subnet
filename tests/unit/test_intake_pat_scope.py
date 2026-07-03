"""Tests for the private-path intake PAT write-scope gate.

An under-scoped (read-only) fine-grained PAT passes every read at intake and
then silently 403s when the benchmark report posts. The gate posts the intake
ACK comment with the miner's token — the same write the report needs — and
rejects definitively-forbidden tokens (401/403/404) with an actionable 400
while failing OPEN on transient signals (network, 5xx, 429).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.routes import _require_pr_comment_scope
from minotaur_subnet.relayer.solver_repo import post_intake_ack

ACK_PATH = "minotaur_subnet.relayer.solver_repo.post_intake_ack"
OWNER_REPO = ("minerdev", "solver-private")


@pytest.fixture(autouse=True)
def _clean_env():
    saved = os.environ.pop("SUBMISSION_INTAKE_ACK", None)
    yield
    if saved is None:
        os.environ.pop("SUBMISSION_INTAKE_ACK", None)
    else:
        os.environ["SUBMISSION_INTAKE_ACK"] = saved


def _gate(status: int) -> None:
    with patch(ACK_PATH, return_value=status):
        _require_pr_comment_scope(
            4, owner_repo=OWNER_REPO, token="ghp_x", round_id="round-1",
        )


# ── definitive permission failures block with the scope message ──────────


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permission_failure_rejects_with_scope_message(status: int):
    with pytest.raises(HTTPException) as exc:
        _gate(status)
    assert exc.value.status_code == 400
    assert "Pull requests: Read and write" in exc.value.detail
    assert f"GitHub {status}" in exc.value.detail
    assert "minerdev/solver-private" in exc.value.detail


# ── success and transient signals pass ───────────────────────────────────


@pytest.mark.parametrize("status", [201, 200])
def test_successful_ack_passes(status: int):
    _gate(status)  # no raise


@pytest.mark.parametrize("status", [0, 429, 500, 502])
def test_transient_failure_fails_open(status: int):
    _gate(status)  # no raise — ACK missed, submission proceeds


# ── kill-switch ──────────────────────────────────────────────────────────


def test_kill_switch_skips_probe_entirely():
    os.environ["SUBMISSION_INTAKE_ACK"] = "0"
    with patch(ACK_PATH) as ack:
        _require_pr_comment_scope(
            4, owner_repo=OWNER_REPO, token="ghp_x", round_id="round-1",
        )
    ack.assert_not_called()


# ── post_intake_ack targets the miner's repo, never the canonical one ────


def test_post_intake_ack_targets_private_repo_and_returns_status():
    with patch(
        "minotaur_subnet.relayer.solver_repo._github_api_request",
        return_value=(403, None),
    ) as req:
        status = post_intake_ack(
            7, owner_repo=OWNER_REPO, token="ghp_x", round_id="round-9",
        )
    assert status == 403
    method, url = req.call_args.args[0], req.call_args.args[1]
    assert method == "POST"
    assert url == (
        "https://api.github.com/repos/minerdev/solver-private/issues/7/comments"
    )
    assert req.call_args.kwargs["token"] == "ghp_x"
    assert "round-9" in req.call_args.args[2]["body"]
