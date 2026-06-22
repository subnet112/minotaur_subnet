"""Unit tests for the miner git-based submission pipeline."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minotaur_subnet.miner.main import (
    submit_solver_git,
    poll_submission_status,
)


class _FakeContextManager:
    """Helper to create a fake async context manager for aiohttp responses."""
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class TestGitSubmit:
    @pytest.mark.asyncio
    async def test_submit_solver_git_round_based(self):
        """submit_solver_git discovers the round from /v1/solver/round and signs
        round-based — there is no /v1/status epoch fallback."""
        # Mock /v1/solver/round: open + accepting, opened_epoch=5
        mock_round_resp = AsyncMock()
        mock_round_resp.status = 200
        mock_round_resp.json = AsyncMock(return_value={
            "round_id": "round-5-n1",
            "status": "open",
            "accepting_submissions": True,
            "opened_epoch": 5,
        })

        # Mock submission endpoint to return success
        mock_submit_resp = AsyncMock()
        mock_submit_resp.status = 201
        mock_submit_resp.json = AsyncMock(return_value={
            "submission_id": "sub_test123",
            "status": "queued",
            "status_url": "/v1/submissions/sub_test123/status",
            "round_id": "round-5-n1",
            "epoch": 5,
        })

        captured_payload = {}

        def mock_get(url, **kwargs):
            return _FakeContextManager(mock_round_resp)

        def mock_post(url, json=None, **kwargs):
            if json:
                captured_payload.update(json)
            return _FakeContextManager(mock_submit_resp)

        mock_session = MagicMock()
        mock_session.get = mock_get
        mock_session.post = mock_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_keypair = MagicMock()
        mock_keypair.ss58_address = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        mock_keypair.sign = MagicMock(return_value=b"fakesignature")

        mock_wallet = MagicMock()
        mock_wallet.get_hotkey = MagicMock(return_value=mock_keypair)

        with patch("minotaur_subnet.miner.main.aiohttp.ClientSession", return_value=mock_session), \
             patch("bittensor_wallet.Wallet", return_value=mock_wallet):
            result = await submit_solver_git(
                pr_number=123,
                head_sha="a" * 40,
                hotkey="test-wallet",
                validator_url="http://localhost:9100",
            )

        assert result.get("submission_id") == "sub_test123"
        assert captured_payload.get("round_id") == "round-5-n1"
        assert captured_payload.get("epoch") == 5
        assert captured_payload.get("pr_number") == 123
        assert captured_payload.get("head_sha") == "a" * 40
        assert captured_payload.get("signature")  # Should be non-empty

    @pytest.mark.asyncio
    async def test_poll_submission_status_terminal(self):
        """poll_submission_status returns when status is terminal."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "submission_id": "sub_test",
            "status": "adopted",
        })

        def mock_get(url, **kwargs):
            return _FakeContextManager(mock_resp)

        mock_session = MagicMock()
        mock_session.get = mock_get
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("minotaur_subnet.miner.main.aiohttp.ClientSession", return_value=mock_session):
            result = await poll_submission_status(
                "sub_test", "http://localhost:9100", timeout=10.0,
            )

        assert result["status"] == "adopted"
