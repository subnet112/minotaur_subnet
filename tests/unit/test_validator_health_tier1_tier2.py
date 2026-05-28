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

import time

from scripts.validator_health_check import (
    STALE_THRESHOLD_SECONDS,
    ValidatorStatus,
    _classify_weight_source,
    _dns_resolve_first_ip,
    _fmt_block_loop,
    _fmt_champion_consensus,
    _fmt_image,
    _fmt_last_emit,
    _fmt_live_solver,
    _fmt_orderbook,
    _fmt_running,
    _fmt_solver_round,
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


def test_transient_emit_error_does_not_alert():
    """A single errored *attempt* with a recent *success* must NOT alert —
    that's a routine rate-limited retry between epochs, not a problem."""
    now = time.time()
    s = _base_status(
        last_emit={"result": "error", "error": "rate limit"},
        last_successful_emit={"attempted_at": now - 120, "result": "ok"},
    )
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_successful_emit" not in types


def test_no_successful_emit_finding_fires_on_sustained_gap():
    """A validator that WAS succeeding but hasn't landed a weight-set in over
    the staleness window is the actionable case — its reputation is at risk."""
    now = time.time()
    s = _base_status(
        last_emit={"result": "error", "error": "rate limit"},
        last_successful_emit={"attempted_at": now - (STALE_THRESHOLD_SECONDS + 600), "result": "ok"},
    )
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_successful_emit" in types


def test_no_successful_emit_silent_for_legacy_daemon():
    """A daemon that never reported last_successful_emit (legacy image, field
    absent → None on the status) must NOT false-fire the sustained-gap alert."""
    s = _base_status(last_successful_emit=None)
    findings = detect_findings([s])
    types = [f["type"] for f in findings]
    assert "no_successful_emit" not in types


# ── _classify_weight_source (last_successful_emit primary) ──────────────────


def _health(**kw):
    base = {"last_emit": {"result": "ok"}, "weights_emitter_configured": True}
    base.update(kw)
    return base


def test_classify_self_on_recent_success():
    now = time.time()
    s = _base_status(last_update_seconds_ago=120)
    h = _health(last_successful_emit={"attempted_at": now - 60, "result": "ok"})
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "self"


def test_classify_self_ignores_recent_failed_attempt():
    """The whole fix: a failed latest attempt does NOT demote a validator
    that succeeded within the window."""
    now = time.time()
    s = _base_status(last_update_seconds_ago=900)
    h = _health(
        last_emit={"attempted_at": now - 5, "result": "error"},
        last_successful_emit={"attempted_at": now - 300, "result": "ok"},
    )
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "self"


def test_classify_external_when_no_recent_success_but_chain_fresh():
    now = time.time()
    s = _base_status(last_update_seconds_ago=600)  # chain fresh
    h = _health(last_successful_emit={"attempted_at": now - 7200, "result": "ok"})
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "external"


def test_classify_stale_when_no_recent_success_and_chain_stale():
    now = time.time()
    s = _base_status(last_update_seconds_ago=7200)  # chain stale
    h = _health(last_successful_emit=None)  # field present, no success
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "stale"


def test_classify_no_emitter_short_circuits():
    now = time.time()
    s = _base_status()
    h = _health(weights_emitter_configured=False, last_successful_emit=None)
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "no-emitter"


def test_classify_legacy_fallback_uses_last_emit_alignment():
    """Daemons predating last_successful_emit (no key) keep the old
    timestamp-alignment heuristic so they don't collapse to unknown."""
    now = time.time()
    s = _base_status(last_update_seconds_ago=60)
    h = {"last_emit": {"attempted_at": now - 60, "result": "ok"}, "weights_emitter_configured": True}
    assert "last_successful_emit" not in h
    assert _classify_weight_source(s, h, now=now, stale_threshold_seconds=3600) == "self"


def test_classify_unknown_when_health_none():
    assert _classify_weight_source(_base_status(), None, now=time.time(), stale_threshold_seconds=3600) == "unknown"


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


def test_fmt_last_emit_ok_abbreviates_source_and_drops_result_word():
    """A successful emit renders the compact triple — age · abbreviated
    source · glyph — so it fits as one cell in the transposed column.
    ``burn_fallback`` collapses to ``burn`` and the ✅ carries the result."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 120, "source": "burn_fallback", "result": "ok"},
        now=now,
    )
    assert out == "2m ago · burn · ✅"


def test_fmt_last_emit_queued_source_abbreviates_to_api():
    """The api-queued path abbreviates ``queued_from_api`` → ``api``."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 30, "source": "queued_from_api", "result": "ok"},
        now=now,
    )
    assert out == "30s ago · api · ✅"


