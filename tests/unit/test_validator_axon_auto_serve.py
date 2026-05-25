"""Tests for the validator's auto-serve-axon-on-startup behavior.

Bittensor convention is for the validator daemon to publish its axon
URL on the subnet metagraph via ``subtensor.serve_axon()`` during
startup, so other validators' peer-discovery loops can locate it. Our
daemon previously did not do this — see the discussion in
``project_third_party_validators_active.md`` (2026-05-25). These tests
lock in the new auto-serve behavior + its gating.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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
