"""Regression tests for peer-discovery axon-URL equivalence with DNS.

Before 2026-05-26: ``peer_discovery._probe_one`` required the signed
``identity.axon_url`` to byte-equal the metagraph's ``http://<ip>:<port>``
URL. Every third-party operator running behind a CDN / load balancer
(staked.cloud, AWS ELB, …) signed identity with a hostname that didn't
match the metagraph's IP form, even though both pointed to the same
endpoint. Discovery silently rejected them with "axon mismatch" → their
sigs were never accepted into consensus → orders couldn't reach quorum.

After: equivalence is host+port with DNS resolution on the signed host.
A hostname that resolves to (one of) the metagraph IP(s) matches; a
hostname that resolves to a different IP, or doesn't resolve at all,
still rejects (preserves the MitM-pinning intent of the cross-check).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.peer_discovery import (
    _axon_urls_equivalent,
    _parse_axon_url,
)


# ── _parse_axon_url ───────────────────────────────────────────────────────


def test_parse_extracts_host_and_port():
    assert _parse_axon_url("http://1.2.3.4:9100") == ("1.2.3.4", 9100)


def test_parse_strips_trailing_slash():
    assert _parse_axon_url("http://example.com:9100/") == ("example.com", 9100)


def test_parse_lowercases_host():
    assert _parse_axon_url("http://EXAMPLE.com:9100") == ("example.com", 9100)


def test_parse_returns_none_when_port_missing():
    # No explicit port — we want explicit ports for axon comparison.
    assert _parse_axon_url("http://example.com") is None


def test_parse_returns_none_on_malformed():
    assert _parse_axon_url("not a url") is None
    assert _parse_axon_url("") is None


# ── _axon_urls_equivalent: byte-equal fast path ───────────────────────────


@pytest.mark.asyncio
async def test_equivalent_byte_equal_ip_form():
    """Both URLs identical IP form — fast path, no DNS needed."""
    assert await _axon_urls_equivalent(
        "http://1.2.3.4:9100",
        "http://1.2.3.4:9100",
    )


@pytest.mark.asyncio
async def test_equivalent_byte_equal_hostname():
    """Both URLs same hostname — fast path."""
    assert await _axon_urls_equivalent(
        "http://example.com:9100",
        "http://example.com:9100",
    )


# ── DNS resolution path — the staked.cloud scenario ───────────────────────


@pytest.mark.asyncio
async def test_hostname_resolves_to_metagraph_ip(monkeypatch):
    """The real-world case: metagraph holds the IP (because Bittensor
    stores ip+port, not hostnames), signed identity holds the public
    hostname operators set via ``VALIDATOR_AXON_URL``. Both resolve to
    the same endpoint → accept."""
    async def fake_getaddrinfo(host, port, **kw):
        # Simulate a hostname A-record pointing to 54.228.70.29
        assert host == "bittensor-sn112.staked.cloud"
        assert port == 9100
        return [(2, 1, 6, "", ("54.228.70.29", 9100))]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    assert await _axon_urls_equivalent(
        "http://54.228.70.29:9100",                  # metagraph (IP)
        "http://bittensor-sn112.staked.cloud:9100",  # signed identity (hostname)
    )


@pytest.mark.asyncio
async def test_hostname_resolves_to_different_ip_rejected(monkeypatch):
    """MitM-pinning intent preserved: a signed hostname that resolves to
    a different IP than the metagraph one is rejected."""
    async def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("9.9.9.9", 9100))]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    assert not await _axon_urls_equivalent(
        "http://1.2.3.4:9100",
        "http://attacker.example:9100",
    )


@pytest.mark.asyncio
async def test_hostname_round_robin_dns_accepts_any_match(monkeypatch):
    """When the signed hostname resolves to multiple A records (round
    robin / multi-AZ load balancer), accept if the metagraph IP is in
    the set."""
    async def fake_getaddrinfo(host, port, **kw):
        return [
            (2, 1, 6, "", ("1.1.1.1", 9100)),
            (2, 1, 6, "", ("2.2.2.2", 9100)),
            (2, 1, 6, "", ("54.228.70.29", 9100)),
        ]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    assert await _axon_urls_equivalent(
        "http://54.228.70.29:9100",       # one of the round-robin IPs
        "http://lb.example.com:9100",
    )


@pytest.mark.asyncio
async def test_hostname_resolution_failure_rejects(monkeypatch):
    """NXDOMAIN, OSError, or timeout during resolution → reject. We
    don't crash; we treat unresolvable as 'can't prove equivalence'."""
    async def fake_getaddrinfo(host, port, **kw):
        raise OSError(-2, "Name or service not known")

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    assert not await _axon_urls_equivalent(
        "http://1.2.3.4:9100",
        "http://nxdomain.invalid:9100",
    )


# ── port mismatch ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_port_mismatch_rejected_even_if_host_matches():
    """Same host, different port — different endpoint, reject."""
    assert not await _axon_urls_equivalent(
        "http://1.2.3.4:9100",
        "http://1.2.3.4:9101",
    )


@pytest.mark.asyncio
async def test_port_mismatch_rejected_even_after_dns(monkeypatch):
    """Hostname resolves to the right IP, but port differs — reject.
    Ports must match exactly; the DNS path doesn't get to override that."""
    async def fake_getaddrinfo(host, port, **kw):
        # Would have matched on host…
        return [(2, 1, 6, "", ("1.2.3.4", 9100))]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    # …but ports differ.
    assert not await _axon_urls_equivalent(
        "http://1.2.3.4:9100",
        "http://example.com:9101",
    )


# ── malformed inputs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unparseable_metagraph_url_rejects():
    assert not await _axon_urls_equivalent("garbage", "http://example.com:9100")


@pytest.mark.asyncio
async def test_unparseable_signed_url_rejects():
    assert not await _axon_urls_equivalent("http://1.2.3.4:9100", "garbage")
