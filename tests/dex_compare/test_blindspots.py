"""Tests for blindspot detection (open + covered) over the comparison history."""

from __future__ import annotations

from minotaur_subnet.dex_compare.blindspots import (
    build_blindspots_response,
    compute_chain_blindspots,
)

DAY = 86400.0
NOW = 1_000_000.0
SPLIT = NOW - 7 * DAY  # earlier < SPLIT <= recent


def _row(status, ts, *, pair=("0xAAA", "0xBBB"), out="1", chain_id=1,
         trade_source="cow_onchain", err=None, in_sym=None, out_sym=None):
    mino = {"status": status, "output_raw": out if status == "ok" else None, "error": err}
    return {
        "chain_id": chain_id, "created_at": ts, "trade_source": trade_source,
        "input_token": pair[0], "output_token": pair[1],
        "input_symbol": in_sym, "output_symbol": out_sym,
        "results": {"minotaur": mino},
    }


def _bs(rows, split=SPLIT, limit=20):
    return compute_chain_blindspots(rows, 1, split, limit)


def test_open_blindspot_currently_failing():
    rows = [_row("failed", NOW - i * 3600, err="no route / zero output") for i in range(4)]
    res = _bs(rows)
    assert res["open_count"] == 1
    o = res["open"][0]
    assert o["recent_no_route"] == 4 and o["recent_fail_rate"] == 1.0
    assert o["last_error"] == "no route / zero output"
    assert res["covered_count"] == 0


def test_covered_failed_earlier_ok_recent():
    rows = (
        [_row("failed", SPLIT - i * DAY) for i in range(1, 4)]      # earlier: 3 no-route
        + [_row("ok", NOW - i * 3600, out="500") for i in range(4)]  # recent: 4 ok
    )
    res = _bs(rows)
    assert res["covered_count"] == 1
    c = res["covered"][0]
    assert c["earlier_no_route"] == 3 and c["earlier_fail_rate"] == 1.0
    assert c["recent_ok"] == 4 and c["recent_ok_rate"] == 1.0
    assert c["covered_at"] is not None and c["covered_at"] >= SPLIT
    # No longer failing recently ⇒ not also listed as open.
    assert res["open_count"] == 0


def test_ok_with_zero_output_counts_as_no_route():
    rows = [_row("ok", NOW - i * 3600, out="0") for i in range(3)]  # status ok but no output
    res = _bs(rows)
    assert res["open_count"] == 1
    assert res["open"][0]["recent_no_route"] == 3


def test_transient_errors_ignored():
    # 'error'/'warming_up' are transient and must not count as no-route.
    rows = [_row("error", NOW - i * 3600) for i in range(5)]
    res = _bs(rows)
    assert res["open_count"] == 0 and res["covered_count"] == 0


def test_min_samples_gate():
    # A single recent failure is below MIN_SAMPLES ⇒ not classified as open.
    res = _bs([_row("failed", NOW - 3600)])
    assert res["open_count"] == 0


def test_non_cow_onchain_ignored():
    rows = [_row("failed", NOW - i * 3600, trade_source="historical") for i in range(4)]
    res = _bs(rows)
    assert res["open_count"] == 0


def test_flapping_not_covered():
    # Earlier blindspot, but recent is only 50% ok (< OK_THRESHOLD) ⇒ not covered.
    rows = (
        [_row("failed", SPLIT - i * DAY) for i in range(1, 4)]
        + [_row("ok", NOW - 1 * 3600, out="9"), _row("failed", NOW - 2 * 3600)]
    )
    res = _bs(rows)
    assert res["covered_count"] == 0


def test_symbols_surface_when_known():
    rows = [_row("failed", NOW - i * 3600, out_sym="USDC") for i in range(3)]
    res = _bs(rows)
    assert res["open"][0]["output_symbol"] == "USDC"
    assert res["open"][0]["input_symbol"] is None  # unresolved stays null


def test_response_groups_by_chain_and_shapes():
    rows = (
        [_row("failed", NOW - i * 3600, chain_id=1) for i in range(3)]
        + [_row("failed", NOW - i * 3600, chain_id=8453, pair=("0xC", "0xD")) for i in range(3)]
    )
    resp = build_blindspots_response(rows, window_days=14, recent_days=7, limit=20, now=NOW)
    assert resp["source"] == "cow_onchain"
    assert resp["window_days"] == 14 and resp["recent_days"] == 7
    assert [c["chain_id"] for c in resp["chains"]] == [1, 8453]
    assert all(c["open_count"] == 1 for c in resp["chains"])
