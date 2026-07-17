"""Aggregate comparison rows into per-chain leaderboards.

Two leaderboards per chain:

* **raw** — each source's GROSS output as reported (ignores fees + gas). Kept for
  reference; it flatters Minotaur, whose basis is pre-platform-fee.
* **net** — what the user actually receives: each source's output AFTER its own
  protocol fee, minus the gas the taker pays; Minotaur's gross minus our platform
  fee and gas. This is the honest, apples-to-apples number.

Converting an ETH-denominated cost (gas, our platform fee) into output-token base
units uses, in priority: output==WETH (1:1) → Velora's USD fields (destUSD gives
the output token's USD price, native_usd gives ETH's) → input==WETH reference
rate. When none apply the cost can't be converted; gas is then treated as ~0
(negligible at normalized trade sizes), but an UNCONVERTIBLE Minotaur platform
fee excludes the row from the net set (never silently drop our own fee).
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Any

from minotaur_subnet.blockchain.chains import get_chain_name

from .models import AGGREGATOR_SOURCES, SOURCES

CAVEATS_NOTE = (
    "net = what the user receives (after each source's fee + gas, and Minotaur's "
    "platform fee). raw = gross output, pre-fee/gas (flatters Minotaur). Trades are "
    "rescaled to a target USD notional so fixed costs don't dominate; see notional_usd."
)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
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


class _RowCtx:
    """Per-row conversion context: turns ETH-wei costs into output-token units."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.results = row.get("results") or {}
        self.output_is_native = bool(row.get("output_is_native"))
        self.input_is_native = bool(row.get("input_is_native"))
        self.gas_price = _int(row.get("gas_price_wei"))
        self.native_usd = _float(row.get("native_usd"))
        self.input_amount = _int(row.get("input_amount"))
        self.notional_usd = _float(row.get("notional_usd"))
        # median of the sources' OK output amounts — the trade's typical output
        oks = [
            _int((self.results.get(s) or {}).get("output_raw"))
            for s in SOURCES
            if (self.results.get(s) or {}).get("status") == "ok"
        ]
        oks = [o for o in oks if o and o > 0]
        self.median_output: int | None = statistics.median(oks) if oks else None
        # shared reference rate: output base-units per 1 input-native wei
        self.ref_rate: float | None = None
        if self.input_is_native and self.input_amount and self.median_output and self.input_amount > 0:
            self.ref_rate = self.median_output / self.input_amount

    def native_to_output(self, native_wei: int | None) -> int | None:
        """Convert a native (ETH) wei amount to output-token base units."""
        if native_wei is None:
            return None
        if native_wei == 0:
            return 0
        if self.output_is_native:
            return native_wei
        # Notional-based: a normalized trade's USD size + its output amount give the
        # output token's USD price directly — robust even when Velora fails (which
        # it does on illiquid Base tokens, the reason Base `net` was empty).
        if self.notional_usd and self.native_usd and self.median_output and self.notional_usd > 0:
            # output_usd_per_unit = notional_usd / median_output
            return int(native_wei * (self.native_usd / 1e18) * self.median_output / self.notional_usd)
        vel = self.results.get("velora") or {}
        v_out = _int(vel.get("output_raw"))
        v_usd = _float(vel.get("output_usd"))
        if vel.get("status") == "ok" and self.native_usd and v_out and v_usd and v_out > 0 and v_usd > 0:
            usd_per_out = v_usd / v_out               # USD per output base-unit
            return int(native_wei * (self.native_usd / 1e18) / usd_per_out)
        if self.input_is_native and self.ref_rate:
            return int(native_wei * self.ref_rate)
        return None


def _after_fee(result: dict[str, Any]) -> int | None:
    v = _int(result.get("output_after_fee_raw"))
    return v if v is not None else _int(result.get("output_raw"))


def _gas_out(source: str, result: dict[str, Any], ctx: _RowCtx) -> int | None:
    """Gas cost in output base units for a source, or None if uncomputable.

    Uncomputable gas must NOT silently become 0 for one source only — the caller
    drops gas for EVERY source in the row when any one is uncomputable, so the
    comparison stays symmetric (a per-source zero one-directionally biases it)."""
    if source == "velora":
        gas_usd = _float(result.get("gas_usd"))
        out_gross = _int(result.get("output_raw"))
        out_usd = _float(result.get("output_usd"))
        if gas_usd is not None and out_gross and out_usd and out_usd > 0:
            return max(0, int(gas_usd * out_gross / out_usd))
        return None
    gnw = result.get("gas_native_wei")
    if gnw is not None:                     # 0x — totalNetworkFee (exact native wei)
        return ctx.native_to_output(_int(gnw))
    g = _int(result.get("gas_units"))       # 1inch / Minotaur — units * snapshot price
    if g is None or ctx.gas_price is None:
        return None
    return ctx.native_to_output(g * ctx.gas_price)


