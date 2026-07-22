"""Blindspot detection for the DEX-compare service.

A *blindspot* is a real (``cow_onchain``) token pair the Minotaur solver could
NOT route — its quote came back ``failed`` / "no route / zero output". This
module classifies pairs over a window into:

* **open**    — currently unservable (the recent no-route rate is high).
* **covered** — WAS a blindspot earlier in the window and is now servable (the
                recent ``ok`` rate is high). A closed gap.

Detection is **passive**: it reads the timestamped comparison history and looks
for a per-pair ``failed → ok`` transition, using *rates across two sub-windows*
(not a single flip) so a pair that merely flaps in and out of routability isn't
mistaken for a durable recovery. A pair is only re-evaluated when the corpus
happens to re-sample it — so frequent pairs are classified promptly and the
long tail lags (an active re-probe of open blindspots would remove that lag).

Transient outcomes (RPC/solver ``error``, ``warming_up``, ``unsupported``) are
ignored — only genuine ``ok`` vs ``no_route`` count, matching the coverage
metric in :mod:`stats`.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from minotaur_subnet.blockchain.chains import get_chain_name

from .stats import _int

# Classification thresholds. Kept deliberately lenient on sample count because
# per-pair sampling is sparse; the counts are returned so a consumer can judge
# confidence itself.
MIN_SAMPLES = 2       # min (ok + no_route) samples in a sub-window before we classify
FAIL_THRESHOLD = 0.5  # >= this no-route rate ⇒ blindspot (open, or "was blindspot")
OK_THRESHOLD = 0.8    # >= this ok rate in the recent window ⇒ now servable (covered)


def _minotaur(row: dict[str, Any]) -> dict[str, Any]:
    return (row.get("results") or {}).get("minotaur") or {}


def _outcome(row: dict[str, Any]) -> str | None:
    """``"ok"`` (routed, output > 0), ``"no_route"`` (failed), or ``None`` for a
    transient/uncountable status."""
    m = _minotaur(row)
    status = m.get("status")
    if status == "ok":
        gross = _int(m.get("output_raw"))
        return "ok" if (gross is not None and gross > 0) else "no_route"
    if status == "failed":
        return "no_route"
    return None


def _pair_key(row: dict[str, Any]) -> tuple[Any, Any]:
    return (row.get("input_token"), row.get("output_token"))


def compute_chain_blindspots(
    rows: list[dict[str, Any]],
    chain_id: int,
    split_ts: float,
    limit: int,
) -> dict[str, Any]:
    """Classify one chain's ``cow_onchain`` pairs into open + covered.

    ``split_ts`` is the boundary between the *earlier* and *recent* sub-windows.
    """
    agg: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if row.get("trade_source") != "cow_onchain":
            continue
        outcome = _outcome(row)
        if outcome is None:
            continue
        key = _pair_key(row)
        pair = agg.get(key)
        if pair is None:
            pair = agg[key] = {
                "input": row.get("input_token"),
                "output": row.get("output_token"),
                "input_symbol": row.get("input_symbol"),
                "output_symbol": row.get("output_symbol"),
                "e_ok": 0, "e_nr": 0, "r_ok": 0, "r_nr": 0,
                "last_failed_at": None, "first_ok_recent_at": None, "last_ok_at": None,
                "last_error": None,
            }
        # Prefer the freshest resolved symbols we've seen for this pair.
        if row.get("input_symbol"):
            pair["input_symbol"] = row.get("input_symbol")
        if row.get("output_symbol"):
            pair["output_symbol"] = row.get("output_symbol")

        ts = float(row.get("created_at") or 0.0)
        recent = ts >= split_ts
        if outcome == "ok":
            pair["r_ok" if recent else "e_ok"] += 1
            pair["last_ok_at"] = ts if pair["last_ok_at"] is None else max(pair["last_ok_at"], ts)
            if recent:
                pair["first_ok_recent_at"] = (
                    ts if pair["first_ok_recent_at"] is None else min(pair["first_ok_recent_at"], ts)
                )
        else:  # no_route
            pair["r_nr" if recent else "e_nr"] += 1
            pair["last_failed_at"] = ts if pair["last_failed_at"] is None else max(pair["last_failed_at"], ts)
            pair["last_error"] = _minotaur(row).get("error") or pair["last_error"]

    open_list: list[dict[str, Any]] = []
    covered_list: list[dict[str, Any]] = []
    for pair in agg.values():
        e_total = pair["e_ok"] + pair["e_nr"]
        r_total = pair["r_ok"] + pair["r_nr"]
        e_fail = pair["e_nr"] / e_total if e_total else None
        r_fail = pair["r_nr"] / r_total if r_total else None
        r_ok = pair["r_ok"] / r_total if r_total else None
        base = {
            "input": pair["input"], "output": pair["output"],
            "input_symbol": pair["input_symbol"], "output_symbol": pair["output_symbol"],
        }

        # OPEN: currently failing at a high rate.
        if r_total >= MIN_SAMPLES and r_fail is not None and r_fail >= FAIL_THRESHOLD:
            open_list.append({
                **base,
                "recent_no_route": pair["r_nr"], "recent_ok": pair["r_ok"], "recent_total": r_total,
                "recent_fail_rate": round(r_fail, 4),
                "last_failed_at": pair["last_failed_at"], "last_error": pair["last_error"],
            })

        # COVERED: was a blindspot earlier, now servable at a high rate.
        was_blind = e_total >= MIN_SAMPLES and e_fail is not None and e_fail >= FAIL_THRESHOLD
        now_servable = r_total >= MIN_SAMPLES and r_ok is not None and r_ok >= OK_THRESHOLD
        if was_blind and now_servable:
            covered_list.append({
                **base,
                "earlier_no_route": pair["e_nr"], "earlier_total": e_total,
                "earlier_fail_rate": round(e_fail, 4),
                "recent_ok": pair["r_ok"], "recent_total": r_total, "recent_ok_rate": round(r_ok, 4),
                "covered_at": pair["first_ok_recent_at"], "last_ok_at": pair["last_ok_at"],
                "last_failed_at": pair["last_failed_at"],
            })

    open_list.sort(key=lambda d: d["recent_no_route"], reverse=True)
    covered_list.sort(key=lambda d: (d["covered_at"] or 0.0), reverse=True)  # freshest recovery first
    return {
        "chain_id": chain_id,
        "chain_name": get_chain_name(chain_id),
        "open_count": len(open_list),
        "covered_count": len(covered_list),
        "open": open_list[:limit],
        "covered": covered_list[:limit],
    }


def build_blindspots_response(
    rows: list[dict[str, Any]],
    window_days: int,
    recent_days: int,
    limit: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the per-chain blindspots response (mirrors build_stats_response)."""
    now = time.time() if now is None else now
    split_ts = now - recent_days * 86400
    by_chain: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_chain[int(row["chain_id"])].append(row)
    chains = [compute_chain_blindspots(rws, cid, split_ts, limit) for cid, rws in sorted(by_chain.items())]
    return {
        "generated_at": now,
        "window_days": window_days,
        "recent_days": recent_days,
        "source": "cow_onchain",
        "note": (
            "open = real CoW pairs the solver currently can't route (recent no-route rate "
            f">= {FAIL_THRESHOLD:.0%}); covered = pairs that were unservable earlier in the "
            f"window and are now servable (recent ok rate >= {OK_THRESHOLD:.0%}). Passive "
            "detection over re-sampled real trades — long-tail pairs lag until re-drawn."
        ),
        "chains": chains,
    }
