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
    _fmt_block_loop,
    _fmt_image,
    _fmt_last_emit,
    _fmt_live_solver,
    _fmt_uptime,
    _identify_leader_uid,
    _render_health_detail_table,
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


# ── render helpers: image / uptime / block loop / last emit / live solver ──


def test_fmt_image_published_short_sha_renders_in_code_fence():
    """Hex SHAs (>=7 chars) render as a bare code span — the common case
    for any GHCR-published build."""
    assert _fmt_image("2f14b2e") == "`2f14b2e`"


def test_fmt_image_longer_hex_is_truncated_to_8_chars():
    """Long SHAs are truncated to 8 chars so the column stays narrow."""
    assert _fmt_image("2f14b2ec0ffee123") == "`2f14b2ec`"


def test_fmt_image_non_hex_string_gets_warning_marker():
    """Local/dev builds (anything not a hex SHA) get ⚠ so operators
    immediately see who's running an unpublished image."""
    assert _fmt_image("dev") == "`dev` ⚠"
    assert _fmt_image("local-build") == "`local-build` ⚠"


def test_fmt_image_handles_missing_field():
    """Older daemons predate ``image_sha`` and report None — render as
    em-dash rather than the literal ``None``."""
    assert _fmt_image(None) == "—"
    assert _fmt_image("") == "—"


def test_fmt_uptime_picks_largest_unit():
    """Format picks the largest unit that fits to keep the column narrow."""
    assert _fmt_uptime(45) == "45s"
    assert _fmt_uptime(125) == "2m"
    assert _fmt_uptime(3600) == "1h 0m"
    assert _fmt_uptime(7320) == "2h 2m"
    assert _fmt_uptime(90_000) == "1d 1h"


def test_fmt_uptime_handles_none():
    assert _fmt_uptime(None) == "—"


def test_fmt_block_loop_renders_role_states():
    """The three meaningful states must each render uniquely so a
    glance at the table tells operators their role."""
    follower = ValidatorStatus(evm_address="0x1", block_loop_running=False)
    assert _fmt_block_loop(follower) == "follower"

    leader = ValidatorStatus(evm_address="0x2", block_loop_running=True)
    assert _fmt_block_loop(leader) == "✅ leader"

    phantom = ValidatorStatus(
        evm_address="0x3", block_loop_running=True, phantom_leader=True,
    )
    assert _fmt_block_loop(phantom) == "⚠ phantom-leader"

    unknown = ValidatorStatus(evm_address="0x4", block_loop_running=None)
    assert _fmt_block_loop(unknown) == "—"


def test_fmt_last_emit_ok_includes_source_and_age():
    """A successful emit renders all three pieces (age · source · ✅ ok)
    so operators can correlate cadence vs the burn/queue source."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 120, "source": "burn_fallback", "result": "ok"},
        now=now,
    )
    assert out == "2m ago · burn_fallback · ✅ ok"


def test_fmt_last_emit_error_keeps_failure_marker():
    """An errored emit shows ❌ — this is the smoking-gun pattern for
    'daemon trying, something on chain rejecting'."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 30, "source": "burn_fallback", "result": "error"},
        now=now,
    )
    assert out == "30s ago · burn_fallback · ❌ error"


def test_fmt_last_emit_missing_source_falls_back():
    """Pre-PR-#95 daemons report last_emit without ``source`` — render
    as em-dash rather than the literal ``None``."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 60, "result": "ok"},
        now=now,
    )
    assert out == "1m ago · — · ✅ ok"


def test_fmt_last_emit_none():
    """No last_emit yet (fresh daemon, hasn't ticked an epoch boundary)."""
    assert _fmt_last_emit(None, now=0) == "—"


def test_fmt_live_solver_silent_when_api_unreachable():
    """No api /health response → field stays unknown, render em-dash so
    third-party operators without an exposed api don't get a false-bad."""
    s = ValidatorStatus(evm_address="0x1", live_solver_running=None)
    assert _fmt_live_solver(s) == "—"


def test_fmt_live_solver_clean_path_is_a_bare_check():
    """Solver up, no respawns → just ✅, no count noise."""
    s = ValidatorStatus(
        evm_address="0x1", live_solver_running=True, live_solver_respawn_count=0,
    )
    assert _fmt_live_solver(s) == "✅"


def test_fmt_live_solver_appends_respawn_count_when_nonzero():
    """Respawn count surfaces crash-loop signal without spawning its own column."""
    s = ValidatorStatus(
        evm_address="0x1", live_solver_running=True, live_solver_respawn_count=3,
    )
    assert _fmt_live_solver(s) == "✅ (3 respawns)"

    s_one = ValidatorStatus(
        evm_address="0x1", live_solver_running=True, live_solver_respawn_count=1,
    )
    assert _fmt_live_solver(s_one) == "✅ (1 respawn)"


# ── render: full daemon-detail table ───────────────────────────────────────


def test_render_health_detail_empty_when_no_probes_succeeded():
    """Zero reachable daemons → render the placeholder instead of an
    empty table (a bare header confuses operators into thinking probe
    succeeded but everyone's silent)."""
    unreachable = ValidatorStatus(evm_address="0x1", health_reachable=False)
    out = _render_health_detail_table([unreachable])
    assert "## Daemon /health detail" in out
    assert "No `/health` probes succeeded" in out
    assert "| Validator |" not in out  # no table header should appear


def test_render_health_detail_renders_one_row_per_reachable_validator():
    """Only validators with successful /health probes appear in the
    detail table — unreachable rows are already represented in the
    main table's 'Last set by' column as ``·``."""
    reachable = ValidatorStatus(
        evm_address="0xreachable", uid=1, display_name="Acme",
        health_reachable=True, image_sha="2f14b2e",
        uptime_seconds=3600, loaded_intents=1,
        weights_emitter_configured=True, owner_hotkey_resolved=True,
        block_loop_running=False,
        last_emit={"attempted_at": 0, "source": "burn_fallback", "result": "ok"},
    )
    unreachable = ValidatorStatus(
        evm_address="0xsilent", uid=2, health_reachable=False,
    )
    out = _render_health_detail_table([reachable, unreachable])
    assert "**Acme**" in out
    assert "0xsilent" not in out  # silent row excluded from this table
    assert "Listing **1** of 2 validator(s)" in out


def test_render_health_detail_live_solver_column_only_when_any_value():
    """The Live solver column is suppressed when every row has None — it
    would just add empty cells. It appears as soon as one row has
    live_solver_running set (the elected leader, typically)."""
    no_solver = ValidatorStatus(
        evm_address="0x1", uid=1, health_reachable=True,
        live_solver_running=None,
    )
    out = _render_health_detail_table([no_solver])
    assert "Live solver" not in out

    with_solver = ValidatorStatus(
        evm_address="0x2", uid=2, health_reachable=True,
        live_solver_running=True, live_solver_respawn_count=0,
    )
    out_with = _render_health_detail_table([no_solver, with_solver])
    assert "| Live solver |" in out_with