REALISTIC_USD = 100.0  # trades >= this are "realistic"; below is dust where fixed costs dominate


def _trade_usd(row: dict[str, Any]) -> float | None:
    """USD size of the trade: the normalized notional, else Velora's srcUSD."""
    n = _float(row.get("notional_usd"))
    if n and n > 0:
        return n
    vel = (row.get("results") or {}).get("velora") or {}
    iu = _float(vel.get("input_usd"))
    if vel.get("status") == "ok" and iu and iu > 0:
        return iu
    return None


def _net_leaderboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Net (fee+gas adjusted, symmetric gas) leaderboard over ``rows``."""
    net_src = {s: _new_acc() for s in AGGREGATOR_SOURCES}
    net_best = _new_acc()
    for row in rows:
        results = row.get("results") or {}
        mino = results.get("minotaur") or {}
        if mino.get("status") != "ok":
            continue
        mino_gross = _int(mino.get("output_raw"))
        if mino_gross is None or mino_gross <= 0:
            continue
        ctx = _RowCtx(row)
        # Minotaur platform fee -> output units; unconvertible fee excludes the row.
        fee_native = _int(mino.get("fee_raw"))
        if fee_native and fee_native > 0:
            fee_out = ctx.native_to_output(fee_native)
            if fee_out is None:
                continue
        else:
            fee_out = 0
        ok_aggs: list[list[Any]] = []
        for s in AGGREGATOR_SOURCES:
            r = results.get(s) or {}
            if r.get("status") != "ok":
                continue
            af = _after_fee(r)
            if af is None or af <= 0:
                continue
            gas_out = 0 if r.get("is_net_of_gas") else _gas_out(s, r, ctx)
            ok_aggs.append([s, af, gas_out])
        if not ok_aggs:
            continue
        mino_gas_out = _gas_out("minotaur", mino, ctx)
        # SYMMETRY: if gas can't be computed for EVERY side, drop it for ALL sides.
        if mino_gas_out is None or any(g is None for _, _, g in ok_aggs):
            mino_gas_out = 0
            for a in ok_aggs:
                a[2] = 0
        m_net = max(0, mino_gross - fee_out - mino_gas_out)
        if m_net <= 0:
            continue
        best_net: int | None = None
        for s, af, gas_out in ok_aggs:
            s_net = max(0, af - gas_out)
            if s_net <= 0:
                continue
            _accumulate(net_src[s], m_net, s_net)
            best_net = s_net if best_net is None else max(best_net, s_net)
        if best_net is not None:
            _accumulate(net_best, m_net, best_net)
    return {
        "vs_best_aggregator": _finalize(net_best),
        "vs_source": {s: _finalize(net_src[s]) for s in AGGREGATOR_SOURCES},
    }


def _cost_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Median of Minotaur's platform fee and gas, in $ and as % of trade size —
    lets the frontend attribute the net gap (small on realistic trades, large on dust)."""
    fee_usd: list[float] = []
    gas_usd: list[float] = []
    fee_pct: list[float] = []
    gas_pct: list[float] = []
    for row in rows:
        mino = (row.get("results") or {}).get("minotaur") or {}
        if mino.get("status") != "ok":
            continue
        nusd = _float(row.get("native_usd"))
        if not nusd or nusd <= 0:
            continue
        tusd = _trade_usd(row)
        fw = _int(mino.get("fee_raw"))
        if fw is not None:
            f = fw / 1e18 * nusd
            fee_usd.append(f)
            if tusd:
                fee_pct.append(100 * f / tusd)
        g = _int(mino.get("gas_units"))
        gp = _int(row.get("gas_price_wei"))
        if g is not None and gp:
            gu = g * gp / 1e18 * nusd
            gas_usd.append(gu)
            if tusd:
                gas_pct.append(100 * gu / tusd)

    def _med(vals: list[float]) -> float | None:
        return round(statistics.median(vals), 6) if vals else None

    return {
        "samples": len(fee_usd),
        "platform_fee_usd_median": _med(fee_usd),
        "gas_usd_median": _med(gas_usd),
        "platform_fee_pct_of_trade_median": _med(fee_pct),
        "gas_pct_of_trade_median": _med(gas_pct),
    }


