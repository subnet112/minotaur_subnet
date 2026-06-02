"""Runtime build-version resolution for the /health endpoints.

Published images bake ``MINOTAUR_IMAGE_SHA`` at build time (Dockerfile ARG,
set by CI to the git SHA). Operators who build from source themselves —
especially bare-metal runs without Docker — never set it, so /health reported
the uninformative ``"dev"``. This resolves a real version for them.

Resolution order:
  1. ``MINOTAUR_IMAGE_SHA`` env (CI/published images) — 7-char prefix.
  2. ``git rev-parse`` of the running source checkout, with a ``-dirty`` suffix
     when the tree has uncommitted changes (bare-metal / from-source runs).
  3. ``"dev"`` fallback.

NOTE: a *Docker* self-build can't use step 2 — the runtime image strips ``.git``
(audit F-07/F-08) and ships no ``git`` binary, so the fallback no-ops there.
Docker self-builders should instead pass
``--build-arg MINOTAUR_IMAGE_SHA=$(git rev-parse HEAD)``.
"""

from __future__ import annotations

import os
import subprocess

# version.py lives at <repo>/minotaur_subnet/version.py → repo root is two up.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_version() -> str:
    """Best-available build version string for /health (see module docstring)."""
    sha = os.environ.get("MINOTAUR_IMAGE_SHA", "").strip()
    if sha and sha != "dev":
        return sha[:7]
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
        if rev:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=_REPO_ROOT, capture_output=True, text=True, timeout=3, check=False,
            ).stdout.strip()
            return rev + ("-dirty" if dirty else "")
    except Exception:
        # No git, no .git (Docker image), or git too slow — fall through.
        pass
    return "dev"
