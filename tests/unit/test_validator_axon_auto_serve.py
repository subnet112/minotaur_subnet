"""Tests for the validator's auto-serve-axon-on-startup behavior.

Bittensor convention is for the validator daemon to publish its axon
URL on the subnet metagraph via ``subtensor.serve_axon()`` during
startup, so other validators' peer-discovery loops can locate it. Our
daemon previously did not do this — see the discussion in
``project_third_party_validators_active.md`` (2026-05-25). These tests
lock in the new auto-serve behavior + its gating, plus the idempotency
pre-check + rate-limit handling added on 2026-05-26 (Custom error: 12
/ ServingRateLimitExceeded was producing noisy ERROR logs on every
restart inside the chain's ~50-block rate-limit window).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import _auto_serve_axon_on_metagraph


# ── URL parsing matches what the daemon does ───────────────────────────


def _parse(url: str) -> tuple[str, int]:
    """Mirror the parsing the daemon performs on VALIDATOR_AXON_URL."""
    parsed = urlparse(url)
    return (parsed.hostname or ""), (parsed.port or 9100)


def test_parses_axon_url_with_explicit_port():
    ip, port = _parse("http://192.150.253.122:9100")
    assert ip == "192.150.253.122"
    assert port == 9100


def test_parses_axon_url_with_dns_name():
    ip, port = _parse("http://validator.example.com:9100")
    assert ip == "validator.example.com"
    assert port == 9100


def test_parses_axon_url_without_explicit_port_defaults_to_9100():
    ip, port = _parse("http://192.150.253.122")
    assert ip == "192.150.253.122"
    assert port == 9100


def test_parses_https_url():
    ip, port = _parse("https://validator.example.com:9100")
    assert ip == "validator.example.com"
    assert port == 9100


def test_unparseable_url_yields_empty_hostname():
    """The daemon's gate (``if axon_ip:``) skips the serve call entirely
    when the URL has no hostname — protects against garbled env vars."""
    ip, port = _parse("not-a-url")
    assert ip == ""


# ── source-level check that the call is wired in startup ─────────────


def test_serve_axon_call_is_wired_into_validator_startup():
    """Lock in that ``subtensor.serve_axon`` is invoked from
    ``minotaur_subnet/validator/main.py`` startup. A future refactor
    that accidentally removes it would silently regress every operator
    back to ``axon=0.0.0.0:0`` on the metagraph."""
    src = (_REPO_ROOT / "minotaur_subnet" / "validator" / "main.py").read_text()
    assert "subtensor.serve_axon" in src, (
        "validator startup must call subtensor.serve_axon() to auto-publish "
        "the axon URL on the metagraph — see Bittensor convention"
    )
    # Gate: must be conditional on VALIDATOR_AXON_URL being set so
    # operators that publish out-of-band can opt out
    assert 'os.environ.get("VALIDATOR_AXON_URL"' in src


def test_serve_axon_failure_is_non_fatal():
    """The startup wrapper must catch exceptions and continue — a failed
    auto-serve should not crash the validator daemon. The operator can
    re-serve manually or fix VALIDATOR_AXON_URL and restart."""
    src = (_REPO_ROOT / "minotaur_subnet" / "validator" / "main.py").read_text()
    # The serve_axon call lives inside a try/except block with a warning log
    assert "Auto-serve axon failed" in src, (
        "serve_axon failure must be logged + non-fatal so the daemon "
        "continues running with a degraded peer-discovery posture"
    )


# ── _auto_serve_axon_on_metagraph behaviour ──────────────────────────


_HOTKEY = "5G66U8yjZJygrr8E2JGaR3PkY7UQzMtJdq9ZU2U7UQUsn112"


def _make_subtensor(*, metagraph_hotkeys, metagraph_axons, serve_axon_result=True,
                    serve_axon_exc=None):
    """Build a mock subtensor with a metagraph + serve_axon stub."""
    metagraph = SimpleNamespace(hotkeys=metagraph_hotkeys, axons=metagraph_axons)
    subtensor = MagicMock()
    subtensor.metagraph.return_value = metagraph
    if serve_axon_exc is not None:
        subtensor.serve_axon.side_effect = serve_axon_exc
    else:
        subtensor.serve_axon.return_value = serve_axon_result
    return subtensor


def _bt_module_stub():
    """Provide a stand-in for the ``bittensor`` module — only ``.Axon`` matters."""
    bt = SimpleNamespace(Axon=MagicMock(name="Axon"))
    return bt


def test_skips_serve_when_metagraph_already_matches():
    """Idempotency: when the on-chain axon entry already matches the
    desired ip:port, the helper must NOT call serve_axon. This avoids
    the chain's per-hotkey rate limit on every restart."""
    matching_axon = SimpleNamespace(ip="192.150.253.122", port=9100)
    subtensor = _make_subtensor(
        metagraph_hotkeys=["someone-else", _HOTKEY],
        metagraph_axons=[
            SimpleNamespace(ip="10.0.0.1", port=9100),
            matching_axon,
        ],
    )
    with patch("socket.gethostbyname", return_value="192.150.253.122"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    subtensor.metagraph.assert_called_once_with(112)
    subtensor.serve_axon.assert_not_called()


def test_calls_serve_when_metagraph_entry_is_stale():
    """When the metagraph entry differs (different port), the helper
    must call serve_axon to update it."""
    stale_axon = SimpleNamespace(ip="192.150.253.122", port=8080)
    subtensor = _make_subtensor(
        metagraph_hotkeys=[_HOTKEY],
        metagraph_axons=[stale_axon],
    )
    with patch("socket.gethostbyname", return_value="192.150.253.122"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    subtensor.serve_axon.assert_called_once()


def test_calls_serve_when_hotkey_not_yet_on_metagraph():
    """First-time registration: the hotkey isn't in the metagraph yet —
    fall through to serve_axon."""
    subtensor = _make_subtensor(
        metagraph_hotkeys=["someone-else"],
        metagraph_axons=[SimpleNamespace(ip="10.0.0.1", port=9100)],
    )
    with patch("socket.gethostbyname", return_value="192.150.253.122"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    subtensor.serve_axon.assert_called_once()


def test_rate_limit_error_is_treated_as_benign(caplog):
    """When serve_axon raises with the Custom error: 12 string (Serving-
    RateLimitExceeded), the helper must NOT log a warning — the previous
    axon entry is still valid; nothing to do."""
    rate_limit_exc = Exception(
        "Subtensor returned 'SubstrateRequestException(Invalid Transaction)' "
        "error. This means: 'Custom error: 12 | Please consult ...'"
    )
    subtensor = _make_subtensor(
        metagraph_hotkeys=[_HOTKEY],
        # No match: stale port, so we fall through past the idempotency
        # check into serve_axon, which then raises.
        metagraph_axons=[SimpleNamespace(ip="192.150.253.122", port=8080)],
        serve_axon_exc=rate_limit_exc,
    )
    with patch("socket.gethostbyname", return_value="192.150.253.122"), \
         caplog.at_level("INFO", logger="minotaur_subnet.validator"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    # Must NOT have emitted the "Auto-serve axon failed" warning
    assert not any(
        r.levelname == "WARNING" and "Auto-serve axon failed" in r.message
        for r in caplog.records
    ), "Rate-limit error must be downgraded to INFO, not WARNING"
    assert any(
        "ServingRateLimitExceeded" in r.message or "rate-limited" in r.message
        for r in caplog.records
    )


def test_other_errors_still_warn(caplog):
    """Anything other than the rate-limit error must still produce the
    operator-actionable warning."""
    subtensor = _make_subtensor(
        metagraph_hotkeys=[_HOTKEY],
        metagraph_axons=[SimpleNamespace(ip="192.150.253.122", port=8080)],
        serve_axon_exc=Exception("balance too low"),
    )
    with patch("socket.gethostbyname", return_value="192.150.253.122"), \
         caplog.at_level("WARNING", logger="minotaur_subnet.validator"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    assert any(
        "Auto-serve axon failed" in r.message for r in caplog.records
    )


def test_metagraph_check_failure_falls_through(caplog):
    """If the metagraph read itself blows up (network blip), the helper
    must NOT bail — it falls through to serve_axon so the operator gets
    a fresh attempt regardless."""
    subtensor = MagicMock()
    subtensor.metagraph.side_effect = Exception("RPC unreachable")
    subtensor.serve_axon.return_value = True
    with patch("socket.gethostbyname", return_value="192.150.253.122"):
        _auto_serve_axon_on_metagraph(
            subtensor=subtensor,
            bt_module=_bt_module_stub(),
            wallet=MagicMock(),
            netuid=112,
            my_hotkey=_HOTKEY,
            axon_url="http://192.150.253.122:9100",
        )
    subtensor.serve_axon.assert_called_once()