# ── Liquidity gate ────────────────────────────────────────────────────────────
# A trade only counts toward the leaderboards when it is genuinely tradeable at the
# target size. Velora's router applies a max-price-impact guard and REJECTS trades
# whose impact exceeds real pool liquidity (it errors with
# ESTIMATED_LOSS_GREATER_THAN_MAX_IMPACT); a successful Velora quote is therefore a
# per-trade liquidity signal. Gating on it drops the illiquid Base trades where
# Velora max-impacts but the other routers still quote optimistically — there
# Minotaur "beats" a depleted competitor set, producing a bogus >100% "receives"
# that contradicts the per-source view (we trail Velora when it CAN quote). Liquid
# chains (Ethereum: Velora quotes every trade) are unaffected. Measured effect on
# Base: vs_best 1.0002 → 0.9985 (behind best, reconciled with the field), at the
# cost of a smaller, honest sample. A spread/consensus filter does NOT work — the
# other routers agree tightly on illiquid trades Velora rejects, so disagreement is
# not the signal; Velora's coverage is.
LIQUIDITY_GATE_SOURCE = "velora"


def _tradeable_at_size(row: dict[str, Any]) -> bool:
    gate = (row.get("results") or {}).get(LIQUIDITY_GATE_SOURCE) or {}
    return gate.get("status") == "ok"


