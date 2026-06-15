#!/usr/bin/env python3
"""Tally independent adopt votes across the validator fleet (CHALLENGER_QUORUM_MODE).

Each validator publishes its latest independent ADOPT/REJECT vote on `/health`
(`independent_vote`). This polls the fleet — seeded from one node's
`champion_consensus.peer_endpoints` (the same dynamic discovery the consensus uses) —
groups votes by candidate, and reports whether the ADOPT count reaches quorum.

This is the SHADOW tally: with `DISABLE_CHAMPION_ADOPTION` on no champion is actually
swapped, so it shows what the quorum WOULD decide — the evidence that the model adopts
good challengers and rejects bad ones BEFORE adoption is enabled.

Usage:
    tally_independent_votes.py --from-leader http://<leader>:8080
    tally_independent_votes.py http://v1:8080 http://v2:8080 ...
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse


def _fetch_health(base: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=timeout) as r:  # noqa: S310
        return json.load(r)


def _swap_port(url: str, port: int) -> str:
    p = urlparse(url if "://" in url else "http://" + url)
    return f"{p.scheme or 'http'}://{p.hostname or url}:{port}"


def fleet_from_leader(leader_base: str, api_port: int, timeout: float = 10.0) -> tuple[list[str], int]:
    """Return (fleet api URLs, quorum_required) from one node's /health."""
    d = _fetch_health(leader_base, timeout)
    cc = d.get("champion_consensus") or {}
    urls = [_swap_port(leader_base, api_port)]
    for pe in cc.get("peer_endpoints") or []:
        if pe.get("url"):
            urls.append(_swap_port(pe["url"], api_port))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out, int(cc.get("quorum_required") or 0)


def probe(base: str, timeout: float = 10.0) -> dict:
    try:
        d = _fetch_health(base, timeout)
        return {"url": base, "ok": True, "vote": d.get("independent_vote") or {}}
    except Exception as exc:  # noqa: BLE001
        return {"url": base, "ok": False, "error": str(exc)}


def tally(probes: list[dict], quorum: int) -> tuple[list[str], dict]:
    """Group votes by candidate and decide WOULD-ADOPT vs quorum. Returns
    (report_lines, {candidate: would_adopt_bool})."""
    lines: list[str] = []
    by_cand: dict[str, list] = defaultdict(list)
    for p in probes:
        if not p["ok"]:
            lines.append(f"  UNREACHABLE {p['url']}: {p['error']}")
            continue
        iv = p["vote"]
        cand = iv.get("candidate_id")
        if not cand:
            lines.append(f"  {p['url']}: no independent_vote published yet")
            continue
        by_cand[cand].append((p["url"], iv))

    verdicts: dict[str, bool] = {}
    for cand, voters in sorted(by_cand.items()):
        adopt = sum(1 for _, iv in voters if str(iv.get("vote")).upper() == "ADOPT")
        total = len(voters)
        would = quorum > 0 and adopt >= quorum
        verdicts[cand] = would
        lines.append(
            f"candidate={cand}: {adopt}/{total} ADOPT (quorum={quorum}) -> "
            f"{'WOULD ADOPT' if would else 'rejected (no quorum)'}"
        )
        for url, iv in voters:
            lines.append(
                f"    {str(iv.get('role', '?')):8} {str(iv.get('vote', '?')):6} "
                f"chal={iv.get('chal_score')} champ={iv.get('champ_score')} {url} "
                f"| {iv.get('reason', '')}"
            )
    return lines, verdicts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="*", help="validator base URLs")
    ap.add_argument("--from-leader", metavar="URL", help="seed fleet from this node's /health")
    ap.add_argument("--api-port", type=int, default=8080)
    ap.add_argument("--quorum", type=int, default=0, help="override quorum (else read from leader)")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    urls = list(args.urls)
    quorum = args.quorum
    if args.from_leader:
        fleet, q = fleet_from_leader(args.from_leader, args.api_port, timeout=args.timeout)
        urls += fleet
        if not quorum:
            quorum = q
    if not urls:
        ap.error("provide validator URLs (positional args or --from-leader)")

    probes = [probe(u, timeout=args.timeout) for u in urls]
    lines, _ = tally(probes, quorum)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
