"""Structural fingerprint — "same logic, different salt" identity for sybil dedup.

The content fingerprint (``code_fingerprint.py``) hashes what the code MEANS,
including identifiers AND constant VALUES — so it is defeated by a single
salted constant (the ``james_base.py``/``king_base.py`` "build-salt
hash-buster" observed live: three coldkeys shipping structurally-identical
solvers — identical ``max_region_nodes``, identical ``unproductive_nodes``,
identical top-offender functions and node counts — that differ ONLY in one
salted constant, thereby minting three distinct content fingerprints and
capturing all three slate slots).

This fingerprint hashes the code's STRUCTURE, invariant to that salt:

  * ``.py`` files parse to AST; docstrings are stripped and every constant
    VALUE is replaced by a per-type tag (all strings → one tag, all ints →
    one tag, …). Identifiers, attribute names, call targets, control flow
    and node structure all STILL count. So a salted build-constant collapses
    into the same identity, while a real logic change — a different call, a
    reordered branch, an added strategy — diverges it.
  * Non-``.py`` files are ignored: this is a fingerprint of solver LOGIC, not
    of data tables (a nonce hidden in a ``.json`` must not mint a new
    structural identity).
  * Paths are hashed (sorted, relative) — moving logic between files is a
    structural change.
  * A ``.py`` that does not parse falls back to raw bytes (identity must
    never crash on miner input; the analyzability gate handles unparseable
    code separately).

WHY THIS DOESN'T PUNISH FORK-AND-IMPROVE. The subnet WANTS organic
fork-and-improve, so high overlap with the champion is healthy. This never
looks at overlap — it collapses only NEAR-IDENTICAL structure across
DISTINCT actors. A legit improvement changes the productive structure (that
is what "improve" means) → a distinct fingerprint → its own slot. Only a
free-riding salted COPY collapses.

SCOPE / ARMS RACE (honest). v1 keeps identifiers, so it catches the observed
salt (a constant) but is evadable by ALSO renaming identifiers per copy. That
is a deliberate conservative first cut: keeping identifiers minimises false
positives (two legit solvers with different names stay distinct), and the
observe-only phase will reveal if rename-salting appears — a cluster we
expect to catch but don't — at which point we escalate normalization
(identifier canonicalisation, and/or restricting the hash to the PRODUCTIVE
subgraph the deadwood pass already identifies, to defeat per-copy padding).
Each escalation raises the cost of slate capture from "flip one byte" toward
"meaningfully diversify N solvers", which — with per-coldkey min_burn — is
defence in depth, not a wall.

DETERMINISM (consensus). Computed ONCE at screening stage 1 and persisted;
consumers read the stored value and never recompute, so a cross-CPython
``ast.dump`` difference cannot split a slate. Bump the version on ANY change
to the algorithm below so identities never collide across rule changes.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def structural_dedup_mode() -> str:
    """How the structural dedup ACTS on a slate — an explicit env lever so
    this stays a KNOWN, discoverable feature rather than silent always-on
    behaviour.

    ``STRUCTURAL_DEDUP_MODE``:
      * ``off`` (DEFAULT) — do nothing. The structural fingerprint is still
        computed + persisted at screening (a passive metric, like
        max_region_nodes), but rotation neither logs nor collapses. Set this
        explicitly in the leader env so the lever is visible.
      * ``observe`` — Phase 0: LOG cross-actor structural clusters at slate
        selection; selection unchanged.
      * ``reject`` — one queue seat per (OPERATOR, structure): a submission
        whose structural fingerprint matches a LIVE submission from the SAME
        operator is rejected at intake, pre-build, with the duplicate named
        (screening_pipeline). Cross-operator matches are deliberately out of
        scope here — includes ``observe`` logging.
      * ``enforce`` — DEPRECATED, do not arm. Collapses a CROSS-ACTOR
        structural cluster to one slate slot + build-budget preference for
        novel fingerprints. Measured 2026-07-29: the fingerprint cannot
        separate a copy from an improvement (a champion fork with a real
        -123-node factorization win kept the champion's exact fingerprint),
        so population-keyed enforcement starved fork-and-improve — ~30-member
        champion-lineage clusters sat 22-28 waitlisted while novel-fp
        mutations were built and seated; flipping to ``observe`` produced
        perfect-tie verdicts within one round and a dethrone in 2.5h. The
        cross-actor fleet threat belongs to OPERATOR identity (actor
        owner-union merges), not code shape. Kept only so an already-set env
        keeps its (known-bad) meaning rather than silently becoming ``off``.

    Unknown values fall back to ``off`` (fail safe — an env typo must not
    silently enable a slate-changing feature).
    """
    v = os.environ.get("STRUCTURAL_DEDUP_MODE", "").strip().lower()
    return v if v in ("off", "observe", "reject", "enforce") else "off"

# Bump on ANY change to the normalization below (see module docstring).
STRUCTURAL_FINGERPRINT_VERSION = 1

# Transport artifacts, not logic (identical to the content fingerprint's set).
_EXCLUDED_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}

_MAX_FILE_BYTES = 100 * 1024 * 1024

# Per-type constant tags. All values of a type collapse to one tag, so a
# salted build-constant is indistinguishable from any other constant of its
# type — while the PRESENCE and TYPE of a constant (str vs int vs bytes) stays
# in the structure.
_CONST_TAG = {
    str: "\x00S", bytes: "\x00B", int: "\x00I", float: "\x00F",
    complex: "\x00C", bool: "\x00b", type(None): "\x00N",
}


class _StructuralNormalizer(ast.NodeTransformer):
    """Strip docstrings and replace every constant VALUE with a per-type tag.

    Identifiers, attributes, call targets and control-flow structure are left
    intact — only the salt-carrying constant values are erased.
    """

    def _strip_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list) and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]

    def visit_Module(self, node: ast.Module) -> ast.AST:  # noqa: N802
        self._strip_docstring(node)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:  # noqa: N802
        self._strip_docstring(node)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        self._strip_docstring(node)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:  # noqa: N802
        self._strip_docstring(node)
        self.generic_visit(node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        # type()-keyed (not isinstance) so bool — a subclass of int — keeps its
        # own tag instead of folding into the int bucket. Unknown types get a
        # generic tag; the PRESENCE of a constant still counts.
        node.value = _CONST_TAG.get(type(node.value), "\x00?")
        node.kind = None
        return node


def structural_python_bytes(source: str) -> bytes:
    """Canonical structural payload for one Python source: docstring-stripped,
    constant-erased ``ast.dump`` (no attributes ⇒ no line/col), or raw bytes
    when the source does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return source.encode("utf-8", errors="surrogateescape")
    tree = _StructuralNormalizer().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False).encode()


def repo_structural_fingerprint(repo_path: str) -> str | None:
    """Structural fingerprint for a cloned submission tree (``.py`` only).

    ``None`` only when the path itself is unusable — identity is best-effort
    and screening carries on.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return None
    h = hashlib.sha256()
    h.update(f"v{STRUCTURAL_FINGERPRINT_VERSION}|struct|".encode())
    try:
        files = sorted(
            p for p in root.rglob("*.py")
            if p.is_file() and not p.is_symlink()
            and not (set(p.relative_to(root).parts[:-1]) & _EXCLUDED_DIRS)
        )
        if not files:
            return None
        for p in files:
            rel = p.relative_to(root).as_posix()
            h.update(f"|{rel}|".encode())
            raw = p.read_bytes()[:_MAX_FILE_BYTES]
            try:
                h.update(structural_python_bytes(raw.decode("utf-8")))
            except UnicodeDecodeError:
                h.update(raw)
    except OSError:
        logger.warning("structural fingerprint failed for %s", repo_path, exc_info=True)
        return None
    return h.hexdigest()
