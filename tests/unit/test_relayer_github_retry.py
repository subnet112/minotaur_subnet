"""Retry behaviour for ``_github_api_request`` (relayer champion-publish path).

A transient GitHub 5xx/429/network error must be retried with backoff so a
certified dethrone is not dropped — incident 2026-07-20, round-e29741775
aborted ``merge_failed:publish_failed`` on a one-off 503 while creating the
canonical blob. A 4xx is a deterministic client error and is NOT retried.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.relayer.solver_repo import _github_api_request


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/x", code=code, msg="err", hdrs=None, fp=None
    )


def _ok(status: int, body: str) -> MagicMock:
    cm, inner = MagicMock(), MagicMock()
    inner.status = status
    inner.read.return_value = body.encode()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm


def test_retries_transient_503_then_succeeds():
    seq = [_http_error(503), _http_error(503), _ok(200, '{"sha": "abc"}')]
    with patch("urllib.request.urlopen", side_effect=seq) as uo, patch("time.sleep") as slp:
        status, body = _github_api_request("POST", "https://api.github.com/x")
    assert (status, body) == (200, {"sha": "abc"})
    assert uo.call_count == 3
    assert slp.call_count == 2  # backed off before each retry


def test_client_error_404_not_retried():
    with patch("urllib.request.urlopen", side_effect=_http_error(404)) as uo, patch("time.sleep") as slp:
        status, body = _github_api_request("GET", "https://api.github.com/x")
    assert (status, body) == (404, None)
    assert uo.call_count == 1
    assert slp.call_count == 0


def test_persistent_503_exhausts_retries():
    with patch("urllib.request.urlopen", side_effect=_http_error(503)) as uo, patch("time.sleep"):
        status, body = _github_api_request("GET", "https://api.github.com/x")
    assert (status, body) == (503, None)
    assert uo.call_count == 4  # 1 initial + 3 retries


def test_network_error_retried_then_succeeds():
    seq = [urllib.error.URLError("connection reset"), _ok(200, '{"k": 1}')]
    with patch("urllib.request.urlopen", side_effect=seq) as uo, patch("time.sleep"):
        status, body = _github_api_request("GET", "https://api.github.com/x")
    assert (status, body) == (200, {"k": 1})
    assert uo.call_count == 2


def test_success_first_try_no_sleep():
    with patch("urllib.request.urlopen", side_effect=[_ok(201, '{"ok": true}')]) as uo, patch("time.sleep") as slp:
        status, body = _github_api_request("POST", "https://api.github.com/x", {"a": 1})
    assert (status, body) == (201, {"ok": True})
    assert uo.call_count == 1
    assert slp.call_count == 0