def test_fmt_last_emit_error_keeps_failure_marker():
    """An errored emit shows ❌ — this is the smoking-gun pattern for
    'daemon trying, something on chain rejecting'."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 30, "source": "burn_fallback", "result": "error"},
        now=now,
    )
    assert out == "30s ago · burn · ❌"


def test_fmt_last_emit_missing_source_falls_back():
    """Pre-PR-#95 daemons report last_emit without ``source`` — render
    as em-dash rather than the literal ``None``."""
    now = 1_000_000.0
    out = _fmt_last_emit(
        {"attempted_at": now - 60, "result": "ok"},
        now=now,
    )
    assert out == "1m ago · — · ✅"


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


# ── _fmt_orderbook ───────────────────────────────────────────────────────


def test_fmt_orderbook_none_is_em_dash():
    """Field absent (older image / unreachable) → em-dash, not ``0``."""
    assert _fmt_orderbook(None) == "—"


def test_fmt_orderbook_empty_dict_is_zero():
    """Daemon up but holding no orders renders ``0`` — the normal follower
    state, distinct from the ``—`` of a daemon that can't report at all."""
    assert _fmt_orderbook({}) == "0"


def test_fmt_orderbook_joins_status_counts_sorted():
    """Non-empty book joins ``status:count`` pairs in sorted key order so
    the cell is stable run-to-run."""
    assert _fmt_orderbook({"open": 12, "executed": 3}) == "executed:3 open:12"


# ── _fmt_running ───────────────────────────────────────────────────────────


def test_fmt_running_maps_states_to_glyphs():
    assert _fmt_running(None) == "—"
    assert _fmt_running("running") == "✅ running"
    assert _fmt_running("disabled") == "❌ disabled"
    assert _fmt_running("weird") == "weird"


# ── _fmt_solver_round ────────────────────────────────────────────────────────


def test_fmt_solver_round_none_is_em_dash():
    assert _fmt_solver_round(None) == "—"
    assert _fmt_solver_round({}) == "—"


def test_fmt_solver_round_with_id_and_accepting():
    out = _fmt_solver_round(
        {"round_id": 42, "status": "open", "accepting_submissions": True}
    )
    assert out == "#42 open · accepting"


def test_fmt_solver_round_closed_not_accepting():
    out = _fmt_solver_round(
        {"round_id": 42, "status": "closed", "accepting_submissions": False}
    )
    assert out == "#42 closed"


# ── _fmt_champion_consensus ──────────────────────────────────────────────────


def test_fmt_champion_consensus_none_and_disabled():
    assert _fmt_champion_consensus(None) == "—"
    assert _fmt_champion_consensus({"enabled": False}) == "off"


def test_fmt_champion_consensus_quorum_and_peers():
    out = _fmt_champion_consensus(
        {"enabled": True, "quorum_required": 2, "validator_count": 3, "peer_count": 2}
    )
    assert out == "2-of-3, 2 peers"


def test_fmt_champion_consensus_singular_peer():
    out = _fmt_champion_consensus(
        {"enabled": True, "quorum_required": 2, "validator_count": 3, "peer_count": 1}
    )
    assert out == "2-of-3, 1 peer"


