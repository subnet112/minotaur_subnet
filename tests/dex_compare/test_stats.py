"""Tests for the stats aggregation (win/loss, relative output, gas math)."""

from __future__ import annotations

from minotaur_subnet.dex_compare.stats import build_stats_response, compute_chain_stats


def _r(status="ok", out=None, gas=None, net=False):
    return {
        "status": status,
        "output_raw": None if out is None else str(out),
        "gas_units": gas,
        "is_net_of_gas": net,
    }


def _row(
    results,
    *,
    chain_id=8453,
    input_amount="1000000000",
    input_is_native=False,
    output_is_native=True,
    gas_price_wei="1000000000",
    created_at=1000.0,
):
    return {
        "chain_id": chain_id,
        "input_amount": input_amount,
        "input_is_native": input_is_native,
        "output_is_native": output_is_native,
        "gas_price_wei": gas_price_wei,
        "created_at": created_at,
        "results": results,
    }


def test_raw_win_and_relative():
    rows = [_row({"minotaur": _r(out=100), "cow": _r(out=90)})]
    cs = compute_chain_stats(rows, 8453)
    cow = cs["raw"]["vs_source"]["cow"]
    assert cow["comparable"] == 1
    assert cow["minotaur_wins"] == 1 and cow["minotaur_losses"] == 0
    assert cow["win_rate"] == 1.0
    assert abs(cow["median_relative_output"] - (100 / 90)) < 1e-6


def test_raw_best_aggregator_picks_max():
    rows = [_row({"minotaur": _r(out=100), "cow": _r(out=90), "velora": _r(out=110)})]
    cs = compute_chain_stats(rows, 8453)
    best = cs["raw"]["vs_best_aggregator"]
    assert best["comparable"] == 1 and best["minotaur_losses"] == 1  # 100 < best(110)
    assert cs["raw"]["vs_source"]["cow"]["minotaur_wins"] == 1        # 100 > 90
    assert cs["raw"]["vs_source"]["velora"]["minotaur_losses"] == 1   # 100 < 110


def test_coverage_counts_unsupported_and_error():
    rows = [_row({
        "minotaur": _r(out=100),
        "cow": _r(status="unsupported"),
        "velora": _r(status="error"),
    })]
    cs = compute_chain_stats(rows, 8453)
    assert cs["raw"]["vs_source"]["cow"]["source_unsupported"] == 1
    assert cs["raw"]["vs_source"]["cow"]["comparable"] == 0
    assert cs["raw"]["vs_source"]["velora"]["source_error"] == 1


def test_minotaur_failure_excluded_but_counted():
    rows = [_row({"minotaur": _r(status="failed"), "cow": _r(out=90)})]
    cs = compute_chain_stats(rows, 8453)
    assert cs["minotaur_fail"] == 1 and cs["minotaur_ok"] == 0
    assert cs["raw"]["vs_source"]["cow"]["comparable"] == 0
    assert cs["raw"]["vs_source"]["cow"]["source_ok"] == 1  # coverage still tallied


def test_net_of_gas_output_native_subtracts_minotaur_gas():
    # output token IS wrapped native -> gas converts 1:1 to output units.
    # mino_net = 1_000_000 - 100*1000 = 900_000 ; cow net (gasless) = 800_000
    rows = [_row(
        {
            "minotaur": _r(out=1_000_000, gas=100, net=False),
            "cow": _r(out=800_000, net=True),
        },
        output_is_native=True,
        gas_price_wei="1000",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_cow = cs["net_of_gas"]["vs_source"]["cow"]
    assert cs["net_of_gas"]["gas_adjustable_comparisons"] == 1
    assert net_cow["comparable"] == 1 and net_cow["minotaur_wins"] == 1


def test_net_of_gas_cow_not_double_charged():
    # CoW is net-of-gas: its huge gas_units must NOT be subtracted. If it were,
    # cow_net would clamp to 0 and Minotaur would win. Correct behaviour: cow
    # keeps 950_000 and Minotaur (900_000) LOSES.
    rows = [_row(
        {
            "minotaur": _r(out=1_000_000, gas=100, net=False),
            "cow": _r(out=950_000, gas=999_999, net=True),
        },
        output_is_native=True,
        gas_price_wei="1000",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_cow = cs["net_of_gas"]["vs_source"]["cow"]
    assert net_cow["comparable"] == 1 and net_cow["minotaur_losses"] == 1


def test_net_of_gas_neither_native_excluded():
    rows = [_row(
        {"minotaur": _r(out=100, gas=10), "cow": _r(out=90, gas=10)},
        input_is_native=False,
        output_is_native=False,
    )]
    cs = compute_chain_stats(rows, 8453)
    assert cs["net_of_gas"]["gas_adjustable_comparisons"] == 0
    assert cs["net_of_gas"]["vs_source"]["cow"]["comparable"] == 0
    # raw is unaffected
    assert cs["raw"]["vs_source"]["cow"]["comparable"] == 1


def test_net_of_gas_input_native_uses_shared_reference():
    # input token is native; ref = median(ok outputs)/input_amount.
    # outputs: mino 2000, velora 2000 -> median 2000 ; input_amount 1_000_000
    # ref = 0.002 ; velora gas_out = 100*10*0.002 = 2 -> net 1998
    #               mino  gas_out = 200*10*0.002 = 4 -> net 1996  -> mino LOSES
    rows = [_row(
        {
            "minotaur": _r(out=2000, gas=200, net=False),
            "velora": _r(out=2000, gas=100, net=False),
        },
        input_amount="1000000",
        input_is_native=True,
        output_is_native=False,
        gas_price_wei="10",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_velora = cs["net_of_gas"]["vs_source"]["velora"]
    assert net_velora["comparable"] == 1 and net_velora["minotaur_losses"] == 1


def test_build_response_groups_by_chain():
    rows = [
        _row({"minotaur": _r(out=100), "cow": _r(out=90)}, chain_id=8453),
        _row({"minotaur": _r(out=100), "cow": _r(out=90)}, chain_id=1),
    ]
    resp = build_stats_response(rows, 30)
    assert resp["total_comparisons"] == 2
    assert {c["chain_id"] for c in resp["chains"]} == {1, 8453}
    assert resp["sources"][0] == "minotaur"
    assert isinstance(resp["caveats"], list) and resp["caveats"]


def test_empty_is_valid():
    resp = build_stats_response([], 30)
    assert resp["chains"] == [] and resp["total_comparisons"] == 0
