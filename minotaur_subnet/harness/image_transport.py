"""Content-addressed champion image transport.

The champion image is identified, certified, and run by its GHCR **manifest
digest** so every validator pulls byte-identical bytes — this is the only champion
identity (the old per-host ``{{.Id}}`` identity + local-sha256 compare is retired).

There is deliberately **no per-validator mode env**: that would split the fleet
because third-party validators never update their envs. Instead the behavior is
**proposal-driven** — a follower branches on the *shape* of the ``candidate_image_id``
the leader proposed (``is_digest_ref`` → pull-by-digest + verify; else legacy),
which ships uniformly to everyone as code. Only the **leader** decides to build,
push, and propose a digest, gated on ``CANDIDATE_IMAGE_REPO`` being configured on
that one node (leader-local, not a consensus toggle).

Three reference shapes this module translates between:

  - local image id:   ``sha256:<64hex>``         (docker ``{{.Id}}``, per-host, NOT portable)
  - bare digest:      ``<64hex>``                (on-chain ``candidateImageId`` encoding)
  - pullable ref:     ``<repo>@sha256:<64hex>``  (GHCR manifest digest, portable)

Nothing here reads or runs Docker — these are pure functions so the ref parsing
is auditable in isolation.
"""

from __future__ import annotations

import os
import re

# A 64-char lowercase hex sha256 digest, and a full ``<repo>@sha256:<hex>`` ref.
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO_DIGEST = re.compile(r"^(?P<repo>.+)@sha256:(?P<hex>[0-9a-f]{64})$")

DEFAULT_CANDIDATE_REPO = "ghcr.io/subnet112/minotaur-solver-candidates"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def candidate_repo() -> str:
    """GHCR repo the LEADER pushes candidate images to (leader-local config).

    Followers never read this — they pull the full ``<repo>@sha256:D`` ref carried
    in the champion proposal, so the repo travels with the digest.
    """
    return _env("CANDIDATE_IMAGE_REPO") or DEFAULT_CANDIDATE_REPO


def leader_pushes_digests() -> bool:
    """True when THIS node is configured to build+push candidate images.

    Leader-local capability gate (not a fleet-wide consensus toggle): the leader
    proposes digests when it has a candidate repo configured; everything else is
    driven by the shape of what gets proposed.
    """
    return bool(_env("CANDIDATE_IMAGE_REPO"))


def bare_hex(ref: str | None) -> str | None:
    """Extract the bare 64-char hex sha256 from any image-reference shape.

    Accepts ``<repo>@sha256:<hex>``, ``sha256:<hex>``, or a bare ``<hex>``.
    Returns the lowercased 64-hex, or ``None`` if no valid digest is present.
    This is the value that must go on-chain in ``candidateImageId`` — the
    ``sha256:``-prefixed string is 70 chars and would be keccak-hashed instead of
    decoded, so the on-chain field would NOT equal the real digest.
    """
    if not ref:
        return None
    s = ref.strip()
    m = _REPO_DIGEST.match(s)
    if m:
        return m.group("hex")
    if s.lower().startswith("sha256:"):
        s = s[len("sha256:"):]
    s = s.lower()
    return s if _HEX64.match(s) else None


def is_digest_ref(s: str | None) -> bool:
    """True if *s* is a pullable digest ref ``<repo>@sha256:<64hex>``."""
    return bool(s and _REPO_DIGEST.match(s.strip()))


def parse_repo_digest(s: str | None) -> tuple[str, str] | None:
    """Split ``<repo>@sha256:<64hex>`` into ``(repo, bare_hex)``; else ``None``."""
    if not s:
        return None
    m = _REPO_DIGEST.match(s.strip())
    return (m.group("repo"), m.group("hex")) if m else None


def make_digest_ref(repo: str, hex_or_ref: str | None) -> str | None:
    """Build a pullable ``<repo>@sha256:<64hex>`` from a repo + any digest shape."""
    h = bare_hex(hex_or_ref)
    return f"{repo}@sha256:{h}" if (h and repo) else None
