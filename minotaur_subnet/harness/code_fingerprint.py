"""Normalized content fingerprint — "same code, same quota" that survives nonce spam.

The per-(hotkey, commit) resubmit cap keys on the GIT SHA, which changes when
any byte changes. Live 2026-07-07: two consecutive champion dethrones differed
ONLY by a ``# putty-nonce <timestamp>`` comment — a fresh SHA per round to dodge
the cap while resubmitting identical code, across ~13 hotkeys every round.

This module computes a fingerprint over what the code MEANS rather than its
bytes, so cosmetic rotation collapses back into one identity:

  * ``.py`` files parse to AST and hash the canonical ``ast.dump`` with
    docstrings stripped — comments, whitespace, formatting, line numbers and
    docstrings all vanish. Any SEMANTIC byte (identifiers, string constants —
    replay calldata lives in strings — numbers, structure) still counts.
  * Every other file (the replay ``.json`` tables, configs, Dockerfile …)
    hashes raw — data edits are real content changes and deserve a new
    identity.
  * A ``.py`` that does not parse falls back to raw bytes (stage 2's import
    check is the correctness backstop; identity must never crash).
  * Paths are hashed too (sorted, relative, ``/``-separated) — moving logic
    between files is a structural change.

Honest scope note (same as the commit cap's): a determined adversary can still
rotate by flipping one semantic byte (e.g. a version string constant). This
kills the FREE rotation — comments and reformatting — and, keyed fleet-wide
(across hotkeys) by the store's counter, it also collapses N sybil hotkeys
shipping byte-identical trees into ONE quota bucket, which the per-hotkey
commit cap structurally cannot do.

Like ``max_region_nodes``, the value is computed ONCE at screening stage 1 and
persisted; consumers read the stored value and never recompute, so a
cross-CPython ``ast.dump`` difference cannot split anything.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Mixed into every hash so a future normalization change (new exclusions, new
# AST canonicalization) can never collide with identities minted under the old
# rules — bump on ANY change to the algorithm below.
FINGERPRINT_VERSION = 1

# Directories that are transport artifacts, not content. ``.git`` alone made
# every clone of identical code hash differently (pack names embed SHAs).
_EXCLUDED_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}

# Per-file byte cap folded into the hash streaming read — matches the stage-1
# repo-size policy scale; a >100MB file only contributes its first 100MB, which
# is fine for identity purposes (stage 1 rejects such repos anyway).
_MAX_FILE_BYTES = 100 * 1024 * 1024


class _StripDocstrings(ast.NodeTransformer):
    """Remove the leading docstring Expr from module/class/function bodies.

    Docstrings are documentation: editing one must not mint a new identity.
    All OTHER string constants are semantics (calldata hex, addresses, ABI
    payloads live in strings on this subnet) and stay in the hash.
    """

    def _strip(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list) and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            # An emptied body needs a placeholder to stay valid AST.
            node.body = body[1:] or [ast.Pass()]
        self.generic_visit(node)
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:  # noqa: N802
        return self._strip(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:  # noqa: N802
        return self._strip(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        return self._strip(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:  # noqa: N802
        return self._strip(node)


def normalized_python_bytes(source: str) -> bytes:
    """The canonical hash payload for one Python source: docstring-stripped
    ``ast.dump`` (no attributes ⇒ no line/col numbers), or the raw bytes when
    the source does not parse (identity must never crash on miner input)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return source.encode("utf-8", errors="surrogateescape")
    tree = _StripDocstrings().visit(tree)
    return ast.dump(tree, include_attributes=False).encode()


def source_fingerprint(source: str) -> str:
    """Fingerprint for a SINGLE solver source string — the one-file variant
    of :func:`repo_fingerprint` (tests / tooling; the inline ``/submissions/
    source`` endpoint that consumed it in production was removed as unused)."""
    h = hashlib.sha256()
    h.update(f"v{FINGERPRINT_VERSION}|inline|".encode())
    h.update(normalized_python_bytes(source))
    return h.hexdigest()


def repo_fingerprint(repo_path: str) -> str | None:
    """Fingerprint for a cloned submission tree. ``None`` only when the path
    itself is unusable (identity is best-effort; screening carries on)."""
    root = Path(repo_path)
    if not root.is_dir():
        return None
    h = hashlib.sha256()
    h.update(f"v{FINGERPRINT_VERSION}|tree|".encode())
    try:
        files = sorted(
            p for p in root.rglob("*")
            if p.is_file() and not p.is_symlink()
            and not (set(p.relative_to(root).parts[:-1]) & _EXCLUDED_DIRS)
        )
        for p in files:
            rel = p.relative_to(root).as_posix()
            h.update(f"|{rel}|".encode())
            raw = p.read_bytes()[:_MAX_FILE_BYTES]
            if p.suffix == ".py":
                try:
                    h.update(normalized_python_bytes(raw.decode("utf-8")))
                    continue
                except UnicodeDecodeError:
                    pass  # non-UTF8 "python" — raw bytes below
            h.update(raw)
    except OSError:
        logger.warning("content fingerprint failed for %s", repo_path, exc_info=True)
        return None
    return h.hexdigest()
