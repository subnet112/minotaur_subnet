"""Content-addressed champion image transport (P1).

Single source of truth for the ``CHAMPION_IMAGE_TRANSPORT`` feature flag and the
small pure helpers that translate between the three image-reference shapes:

  - local image id:   ``sha256:<64hex>``         (docker ``{{.Id}}``, per-host, NOT portable)
  - bare digest:      ``<64hex>``                (on-chain ``candidateImageId`` encoding)
  - pullable ref:     ``<repo>@sha256:<64hex>``  (GHCR manifest digest, portable)

Modes (env ``CHAMPION_IMAGE_TRANSPORT``):
  - ``local`` (default): today's behavior — build locally, identify by ``{{.Id}}``,
    no registry push/pull. Fully inert.
  - ``digest``: build once, push to the candidate repo, certify/run by the GHCR
    manifest digest so every validator pulls byte-identical bytes.

``CHAMPION_IMAGE_TRANSPORT_STRICT`` (default off) controls degradation in
``digest`` mode: off → log loud and fall back to local semantics on registry
failure; on → fail the operation (used on testnet to prove the path works).

Nothing here reads or runs Docker — these are pure functions so the flag and the
ref parsing are auditable in isolation. Callers branch on ``digest_mode()``.
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


def transport_mode() -> str:
    """Champion image transport mode: ``"local"`` (default) or ``"digest"``."""
    return "digest" if _env("CHAMPION_IMAGE_TRANSPORT").lower() == "digest" else "local"


def digest_mode() -> bool:
    """True when content-addressed (digest) transport is enabled."""
    return transport_mode() == "digest"


def transport_strict() -> bool:
    """True when digest-mode failures must fail loudly instead of falling back."""
    return _env("CHAMPION_IMAGE_TRANSPORT_STRICT").lower() in ("1", "true", "yes", "on")


def candidate_repo() -> str:
    """GHCR repo candidate images are pushed to / pulled from in digest mode."""
    return _env("CANDIDATE_IMAGE_REPO") or DEFAULT_CANDIDATE_REPO


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
