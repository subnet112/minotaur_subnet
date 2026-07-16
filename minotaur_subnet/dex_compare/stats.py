"""Aggregate a set of comparison rows into per-chain leaderboards.

Pure functions over the row dicts returned by ``DexCompareStore.fetch_since``.
All magnitude math is done in Python ``int`` (amounts are decimal strings).

Two leaderboards per chain:

* **raw** — each source's reported output as-is. CoW is slightly understated
  here because its number is already net-of-gas.
* **net_of_gas** — each source's gross output minus ``gas_units * gas_price``
  converted into output-token base units. CoW (``is_net_of_gas``) is not charged
  gas a second time. This is the apples-to-apples view. A row is only included
  when the native<->output rate is derivable (output or input is the wrapped
  native) and a gas-price snapshot exists.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Any

from minotaur_subnet.blockchain.chains import get_chain_name

from .models import AGGREGATOR_SOURCES, SOURCES

_NET_NOTE = (
    "Gas-adjusted output in output-token base units; only rows where a native"
    "<->output rate is derivable are included; CoW is already gas-net."
)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _new_acc() -> dict[str, Any]:
    return {"comparable": 0, "wins": 0, "ties": 0, "losses": 0, "relatives": []}


def _accumulate(acc: dict[str, Any], mino: int, other: int) -> None:
    acc["comparable"] += 1
    if mino > other:
        acc["wins"] += 1
    elif mino == other:
        acc["ties"] += 1
    else:
        acc["losses"] += 1
    acc["relatives"].append(mino / other)


def _finalize(acc: dict[str, Any]) -> dict[str, Any]:
    comparable = acc["comparable"]
    rels = acc["relatives"]
    return {
        "comparable": comparable,
        "minotaur_wins": acc["wins"],
        "ties": acc["ties"],
        "minotaur_losses": acc["losses"],
        "win_rate": round(acc["wins"] / comparable, 4) if comparable else None,
        "median_relative_output": round(statistics.median(rels), 6) if rels else None,
        "mean_relative_output": round(statistics.fmean(rels), 6) if rels else None,
    }


def _net_output(result: dict[str, Any], gas_price: int, mode: str, ref_rate: float | None) -> int | None:
    """Gas-adjusted output for one source on one row, in output base units."""
    if result.get("status") != "ok":
        return None
    out = _int(result.get("output_raw"))
    if out is None or out <= 0:
        return None
    if result.get("is_net_of_gas"):  # CoW — already net, don't double-charge
        return out
    gas_units = _int(result.get("gas_units"))
    if gas_units is None:
        return None
    gas_native = gas_units * gas_price
    if mode == "output_native":
        gas_out = gas_native
    else:  # input_native — convert native wei -> output units via shared rate
        if ref_rate is None:
            return None
        gas_out = int(gas_native * ref_rate)
    return max(0, out - gas_out)


def compute_chain_stats(rows: list[dict[str, Any]], chain_id: int) -> dict[str, Any]:
    """Aggregate ``rows`` (already for one chain) into a stats block."""
    raw_src = {s: _new_acc() for s in AGGREGATOR_SOURCES}
    raw_best = _new_acc()
    net_src = {s: _new_acc() for s in AGGREGATOR_SOURCES}
    net_best = _new_acc()
    coverage = {
        s: {"ok": 0, "failed": 0, "error": 0, "unsupported": 0}
        for s in AGGREGATOR_SOURCES
    }
    mino_ok = 0
    mino_fail = 0
    net_adjustable_rows = 0

    for row in rows:
        results = row.get("results") or {}
        mino = results.get("minotaur") or {}
        mino_out = _int(mino.get("output_raw")) if mino.get("status") == "ok" else None
        mino_present = mino_out is not None and mino_out > 0
        mino_ok += 1 if mino_present else 0
        mino_fail += 0 if mino_present else 1

        # per-source coverage (independent of whether minotaur succeeded)
        for s in AGGREGATOR_SOURCES:
            st = (results.get(s) or {}).get("status")
            if st in coverage[s]:
                coverage[s][st] += 1

        if not mino_present:
            continue  # no comparable pairs without a Minotaur output

        # ── raw leaderboard ──
        best_out: int | None = None
        for s in AGGREGATOR_SOURCES:
            r = results.get(s) or {}
            if r.get("status") != "ok":
                continue
            s_out = _int(r.get("output_raw"))
            if s_out is None or s_out <= 0:
                continue
            _accumulate(raw_src[s], mino_out, s_out)
            best_out = s_out if best_out is None else max(best_out, s_out)
        if best_out is not None:
            _accumulate(raw_best, mino_out, best_out)

        # ── net-of-gas leaderboard ──
        gas_price = _int(row.get("gas_price_wei"))
        if gas_price is None:
            continue
        mode: str | None
        ref_rate: float | None = None
        if row.get("output_is_native"):
            mode = "output_native"
        elif row.get("input_is_native"):
            mode = "input_native"
            input_amount = _int(row.get("input_amount"))
            oks = [
                o for o in (
                    _int((results.get(s) or {}).get("output_raw"))
                    for s in SOURCES
                    if (results.get(s) or {}).get("status") == "ok"
                )
                if o and o > 0
            ]
            if input_amount and input_amount > 0 and oks:
                ref_rate = statistics.median(oks) / input_amount
            else:
                mode = None
        else:
            mode = None
        if mode is None:
            continue

        mino_net = _net_output(mino, gas_price, mode, ref_rate)
        if mino_net is None or mino_net <= 0:
            continue
        net_adjustable_rows += 1
        best_net: int | None = None
        for s in AGGREGATOR_SOURCES:
            s_net = _net_output(results.get(s) or {}, gas_price, mode, ref_rate)
            if s_net is None or s_net <= 0:
                continue
            _accumulate(net_src[s], mino_net, s_net)
            best_net = s_net if best_net is None else max(best_net, s_net)
        if best_net is not None:
            _accumulate(net_best, mino_net, best_net)

    raw_vs_source = {}
    for s in AGGREGATOR_SOURCES:
        block = _finalize(raw_src[s])
        block.update(
            source_ok=coverage[s]["ok"],
            source_failed=coverage[s]["failed"],
            source_error=coverage[s]["error"],
            source_unsupported=coverage[s]["unsupported"],
        )
        raw_vs_source[s] = block

    return {
        "chain_id": chain_id,
        "chain_name": get_chain_name(chain_id),
        "total_comparisons": len(rows),
        "minotaur_ok": mino_ok,
        "minotaur_fail": mino_fail,
        "raw": {
            "vs_best_aggregator": _finalize(raw_best),
            "vs_source": raw_vs_source,
        },
        "net_of_gas": {
            "note": _NET_NOTE,
            "gas_adjustable_comparisons": net_adjustable_rows,
            "vs_best_aggregator": _finalize(net_best),
            "vs_source": {s: _finalize(net_src[s]) for s in AGGREGATOR_SOURCES},
        },
    }


def build_stats_response(rows: list[dict[str, Any]], window_days: int) -> dict[str, Any]:
    """Group rows by chain and build the full ``/dex-compare/stats`` payload."""
    by_chain: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_chain[int(row["chain_id"])].append(row)
    chains = [compute_chain_stats(rws, cid) for cid, rws in sorted(by_chain.items())]
    return {
        "generated_at": time.time(),
        "window_days": window_days,
        "total_comparisons": len(rows),
        "sources": list(SOURCES),
        "chains": chains,
    }