def _top_unservable(rows: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    """Most-frequent real (cow_onchain) token pairs the solver could NOT route —
    the addressable demand we don't serve yet. Only genuine no-route ('failed')
    gaps count; transient errors are excluded."""
    counts: dict[tuple, int] = {}
    last_err: dict[tuple, Any] = {}
    for row in rows:
        if row.get("trade_source") != "cow_onchain":
            continue
        mino = (row.get("results") or {}).get("minotaur") or {}
        if mino.get("status") != "failed":
            continue
        pair = (
            row.get("input_symbol") or row.get("input_token"),
            row.get("output_symbol") or row.get("output_token"),
        )
        counts[pair] = counts.get(pair, 0) + 1
        last_err[pair] = mino.get("error")
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [
        {"input": p[0], "output": p[1], "count": c, "last_error": last_err[p]}
        for p, c in ranked
    ]


def compute_chain_stats(rows: list[dict[str, Any]], chain_id: int) -> dict[str, Any]:
    coverage = {s: {"ok": 0, "failed": 0, "error": 0, "unsupported": 0} for s in AGGREGATOR_SOURCES}
    mino_ok = mino_fail = 0
    normalized = 0
    # Coverage buckets — real (cow_onchain) trades the solver could/couldn't requote.
    cov_ok = cov_no_route = cov_error = 0

    # Pass 1 — collection stats over EVERY row (describe what we gathered, gate-independent).
    for row in rows:
        results = row.get("results") or {}
        if row.get("notional_usd"):
            normalized += 1
        mino = results.get("minotaur") or {}
        mino_status = mino.get("status")
        mino_gross = _int(mino.get("output_raw")) if mino_status == "ok" else None
        present = mino_gross is not None and mino_gross > 0
        mino_ok += 1 if present else 0
        mino_fail += 0 if present else 1
        if row.get("trade_source") == "cow_onchain":  # coverage: real executed trades only
            if present:
                cov_ok += 1
            elif mino_status == "failed":            # genuine no-route -> coverage gap
                cov_no_route += 1
            else:                                    # transient RPC/solver error / warming_up
                cov_error += 1
        for s in AGGREGATOR_SOURCES:
            st = (results.get(s) or {}).get("status")
            if st in coverage[s]:
                coverage[s][st] += 1

    # ── raw leaderboard (gross output) over ALL present rows ──
    # RAW is the reference "routing quality" view (flatters Minotaur, pre-fee/gas)
    # and is deliberately NOT liquidity-gated — the gate applies only to the honest
    # NET metrics below.
    raw_src = {s: _new_acc() for s in AGGREGATOR_SOURCES}
    raw_best = _new_acc()
    for row in rows:
        results = row.get("results") or {}
        mino = results.get("minotaur") or {}
        mino_gross = _int(mino.get("output_raw")) if mino.get("status") == "ok" else None
        if mino_gross is None or mino_gross <= 0:
            continue
        best_out: int | None = None
        for s in AGGREGATOR_SOURCES:
            r = results.get(s) or {}
            if r.get("status") != "ok":
                continue
            s_out = _int(r.get("output_raw"))
            if s_out is None or s_out <= 0:
                continue
            _accumulate(raw_src[s], mino_gross, s_out)
            best_out = s_out if best_out is None else max(best_out, s_out)
        if best_out is not None:
            _accumulate(raw_best, mino_gross, best_out)

    # NET is the honest 'receives' metric (drives the meter) and IS liquidity-gated:
    # only trades the gate source can quote at the traded size are cleanly
    # comparable, so Minotaur can't 'win' against a field depleted by illiquidity
    # (the Base >100% artifact). Split by trade size for the frontend. See
    # _tradeable_at_size.
    tradeable = [r for r in rows if _tradeable_at_size(r)]
    net_realistic = [r for r in tradeable if (_trade_usd(r) or 0) >= REALISTIC_USD]
    net_small = [r for r in tradeable if 0 < (_trade_usd(r) or 0) < REALISTIC_USD]

    # cost_breakdown is Minotaur's OWN fee/gas (no competitor needed) — computed
    # over ALL normalized rows, UNGATED, split by trade size.
    cost_realistic = [r for r in rows if (_trade_usd(r) or 0) >= REALISTIC_USD]
    cost_small = [r for r in rows if 0 < (_trade_usd(r) or 0) < REALISTIC_USD]

    raw_vs_source = {}
    for s in AGGREGATOR_SOURCES:
        block = _finalize(raw_src[s])
        block.update(
            source_ok=coverage[s]["ok"], source_failed=coverage[s]["failed"],
            source_error=coverage[s]["error"], source_unsupported=coverage[s]["unsupported"],
        )
        raw_vs_source[s] = block

    return {
        "chain_id": chain_id,
        "chain_name": get_chain_name(chain_id),
        "total_comparisons": len(rows),
        "normalized_comparisons": normalized,
        "tradeable_comparisons": len(tradeable),
        "minotaur_ok": mino_ok,
        "minotaur_fail": mino_fail,
        "coverage": {
            "note": "fraction of REAL executed CoW trades the current solver can "
                    "requote; coverage_rate excludes transient RPC/solver errors. "
                    "Inert (zeros/None) for historical-only chains.",
            "source": "cow_onchain",
            "real_trades_attempted": cov_ok + cov_no_route + cov_error,
            "minotaur_ok": cov_ok,
            "minotaur_no_route": cov_no_route,
            "minotaur_error": cov_error,
            "coverage_rate": round(cov_ok / (cov_ok + cov_no_route), 4)
                             if (cov_ok + cov_no_route) else None,
            "top_unservable_pairs": _top_unservable(rows, 10),
        },
        "net": {
            "note": "what the user actually receives (after fees + gas) — the honest metric",
            **_net_leaderboard(tradeable),
        },
        "net_by_size": {
            "note": "net split by trade size — 'realistic' isolates the solver's true "
                    "competitiveness from fixed-cost drag on tiny trades",
            "realistic_threshold_usd": REALISTIC_USD,
            "realistic": {"comparisons": len(net_realistic), **_net_leaderboard(net_realistic)},
            "small": {"comparisons": len(net_small), **_net_leaderboard(net_small)},
        },
        "cost_breakdown": {
            "note": "Minotaur's platform fee + gas as $ and % of trade — the source of "
                    "the net gap; shrinks toward zero as trade size grows (raw shows "
                    "routing is at parity, so the gap is cost, not the solver)",
            "all": _cost_breakdown(rows),
            "realistic": _cost_breakdown(cost_realistic),
            "small": _cost_breakdown(cost_small),
        },
        "raw": {
            "note": "gross output, pre-fee/pre-gas (routing quality — Minotaur ~parity here)",
            "vs_best_aggregator": _finalize(raw_best),
            "vs_source": raw_vs_source,
        },
        "liquidity_gate": {
            "note": "the NET / net_by_size metrics count only trades tradeable at the "
                    f"traded size — those where {LIQUIDITY_GATE_SOURCE} (max-impact-guarded) "
                    "returns a quote; illiquid trades it rejects are excluded so Minotaur "
                    "can't 'win' against a depleted field. raw + cost_breakdown are ungated. "
                    "total-tradeable = trades too illiquid to compare.",
            "gate_source": LIQUIDITY_GATE_SOURCE,
            "applies_to": ["net", "net_by_size"],
            "total": len(rows),
            "tradeable": len(tradeable),
        },
    }


def build_stats_response(rows: list[dict[str, Any]], window_days: int) -> dict[str, Any]:
    by_chain: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_chain[int(row["chain_id"])].append(row)
    chains = [compute_chain_stats(rws, cid) for cid, rws in sorted(by_chain.items())]
    return {
        "generated_at": time.time(),
        "window_days": window_days,
        "total_comparisons": len(rows),
        "sources": list(SOURCES),
        "note": CAVEATS_NOTE,
        "chains": chains,
    }
