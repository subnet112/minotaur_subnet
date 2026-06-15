#!/usr/bin/env python3
"""Verify cross-validator determinism parity by polling /health round-anchor probes.

For p2oc (``ADOPT_RULE=p2oc``) to be safe to flip fleet-wide, every validator
must score challengers on the *same* fork block with the *same* chain state, so
they reach the same on-chain verdict and consensus doesn't split. Each validator
already publishes, on ``/health`` under ``round_anchor`` (default-on, observe-only):

* ``anchor_epoch`` — the epoch whose deterministic timestamp anchors the pin,
* ``pins``         — the canonical per-chain fork block it derived,
* ``pin_hashes``   — the block hash AT that pin per chain (added in #182).

Two validators that, for the same ``anchor_epoch``, derived the same ``pins`` AND
read the same ``pin_hashes`` are forking byte-identical chain state → their sims
are deterministic. A mismatch in ``pins`` means their RPC heads/derivation
disagree; a mismatch in ``pin_hashes`` means their RPCs see different chains
(reorg / archive inconsistency). Either way: **do NOT flip p2oc.**

This needs no log access and no operator action on the polled nodes — just HTTP
reachability — so it works across third-party validators we don't control.

Usage::

    check_determinism_parity.py http://leader:8080 http://peer-a:8080 ...
    check_determinism_parity.py --file fleet_urls.txt

Exit 0 = parity (safe), 1 = divergence or nothing reachable.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse


def _fetch_health(base: str, timeout: float = 10.0) -> dict:
    url = base.rstrip("/") + "/health"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted ops URLs)
        return json.load(resp)


def fetch_round_anchor(base: str, timeout: float = 10.0) -> dict:
    """GET <base>/health and return its ``round_anchor`` block (or {})."""
    return _fetch_health(base, timeout).get("round_anchor") or {}


def _swap_port(url: str, port: int) -> str:
    """Same host, given port — the determinism endpoint is the api (:8080),
    while discovered peer URLs advertise the daemon axon (:9100 convention)."""
    p = urlparse(url if "://" in url else "http://" + url)
    return f"{p.scheme or 'http'}://{p.hostname or url}:{port}"


def fleet_from_leader(leader_base: str, api_port: int, timeout: float = 10.0) -> list[str]:
    """Seed the fleet from one node's /health ``champion_consensus.peer_endpoints``.

    That is the SAME dynamically-discovered peer set the consensus loops use
    (metagraph ∩ on-chain ValidatorRegistry, /identity-verified) — no metagraph
    re-walk. Returns api base URLs (leader + peers) normalized to ``api_port``.
    """
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
    return out


def _norm_hashes(d: dict) -> dict:
    """Canonicalize block hashes for comparison: lowercase, strip any 0x — so a
    web3-version format difference can't masquerade as a determinism DIVERGE."""
    out = {}
    for k, v in (d or {}).items():
        s = str(v).lower()
        out[k] = s[2:] if s.startswith("0x") else s
    return out


def probe(base: str, timeout: float = 10.0) -> dict:
    try:
        ra = fetch_round_anchor(base, timeout=timeout)
        return {
            "url": base,
            "ok": True,
            "anchor_epoch": ra.get("anchor_epoch"),
            "status": ra.get("status"),
            "pins": ra.get("pins") or {},
            "pin_hashes": ra.get("pin_hashes") or {},
        }
    except Exception as exc:  # noqa: BLE001 — report, never crash the sweep
        return {"url": base, "ok": False, "error": str(exc)}


def summarize(probes: list[dict]) -> tuple[list[str], bool]:
    """Pure diff: group reachable probes by anchor_epoch and check parity.

    Returns (report_lines, overall_ok). overall_ok is True iff at least one node
    was reachable AND, within every anchor_epoch group, all nodes agree on both
    ``pins`` and ``pin_hashes``.
    """
    lines: list[str] = []
    reachable = [p for p in probes if p["ok"]]
    for p in probes:
        if not p["ok"]:
            lines.append(f"  UNREACHABLE {p['url']}: {p['error']}")
    if not reachable:
        lines.append("  no validators reachable")
        return lines, False

    by_epoch: dict[object, list[dict]] = defaultdict(list)
    for p in reachable:
        by_epoch[p["anchor_epoch"]].append(p)

    overall_ok = True
    # None-epoch (deferred / no anchor) sorts last; real epochs ascending.
    for epoch in sorted(by_epoch, key=lambda e: (e is None, e)):
        group = by_epoch[epoch]
        pins_variants = {json.dumps(p["pins"], sort_keys=True) for p in group}
        hash_variants = {
            json.dumps(_norm_hashes(p["pin_hashes"]), sort_keys=True) for p in group
        }
        pins_ok = len(pins_variants) == 1
        hashes_ok = len(hash_variants) == 1
        all_have_hashes = all(p["pin_hashes"] for p in group)
        verdict = "AGREE" if (pins_ok and hashes_ok) else "DIVERGE"
        if not (pins_ok and hashes_ok):
            overall_ok = False
        note = "" if all_have_hashes else " [some nodes lack pin_hashes — pre-#182 image]"
        lines.append(
            f"epoch={epoch} n={len(group)} "
            f"pins={'OK' if pins_ok else 'MISMATCH'} "
            f"pin_hashes={'OK' if hashes_ok else 'MISMATCH'}{note} -> {verdict}"
        )
        if not pins_ok:
            for p in group:
                lines.append(f"    {p['url']}: pins={p['pins']}")
        if not hashes_ok:
            for p in group:
                lines.append(f"    {p['url']}: pin_hashes={p['pin_hashes']}")
    return lines, overall_ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="*", help="validator base URLs (e.g. http://host:8080)")
    ap.add_argument("--file", help="file of validator URLs, one per line (# comments ok)")
    ap.add_argument(
        "--from-leader",
        metavar="URL",
        help="seed the fleet from this node's /health peer_endpoints (reuses the "
        "consensus peer discovery — no metagraph re-walk)",
    )
    ap.add_argument(
        "--api-port",
        type=int,
        default=8080,
        help="port to poll /health on (default 8080; discovered peer URLs advertise "
        "the daemon axon port, so the host is reused with this port)",
    )
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if args.file:
        with open(args.file) as fh:
            urls += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if args.from_leader:
        urls += fleet_from_leader(args.from_leader, args.api_port, timeout=args.timeout)
    if not urls:
        ap.error("provide validator URLs (positional args, --file, or --from-leader)")

    probes = [probe(u, timeout=args.timeout) for u in urls]
    lines, ok = summarize(probes)
    print("\n".join(lines))
    print()
    print(
        "RESULT: PARITY OK — determinism gate-2 holds for these nodes"
        if ok
        else "RESULT: DIVERGENCE — do NOT flip ADOPT_RULE=p2oc"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
