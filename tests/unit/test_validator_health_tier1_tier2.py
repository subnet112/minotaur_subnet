"""Tests for the Tier 1 + Tier 2 validator-health enrichments.

These cover the new helpers (``_identify_leader_uid``,
``_dns_resolve_first_ip``) and the additional finding types added to
``detect_findings``. The previous test surface was 0 (scripts/ had no
tests) so this is also the starter set for future enrichments.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.validator_health_check import (
    ValidatorStatus,
    _dns_resolve_first_ip,
    _identify_leader_uid,
    detect_findings,
)


# ── _identify_leader_uid ─────────────────────────────────────────────────


def _mock_metagraph(*, permits, stakes, axon_ips, axon_ports, hotkeys):
    """Build a minimal metagraph stub. All lists must be same length."""
    n = len(permits)
    return SimpleNamespace(
        n=SimpleNamespace(item=lambda: n),
        validator_permit=permits,
        stake=stakes,
        axons=[
            SimpleNamespace(ip=ip, port=port)
            for ip, port in zip(axon_ips, axon_ports)
        ],
        hotkeys=hotkeys,
    )


def test_leader_picks_highest_stake_with_axon_and_permit():
    mg = _mock_metagraph(
        permits=[True, True, True],
        stakes=[100.0, 500.0, 300.0],
        axon_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
        axon_ports=[9100, 9100, 9100],
        hotkeys=["5Hot1", "5Hot2", "5Hot3"],
    )
    assert _identify_leader_uid(mg) == 1  # uid 1 has highest stake


def test_leader_skips_no_permit_holders():
    """A non-permit-holder with more stake than the permitted set must NOT
    be selected — only validators with active permits compete."""
    mg = _mock_metagraph(
        permits=[False, True, True],
        stakes=[1000.0, 100.0, 50.0],  # uid 0 has most stake but no permit
        axon_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
        axon_ports=[9100, 9100, 9100],
        hotkeys=["5Hot1", "5Hot2", "5Hot3"],
    )
    assert _identify_leader_uid(mg) == 1


def test_leader_skips_validators_with_no_axon():
    """Permit-holders without a published axon can't act as leader."""
    mg = _mock_metagraph(
        permits=[True, True],
        stakes=[1000.0, 50.0],
        axon_ips=["0.0.0.0", "1.2.3.4"],  # uid 0 not serving an axon
        axon_ports=[0, 9100],
        hotkeys=["5Hot1", "5Hot2"],
    )
    assert _identify_leader_uid(mg) == 1  # uid 1 wins despite less stake


def test_leader_ties_broken_by_hotkey_ascending():
    """Equal stake → ascending hotkey wins (matches the daemon's election)."""
    mg = _mock_metagraph(
        permits=[True, True],
        stakes=[100.0, 100.0],
        axon_ips=["1.1.1.1", "2.2.2.2"],
        axon_ports=[9100, 9100],
        hotkeys=["5HotB", "5HotA"],  # uid 1's hotkey is lexicographically lower
    )
    assert _identify_leader_uid(mg) == 1


def test_leader_returns_none_when_no_candidates():
    """No permitted+axon-serving validators → None (fresh redeploy state)."""
    mg = _mock_metagraph(
        permits=[False, False],
        stakes=[100.0, 200.0],
        axon_ips=["0.0.0.0", "0.0.0.0"],
        axon_ports=[0, 0],
        hotkeys=["5HotA", "5HotB"],
    )
    assert _identify_leader_uid(mg) is None


# ── _dns_resolve_first_ip ────────────────────────────────────────────────


def test_dns_resolve_numeric_ip_passthrough():
    """Numeric IPs round-trip through gethostbyname unchanged."""
    assert _dns_resolve_first_ip("1.2.3.4") == "1.2.3.4"


def test_dns_resolve_returns_none_on_failure():
    """Resolution failure must NOT raise — fail-soft to None so one DNS
    hiccup doesn't poison the whole workflow run."""
    with patch("socket.gethostbyname", side_effect=OSError("nxdomain")):
        assert _dns_resolve_first_ip("nonexistent.example.invalid") is None


def test_dns_resolve_returns_resolved_address():
    """Happy path: hostname resolves to numeric IP."""
    with patch("socket.gethostbyname", return_value="52.19.89.149"):
        assert _dns_resolve_first_ip("my-elb.example.com") == "52.19.89.149"


# ── detect_findings — Tier 1 alerts ──────────────────────────────────────


def _base_status(**overrides) -> ValidatorStatus:
    """Build a 'healthy validator' status. Overrides flip individual fields."""
    defaults = dict(
        evm_address="0xabc",
        hotkey="5HotKey",
        uid=42,
        stake=100.0,
        trust=1.0,
        last_update_seconds_ago=120,  # 2m — fresh
        health_reachable=True,
        weights_emitter_configured=True,
        owner_hotkey_resolved=True,
        loaded_intents=1,
        last_emit={"result": "ok", "error": None},
    )
    defaults.update(overrides)
    return ValidatorStatus(**defaults)


def test_no_emitter_finding_fires_when_emitter_unconfigured():
    s = _base_status(weights_emitter_configured=False)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_emitter" in types


def test_no_owner_hotkey_finding_fires():
    s = _base_status(owner_hotkey_resolved=False)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_owner_hotkey" in types


def test_recent_emit_error_finding_fires():
    s = _base_status(last_emit={"result": "error", "error": "rate limit"})
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "recent_emit_error" in types


def test_no_loaded_intents_finding_fires():
    s = _base_status(loaded_intents=0)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_loaded_intents" in types


def test_no_findings_when_all_healthy():
    """A perfectly healthy validator generates zero alerts."""
    s = _base_status()
    findings = detect_findings([s])
    assert findings == []


def test_findings_skip_when_health_unreachable():
    """Tier 1 fields are only meaningful when /health succeeded — if the
    probe failed, we have no signal and must not generate false-positive
    alerts based on default-None fields."""
    s = _base_status(
        health_reachable=False,
        weights_emitter_configured=None,
        owner_hotkey_resolved=None,
        loaded_intents=None,
    )
    findings = detect_findings([s])
    # Only the existing stale_weights alert can fire (it's gated on
    # last_update_seconds_ago, not on health_reachable). Our case has
    # fresh weights so even that won't fire.
    assert findings == []


# ── detect_findings — Tier 2 alerts ──────────────────────────────────────


def test_phantom_leader_finding_fires():
    s = _base_status(phantom_leader=True)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "phantom_leader" in types


def test_metagraph_sync_stuck_finding_fires():
    s = _base_status(metagraph_sync_stale=True)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "metagraph_sync_stuck" in types


def test_axon_dns_drift_not_an_alert():
    """axon_dns_drift is rendered in diagnostics but intentionally NOT
    promoted to an alert — ELB rotation between serve_axon ticks causes
    benign transient drift. Surfacing as alert would spam."""
    s = _base_status(axon_dns_drift=True)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "axon_dns_drift" not in types


# ── compound case: multiple problems on one validator ────────────────────


def test_multiple_findings_on_same_validator():
    """A single validator can trigger multiple finding types simultaneously
    (eg. wallet didn't load AND owner_hotkey unresolved). All must surface."""
    s = _base_status(
        weights_emitter_configured=False,
        owner_hotkey_resolved=False,
        loaded_intents=0,
    )
    findings = detect_findings([s])
    types = {f["type"] for f in findings}
    assert "no_emitter" in types
    assert "no_owner_hotkey" in types
    assert "no_loaded_intents" in types