# ── render: transposed daemon-detail table ──────────────────────────────────


def test_render_health_detail_empty_when_no_probes_succeeded():
    """Zero reachable daemons → render the placeholder instead of an
    empty table (a bare header confuses operators into thinking probe
    succeeded but everyone's silent)."""
    unreachable = ValidatorStatus(evm_address="0x1", health_reachable=False)
    out = _render_health_detail_table([unreachable])
    assert "## Daemon /health detail" in out
    assert "No `/health` probes succeeded" in out
    assert "| Field |" not in out  # no table header should appear


def test_render_health_detail_one_column_per_reachable_validator():
    """Transposed: each reachable validator is a COLUMN (name over uid),
    fields run down the side. Unreachable rows are excluded — they're
    already represented in the main table's 'Last set by' column."""
    reachable = ValidatorStatus(
        evm_address="0xreachable", uid=1, display_name="Acme",
        health_reachable=True, image_sha="2f14b2e",
        uptime_seconds=3600, loaded_intents=1,
        weights_emitter_configured=True, owner_hotkey_resolved=True,
        block_loop_running=False,
        last_emit={"attempted_at": 0, "source": "burn_fallback", "result": "ok"},
        orderbook_stats={"open": 4},
    )
    unreachable = ValidatorStatus(
        evm_address="0xsilent", uid=2, health_reachable=False,
    )
    out = _render_health_detail_table([reachable, unreachable])
    # Column header carries the validator; field labels run down the side.
    assert "**Acme**<br>uid 1" in out
    assert "| Image |" in out
    assert "| Last success |" in out
    assert "| Last attempt |" in out
    assert "| OrderBook |" in out
    assert "`2f14b2e`" in out
    assert "open:4" in out
    assert "0xsilent" not in out  # silent validator excluded
    assert "**1** of 2" in out


def test_render_health_detail_orders_columns_by_uid():
    """Columns are uid-ascending for run-to-run stability (and to match
    the Probe-diagnostics table ordering)."""
    hi = ValidatorStatus(
        evm_address="0xhi", uid=230, display_name="Yuma", health_reachable=True,
    )
    lo = ValidatorStatus(
        evm_address="0xlo", uid=1, display_name="General", health_reachable=True,
    )
    out = _render_health_detail_table([hi, lo])
    # The uid=1 header must appear before the uid=230 header in the row.
    assert out.index("uid 1") < out.index("uid 230")


def test_render_health_detail_api_subtable_only_when_api_health_present():
    """The API-process sub-table is suppressed when no validator's port-8080
    /health answered — third-party validator-only stacks shouldn't show a
    dead section. It appears (with the leader's columns) as soon as one
    validator carries an ``api_health`` payload."""
    daemon_only = ValidatorStatus(
        evm_address="0x1", uid=1, health_reachable=True, api_health=None,
    )
    out = _render_health_detail_table([daemon_only])
    assert "### API process" not in out
    assert "| Live solver |" not in out

    leader = ValidatorStatus(
        evm_address="0x2", uid=2, health_reachable=True,
        live_solver_running=True, live_solver_respawn_count=0,
        api_health={
            "live_solver_running": True,
            "benchmark_worker": "running",
            "solver_round_coordinator": "running",
            "solver_round_role": "leader",
            "solver_round": {"round_id": 7, "status": "open", "accepting_submissions": True},
            "champion_consensus": {
                "enabled": True, "quorum_required": 2,
                "validator_count": 3, "peer_count": 2,
            },
        },
    )
    out_with = _render_health_detail_table([daemon_only, leader])
    assert "### API process" in out_with
    assert "| Live solver |" in out_with
    assert "| Benchmark worker |" in out_with
    assert "| Champion consensus |" in out_with
    assert "✅ running" in out_with
    assert "#7 open · accepting" in out_with
    assert "2-of-3, 2 peers" in out_with
