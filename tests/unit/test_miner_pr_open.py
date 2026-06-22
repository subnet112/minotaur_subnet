"""Unit tests for the miner PR-open helper (_open_or_get_pr)."""

from unittest.mock import MagicMock, patch

from minotaur_subnet.miner.agent.loop import _open_or_get_pr

UPSTREAM = "subnet112/minotaur-solver"


def _fake_urlopen(responses):
    """responses: list of (status, json_obj). Each urlopen() pops the next."""
    it = iter(responses)

    class _Resp:
        def __init__(self, status, payload):
            self.status = status
            import json
            self._body = json.dumps(payload).encode() if payload is not None else b""

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=20):
        status, payload = next(it)
        return _Resp(status, payload)

    return _open


def test_open_pr_created():
    with patch("urllib.request.urlopen", _fake_urlopen([(201, {"number": 55})])):
        assert _open_or_get_pr(UPSTREAM, "miner", "miner/x", "tok", head_sha="a" * 40) == 55


def test_open_pr_already_exists_falls_back_to_lookup():
    import urllib.error

    def _open(req, timeout=20):
        # First call (POST /pulls) raises 422; second (GET) returns the existing PR.
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, 422, "exists", {}, None)

        class _Resp:
            status = 200

            def read(self):
                return b'[{"number": 77}]'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    with patch("urllib.request.urlopen", _open):
        assert _open_or_get_pr(UPSTREAM, "miner", "miner/x", "tok") == 77


def test_open_pr_no_token_returns_none():
    assert _open_or_get_pr(UPSTREAM, "miner", "miner/x", "") is None


def test_open_pr_failure_returns_none():
    with patch("urllib.request.urlopen", _fake_urlopen([(500, None), (500, None)])):
        assert _open_or_get_pr(UPSTREAM, "miner", "miner/x", "tok") is None
