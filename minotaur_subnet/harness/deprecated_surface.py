"""Wire-contract migration gates — the intake half of the migration ladder.

docs/architecture/sdk-v2-migration.md §5 ("the ladder"): once a wire field is
deprecated (Phase B), the door — not the population — is where migration is
enforced. Two gates, different jobs, BOTH observe-first:

  SDK VERSION FLOOR (``SDK_VERSION_FLOOR`` / ``SDK_VERSION_FLOOR_ENFORCE``)
      Coordination pressure: a submission whose vendored SDK reports a
      generation below the floor is flagged (observe) or rejected (enforce).
      Forces re-vendoring, which is what delivers the Phase-B deprecation
      warnings into the miner's own solver logs. The marker is SELF-REPORTED
      (a miner can fake the constant), so this gate encourages — it never
      proves.

  DEPRECATED SURFACE (``DEPRECATED_SURFACE_MODE`` = off | observe | enforce)
      Proof: a static scan of the submitted repo for reads of wire fields we
      have deprecated (the same depends/guarded/incidental classification as
      tools/solver_surface_audit.py — keep the two in sync). Per-submission,
      actionable (file:line named), and the property Phase C actually needs:
      once enforced through a few dethrone cycles, every image that can hold
      the throne is field-independent BY ADMISSION.

Defaults: floor OFF (no version configured), surface OBSERVE (log-only —
zero behaviour change, starts producing blast-radius data immediately).
Arming either to enforce is accept/reject-changing: fleet-uniform promote
first, announcement, then the env flip — the 2026-07-29 lesson (population-
keyed enforcement without a blast-radius measurement froze the contest) is
why observe is the default and why the scan names exact lines.

``prices`` is deliberately NOT in the scan set: the attribute name is too
generic to classify reliably from source lines (false rejects are the one
unforgivable failure of an intake gate). Its retirement rides the same
evidence via the audit's ``snapshot-all`` preset instead.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump on ANY change to the scan set or classification below.
DEPRECATED_SURFACE_VERSION = 1

# Wire fields deprecated in SDK v2 Phase B (#1231). Mirrors the audit's
# ``snapshot-dex`` preset — the retirement decision is gated on THAT audit,
# so the intake gate must test the same thing.
DEPRECATED_WIRE_SYMBOLS: tuple[str, ...] = ("pool_states", "dex_config")

# Classification — identical semantics to tools/solver_surface_audit.py:
# only an ATTRIBUTE access (foo.SYM) outside a getattr-with-default counts.
_GUARDED = re.compile(r"""getattr\s*\(\s*[^,()]+,\s*['"](?P<sym>\w+)['"]\s*,""")


def _attr_access(line: str, sym: str) -> bool:
    return re.search(rf"[\w\])]\s*\.\s*{re.escape(sym)}\b", line) is not None


def deprecated_surface_mode() -> str:
    """``off`` | ``observe`` (default) | ``enforce``.

    Observe is the default on purpose: it is log-only, and the blast-radius
    numbers it produces are the prerequisite for ever arming enforce.
    Unknown values fall back to ``observe`` (the safe, visible state).
    """
    v = os.environ.get("DEPRECATED_SURFACE_MODE", "").strip().lower()
    return v if v in ("off", "observe", "enforce") else "observe"


def surface_hits(repo_path: str, max_hits: int = 50) -> list[str]:
    """DEPENDS-class reads of deprecated wire fields, as ``path:line: code``.

    Line-based like the audit tool: a hit is an attribute access to a
    deprecated symbol that is not a getattr-with-default and not a comment.
    Never raises — an unreadable file contributes nothing (the gate must
    fail open on scan trouble, never reject on its own bug).
    """
    hits: list[str] = []
    root = Path(repo_path)
    try:
        files = sorted(root.rglob("*.py"))
    except OSError:
        return hits
    for f in files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = f
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for sym in DEPRECATED_WIRE_SYMBOLS:
                if not _attr_access(line, sym):
                    continue
                guarded = any(
                    m.group("sym") == sym for m in _GUARDED.finditer(line)
                )
                if guarded:
                    continue
                hits.append(f"{rel}:{n}: {stripped[:110]}")
                break
            if len(hits) >= max_hits:
                return hits
    return hits


# ── SDK version floor ────────────────────────────────────────────────────────


def sdk_version_floor() -> str | None:
    """The configured floor version string, or None (gate off)."""
    v = os.environ.get("SDK_VERSION_FLOOR", "").strip()
    return v or None


def sdk_floor_enforced() -> bool:
    return os.environ.get("SDK_VERSION_FLOOR_ENFORCE", "").strip() == "1"


def _parse_version(v: str | None) -> tuple[int, ...]:
    """Lenient semver-ish parse; pre-marker (None/garbage) is (0,) — below
    every configured floor, which is exactly what pre-marker means."""
    if not v:
        return (0,)
    parts: list[int] = []
    for p in str(v).split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def below_floor(reported: str | None, floor: str) -> bool:
    return _parse_version(reported) < _parse_version(floor)
