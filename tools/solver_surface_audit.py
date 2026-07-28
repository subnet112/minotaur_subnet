#!/usr/bin/env python3
"""Do any LIVE solvers reference a symbol we are about to remove?

Why this exists
---------------
``MarketSnapshot.pool_states`` was removed on the evidence that nothing in
this repo read it. The live champion reads it. The batch was reverted (#1172).

An in-repo grep answers "no consumers WE CAN SEE" — solver code lives in
miners' repos, forked from a base we publish, and is invisible here. This
reads the built solver images on the validator, where the real answer is.

HOW A REMOVAL ACTUALLY REACHES A SOLVER — this is not obvious, and getting it
wrong in either direction is expensive:

Solvers VENDOR the SDK. ``/app/minotaur_subnet`` inside the image is the
miner's own copy, and ``import minotaur_subnet`` in the container resolves
there, never to the validator's. The container runs its own
``harness/runner.py`` -> ``dict_to_snapshot`` over JSON on stdin. So:

  SDK SYMBOL (a class, a helper — SwapIntentContext, normalize_*)
      Removing it from THIS repo does not touch the vendored copy. The solver
      keeps importing its own. No crash. It diverges only when the miner
      re-vendors.

  WIRE FIELD (a MarketSnapshot / IntentState attribute we SERIALISE)
      Removing it means we stop SENDING the key. The vendored
      ``dict_to_snapshot`` then defaults it — ``d.get("pool_states", {})`` —
      so ``snapshot.pool_states`` yields ``{}``, NOT AttributeError. The
      hazard is a SILENT BEHAVIOUR CHANGE: a fallback path quietly goes dead
      and scores move with no error anywhere.

  PROTOCOL SHAPE (renaming a JSON key the vendored runner requires)
      This is the one that genuinely crashes an existing solver.

So a hit here is rarely "it will explode". It is "this solver's behaviour
changes and nothing will tell you". That still warrants a deprecation cycle —
silent score movement under the incumbent is harder to notice than a crash,
not easier.

Use it as a PRE-MERGE GATE for any change to the published-interface surface
listed in docs/architecture/sdk-v2-migration.md §2: MarketSnapshot fields,
IntentState / typed_context, the IntentSolver ABC, SolverMetadata, the
harness IPC wire shape, and initialize(config) keys.

Read-only: every container runs with ``--network none`` and no mounts, and
nothing is written to any image.

Usage
-----
On the validator (has both docker and the images)::

    ./tools/solver_surface_audit.py --symbol pool_states --symbol dex_config
    ./tools/solver_surface_audit.py --preset snapshot-dex
    ./tools/solver_surface_audit.py --preset v3-contexts

Exit status is 1 when any live solver references a queried symbol — so it can
gate a merge directly.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys

# Symbol groups matching the removals we have attempted or plan to.
PRESETS: dict[str, list[str]] = {
    # #1161 — MarketSnapshot's Uniswap-shaped fields.
    "snapshot-dex": ["pool_states", "dex_config"],
    # #1163 — the v3 archetype contexts. NOT yet audited against the slate;
    # whether that removal was independently safe is still unknown.
    "v3-contexts": [
        "SwapIntentContext", "TwapIntentContext", "RebalanceIntentContext",
    ],
    # Anything a solver reads off the snapshot.
    "snapshot-all": ["pool_states", "dex_config", "prices", "raw_state"],
    # The normalisers #1163 moved out of v3/manifest.
    "manifest-normalisers": [
        "normalize_swap_intent_params",
        "normalize_twap_intent_params",
        "normalize_rebalance_intent_params",
    ],
}

# Not every mention is a hazard, and a gate that cries wolf gets ignored.
# Three classes, and only the first breaks on removal:
#
#   depends    <expr>.SYM / import SYM — this solver's behaviour changes
#   guarded    getattr(x, "SYM", d)  — explicitly written to tolerate absence
#   incidental SYM as a local/param/dict key — the solver's own name, unrelated
#
# "depends" is NOT "crashes" — see the vendoring note above. It means the
# solver reads something we would stop providing, and would silently take a
# different path. The champion's `snapshot.pool_states` sits inside a
# truthiness guard, so it degrades to the RPC branch; the same file also
# defines `def _best_direct(pool_states, ...)`, which is just a parameter.
_GUARDED = re.compile(r"""getattr\s*\(\s*[^,()]+,\s*['"](?P<sym>\w+)['"]\s*,""")


def _attr_access(line: str, sym: str) -> bool:
    """True when SYM is reached as an ATTRIBUTE (foo.SYM), not as a bare name."""
    return re.search(rf"[\w\])]\s*\.\s*{re.escape(sym)}\b", line) is not None


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as exc:
        return 127, str(exc)


def discover_images(pattern: str) -> list[str]:
    """Local solver images, newest first."""
    rc, out = _run([
        "docker", "images", "--format", "{{.Repository}}:{{.Tag}}",
    ])
    if rc != 0:
        return []
    return [
        line.strip() for line in out.splitlines()
        if line.strip() and re.search(pattern, line.strip())
    ]


def grep_image(image: str, symbols: list[str], root: str) -> list[str]:
    """Lines in ``image`` referencing any symbol. Empty when clean.

    Runs sandboxed: no network, no mounts, nothing written.
    """
    pattern = r"\|".join(re.escape(s) for s in symbols)
    rc, out = _run([
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "sh",
        image, "-c",
        f"grep -rn '{pattern}' {root} --include=*.py 2>/dev/null | head -200",
    ])
    if rc in (124, 127):
        return [f"!! could not inspect ({out.strip()[:80]})"]
    return [ln for ln in out.splitlines() if ln.strip()]


def classify(line: str, symbols: list[str]) -> str:
    """fatal | guarded | incidental — see the note above."""
    guarded = {m.group("sym") for m in _GUARDED.finditer(line)}
    hit = [s for s in symbols if s in line]
    # An attribute access anywhere on the line that ISN'T getattr-guarded is
    # the one that raises.
    for sym in hit:
        if _attr_access(line, sym) and sym not in guarded:
            return "depends"
    if any(s in guarded for s in hit):
        return "guarded"
    # A class NAME being referenced at all matters (import / isinstance /
    # construction all break on removal), unlike a lowercase field name that
    # may just be a local.
    if any(s in line and s[:1].isupper() for s in hit):
        return "depends"
    return "incidental"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", action="append", default=[],
                    help="symbol to search for (repeatable)")
    ap.add_argument("--preset", choices=sorted(PRESETS),
                    help=f"symbol group: {', '.join(sorted(PRESETS))}")
    ap.add_argument("--image", action="append", default=[],
                    help="explicit image (repeatable); default = discovered")
    ap.add_argument("--image-pattern", default=r"^solver-",
                    help="regex for image discovery (default: ^solver-)")
    ap.add_argument("--root", default="/app",
                    help="path inside the image to search (default: /app)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    symbols = list(args.symbol) + (PRESETS[args.preset] if args.preset else [])
    if not symbols:
        ap.error("give --symbol and/or --preset")

    if not shutil.which("docker"):
        print("docker not available — run this on a validator", file=sys.stderr)
        return 2

    images = args.image or discover_images(args.image_pattern)
    if not images:
        print(
            f"no images matched {args.image_pattern!r} — nothing to audit.\n"
            "That is NOT a pass: it means this host has no solver images, so "
            "the question is unanswered.",
            file=sys.stderr,
        )
        return 2

    findings: dict[str, list[dict[str, str]]] = {}
    for image in images:
        hits = grep_image(image, symbols, args.root)
        if hits:
            findings[image] = [
                {"line": h, "severity": classify(h, symbols)} for h in hits
            ]

    if args.json:
        print(json.dumps({
            "symbols": symbols, "images_audited": images, "findings": findings,
        }, indent=1))
    else:
        print(f"symbols : {', '.join(symbols)}")
        print(f"images  : {len(images)} audited\n")
        if not findings:
            print("  CLEAN — no live solver references these symbols.")
        for image, hits in findings.items():
            by = {k: [h for h in hits if h["severity"] == k]
                  for k in ("depends", "guarded", "incidental")}
            print(
                f"  {image}\n"
                f"    {len(by["depends"])} depend, {len(by["guarded"])} guarded, "
                f"{len(by['incidental'])} incidental (of {len(hits)})"
            )
            for h in (by["depends"] + by["guarded"])[:8]:
                mark = "DEPENDS" if h["severity"] == "depends" else "guarded"
                print(f"      [{mark}] {h['line'].strip()[:130]}")
            extra = len(by["depends"]) + len(by["guarded"]) - 8
            if extra > 0:
                print(f"      … {extra} more depends/guarded")

        if findings:
            any_fatal = any(
                h["severity"] == "depends"
                for hits in findings.values() for h in hits
            )
            if not any_fatal:
                print(
                    "\n  (only guarded / incidental references — no crash on "
                    "removal)"
                )
            print(
                "\n  VERDICT: NEEDS A DEPRECATION CYCLE — "
                + ("live solvers read this; removing it changes their "
                   "behaviour SILENTLY (vendored SDK defaults the field, so "
                   "no error is raised anywhere). Stop populating it first, "
                   "keep the field, and retire once this audit is clean."
                   if any_fatal else
                   "references are getattr-guarded and tolerate absence.")
            )
    fatal_found = any(
        h["severity"] == "depends" for hits in findings.values() for h in hits
    )
    # Gate on FATAL only. Flagging a solver's own local variable named
    # `pool_states` would make this noisy enough to be ignored, which is worse
    # than not having it.
    return 1 if fatal_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
