"""Tests for the stats aggregation (win/loss, relative output, gas math)."""

from __future__ import annotations

from minotaur_subnet.dex_compare.stats import build_stats_response, compute_chain_stats


def _r(status="ok", out=None, gas=None, net=False, after_fee=None,
       gas_native=None, gas_usd=None, out_usd=None, in_usd=None, fee=None):
    return {
        "status": status,
        "output_raw": None if out is None else str(out),
        "output_after_fee_raw": None if after_fee is None else str(after_fee),
        "gas_units": gas,
        "gas_native_wei": None if gas_native is None else str(gas_native),
        "gas_usd": gas_usd,
        "output_usd": out_usd,
        "input_usd": in_usd,
        "fee_raw": None if fee is None else str(fee),   # minotaur platform fee (ETH wei)
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
    native_usd=None,
    notional_usd=None,
    created_at=1000.0,
):
    return {
        "chain_id": chain_id,
        "input_amount": input_amount,
        "input_is_native": input_is_native,
        "output_is_native": output_is_native,
        "gas_price_wei": gas_price_wei,
        "native_usd": native_usd,
        "notional_usd": notional_usd,
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


def test_net_output_native_subtracts_minotaur_fee_and_gas():
    # output IS wrapped native -> native amounts convert 1:1 to output units.
    # mino_net = 1_000_000 - fee(50_000) - gas(100*1000=100_000) = 850_000
    # cow net (gasless) = 800_000  -> Minotaur WINS
    rows = [_row(
        {
            "minotaur": _r(out=1_000_000, gas=100, fee=50_000),
            "cow": _r(out=800_000, net=True),
        },
        output_is_native=True,
        gas_price_wei="1000",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_cow = cs["net"]["vs_source"]["cow"]
    assert net_cow["comparable"] == 1 and net_cow["minotaur_wins"] == 1


def test_net_cow_not_double_charged():
    # CoW's after-fee output is already net; its (bogus) gas must NOT be subtracted.
    # mino_net = 1_000_000 - 100*1000 = 900_000 ; cow keeps 950_000 -> Minotaur LOSES.
    rows = [_row(
        {
            "minotaur": _r(out=1_000_000, gas=100, fee=0),
            "cow": _r(out=950_000, net=True, gas_native=999_999_999_999),
        },
        output_is_native=True,
        gas_price_wei="1000",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_cow = cs["net"]["vs_source"]["cow"]
    assert net_cow["comparable"] == 1 and net_cow["minotaur_losses"] == 1


def test_net_uses_after_fee_output_for_aggregator():
    # A source's own protocol fee (destAmountAfterFee < destAmount) must be used.
    # mino_net = 100 (no fee/gas, output native) ; velora after-fee = 90 -> mino WINS
    rows = [_row(
        {
            "minotaur": _r(out=100),
            "velora": _r(out=100, after_fee=90),
        },
        output_is_native=True,
    )]
    cs = compute_chain_stats(rows, 8453)
    assert cs["net"]["vs_source"]["velora"]["minotaur_wins"] == 1
    # raw (gross) sees a tie, not a win
    assert cs["raw"]["vs_source"]["velora"]["ties"] == 1


def test_net_excludes_row_when_minotaur_fee_unconvertible():
    # neither token native, no Velora USD, no native_usd -> can't convert the
    # platform fee -> the row is excluded from NET (never drop our own fee).
    rows = [_row(
        {"minotaur": _r(out=100, fee=5), "1inch": _r(out=90)},
        input_is_native=False, output_is_native=False, native_usd=None,
    )]
    cs = compute_chain_stats(rows, 8453)
    assert cs["net"]["vs_source"]["1inch"]["comparable"] == 0
    assert cs["raw"]["vs_source"]["1inch"]["comparable"] == 1   # raw still counts it


def test_net_converts_fee_via_velora_usd_for_neither_native():
    # neither native, but native_usd + Velora USD let us convert the fee.
    # usd_per_out = velora.output_usd/output_raw = 2.0/1000 = 0.002 USD/unit
    # fee 1e15 wei * (2000/1e18) / 0.002 = 1000 output units
    # mino_net = 100_000 - 1000 = 99_000 ; 1inch = 100_000 -> Minotaur LOSES
    rows = [_row(
        {
            "minotaur": _r(out=100_000, fee=10 ** 15),
            "velora": _r(out=1000, after_fee=1000, out_usd=2.0),
            "1inch": _r(out=100_000),
        },
        input_is_native=False, output_is_native=False, native_usd=2000.0,
    )]
    cs = compute_chain_stats(rows, 8453)
    assert cs["net"]["vs_source"]["1inch"]["minotaur_losses"] == 1


def test_net_gas_symmetric_when_gas_price_missing():
    # gas_price=None -> Minotaur/1inch gas uncomputable. 0x carries native gas
    # independently, but symmetry must drop gas for BOTH (not just Minotaur).
    # If asymmetric, 0x_net = 800-500 = 300 and ratio = 1000/300 = 3.33 (bug).
    # Symmetric: gas dropped for all -> 0x_net = 800, ratio = 1000/800 = 1.25.
    rows = [_row(
        {"minotaur": _r(out=1000, gas=100, fee=0), "0x": _r(out=800, gas_native=500)},
        output_is_native=True, gas_price_wei=None,
    )]
    cs = compute_chain_stats(rows, 8453)
    net_0x = cs["net"]["vs_source"]["0x"]
    assert net_0x["comparable"] == 1
    assert abs(net_0x["median_relative_output"] - 1000 / 800) < 1e-6


def test_net_gas_symmetric_when_native_price_missing():
    # neither token native + native_usd=None -> Minotaur/1inch/0x gas uncomputable,
    # but Velora's gas is on its own USD path. Symmetry must drop Velora's gas too.
    # If asymmetric, velora_net = 800-80 = 720, ratio = 1000/720 = 1.389 (bug).
    # Symmetric: velora_net = 800, ratio = 1000/800 = 1.25.
    rows = [_row(
        {
            "minotaur": _r(out=1000, gas=100, fee=0),
            "velora": _r(out=800, after_fee=800, out_usd=1600.0, gas_usd=160.0),
        },
        input_is_native=False, output_is_native=False, native_usd=None, gas_price_wei="1000",
    )]
    cs = compute_chain_stats(rows, 8453)
    net_vel = cs["net"]["vs_source"]["velora"]
    assert net_vel["comparable"] == 1
    assert abs(net_vel["median_relative_output"] - 1000 / 800) < 1e-6


def test_build_response_groups_by_chain():
    rows = [
        _row({"minotaur": _r(out=100), "cow": _r(out=90)}, chain_id=8453),
        _row({"minotaur": _r(out=100), "cow": _r(out=90)}, chain_id=1),
    ]
    resp = build_stats_response(rows, 30)
    assert resp["total_comparisons"] == 2
    assert {c["chain_id"] for c in resp["chains"]} == {1, 8453}
    assert resp["sources"][0] == "minotaur"
    assert "caveats" not in resp


def test_cost_breakdown_fee_and_gas_in_usd_and_pct():
    # native_usd=2000; fee 1e15 wei -> $2 ; gas 100000*1e9 wei -> $0.2 ; trade $1000
    rows = [_row(
        {"minotaur": _r(out=100, fee=10 ** 15, gas=100000)},
        native_usd=2000.0, notional_usd=1000.0, gas_price_wei="1000000000",
    )]
    cs = compute_chain_stats(rows, 8453)
    cb = cs["cost_breakdown"]["all"]
    assert abs(cb["platform_fee_usd_median"] - 2.0) < 1e-6
    assert abs(cb["platform_fee_pct_of_trade_median"] - 0.2) < 1e-6      # $2 / $1000
    assert abs(cb["gas_usd_median"] - 0.2) < 1e-6
    assert abs(cb["gas_pct_of_trade_median"] - 0.02) < 1e-6             # $0.2 / $1000


def test_net_by_size_segments_realistic_vs_dust():
    # realistic ($5000 notional): Minotaur wins vs cow. dust ($1 via velora srcUSD):
    # Minotaur loses. The split must sort each row into the right bucket.
    realistic = _row(
        {"minotaur": _r(out=100), "cow": _r(out=90, net=True)},
        output_is_native=True, notional_usd=5000.0,
    )
    dust = _row(
        {"minotaur": _r(out=100), "cow": _r(out=110, net=True),
         "velora": _r(out=105, after_fee=105, in_usd=1.0)},
        output_is_native=True,
    )
    cs = compute_chain_stats([realistic, dust], 8453)
    nbs = cs["net_by_size"]
    assert nbs["realistic"]["comparisons"] == 1 and nbs["small"]["comparisons"] == 1
    assert nbs["realistic"]["vs_source"]["cow"]["minotaur_wins"] == 1
    assert nbs["small"]["vs_source"]["cow"]["minotaur_losses"] == 1


def test_net_notional_conversion_without_velora():
    # neither-native normalized row, Velora ABSENT/failed, but notional_usd +
    # native_usd + the output amounts let us convert the fee -> row is INCLUDED
    # in net (previously excluded -> this was why Base net was empty).
    rows = [_row(
        {"minotaur": _r(out=100000, fee=10 ** 15), "1inch": _r(out=100000)},
        input_is_native=False, output_is_native=False,
        native_usd=2000.0, notional_usd=5000.0,
    )]
    cs = compute_chain_stats(rows, 8453)
    assert cs["net"]["vs_source"]["1inch"]["comparable"] == 1


def test_empty_is_valid():
    resp = build_stats_response([], 30)
    assert resp["chains"] == [] and resp["total_comparisons"] == 0
