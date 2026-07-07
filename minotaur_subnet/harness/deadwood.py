"""Deadwood metric (``unproductive_nodes``) — Phase 0, OBSERVE-ONLY.

Measures the AST-node mass of a submission that provably does no work at
runtime. The live champion carries ~16.6% dead mass (superseded-generation
files kept alive as never-taken import fallbacks, plus never-referenced defs
inside reachable files); ``max_region_nodes`` cannot see it because that
metric looks at the *shape* of live code, not at whether code is live at all.

The metric is a deterministic pure function of the submitted tree (stdlib
``ast`` + ``pathlib`` only, pinned screening-container CPython — the same
interpreter that computes ``max_region_nodes``):

    unproductive_nodes = TierA + TierB + ExemptExcess

  Tier A   — all AST nodes of non-exempt ``*.py`` files OUTSIDE the
             import-reachability closure rooted at ``solver.py``. Imports
             lexically inside an ``except`` handler and imports under a
             statically-false test are NON-edges (a dead fallback lineage
             cannot keep itself reachable). Dynamic imports resolve only via
             string-literal arguments, plus the narrow autoloader-dir rule
             for ``spec_from_file_location``/``exec_module`` call sites
             paired with a directory literal.
  Tier B   — inside reachable files, the node mass of *maximal dead
             subtrees* under a seed-based liveness fixpoint. Seeds are
             ``solver.py``'s module-level roots plus ``HARNESS_SURFACE``
             class-body methods on live classes. References propagate only
             from live regions; imports confer edges but never references;
             string literals count only at dispatch-shaped call sites;
             decorators never enliven the decorated def; annotations confer
             no references; write-only module/class assigns and
             statically-false blocks count dead.
  Overflow — the exempt tree (``tests/**``, ``.github/**``, ``conftest.py``)
             above ``TEST_TREE_NODE_BUDGET`` nodes. Quarantining dead code
             into tests/ is an accepted, bounded residual.

Deleting dead code is the only move that lowers the number: minifying,
inlining, or golfing LIVE code moves it by exactly zero, because live nodes
never enter the count and there is no denominator.

Phase 0 computes, logs, and persists the value — it gates NOTHING. The floor
(``UNPRODUCTIVE_NODES_MAX``) and the saturated-tie margin arrive in later,
separately-reviewed PRs after the live distribution has soaked.

Determinism: sorted iteration everywhere, case-sensitive repo-root-relative
module resolution, symlinks not followed, and a monotone worklist fixpoint
(facts only ever get added, so the result is iteration-order independent).
"""

from __future__ import annotations

import ast
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump on ANY semantic change to this module's algorithm. Consumers must only
# ever compare values carrying EQUAL versions (the #504 skew class).
UNPRODUCTIVE_METRIC_VERSION = 1

# Exempt-tree allowance: nodes in EXEMPT_GLOBS files below this budget
# contribute 0; the overflow above it counts into unproductive_nodes. Bounds
# the "git mv the attic into tests/" quarantine dodge.
TEST_TREE_NODE_BUDGET = 8000

# Repo-root-relative glob patterns exempt from Tier A/B (dev-productive trees).
# Supported shapes: "dir/**" (root-anchored subtree), "**/name" (basename at
# any depth), exact path.
EXEMPT_GLOBS = ("tests/**", ".github/**", "**/conftest.py")

# Class-body method names invoked BY the harness / SDK by name, without any
# in-tree reference — they are liveness seeds on live classes. Vetted against
# the callers:
#   initialize / metadata / generate_plan / check_trigger /
#   on_benchmark_start / on_benchmark_end / serialize_state / restore_state /
#   quote            — sdk/intent_solver.py surface, driven by
#                      harness/{runner,orchestrator,runtime_solver}.py
#   accepts / rpc_for — sdk/strategy.py Strategy interface, invoked by
#                      sdk/routing_solver.py (SDK-side dispatch)
# Version-bump UNPRODUCTIVE_METRIC_VERSION if the SDK surface changes.
HARNESS_SURFACE = (
    "initialize",
    "metadata",
    "generate_plan",
    "check_trigger",
    "on_benchmark_start",
    "on_benchmark_end",
    "serialize_state",
    "restore_state",
    "quote",
    "accepts",
    "rpc_for",
)

# Future Phase-1 floor (reject when unproductive_nodes exceeds it). None ⇒
# disarmed. Phase 0 NEVER reads this for gating — it exists so the report can
# truthfully say observe_only and so arming is a one-line, code-reviewed diff.
UNPRODUCTIVE_NODES_MAX: int | None = None

# Cap on the persisted offender list (what miners see they should delete).
MAX_TOP_OFFENDERS = 20

_EXCLUDE_DIRS = {".git"}

# Exception names that make a module-level try an "import guard": its body's
# imports still confer edges (the live champ_top pattern) but no OTHER
# statement in the body or handlers confers reference roots — kills the
# 2-line try-import + keep-alive resurrection of a dead fallback lineage.
_GUARD_EXCEPTIONS = {"ImportError", "ModuleNotFoundError", "Exception"}

_ENTRY_MODULE = "solver.py"

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Region-processing modes.
_LIVE = "live"              # refs + edges + bindings
_REGISTER = "register"      # bindings only (text-read modules, guard handlers)
_REGISTER_EDGES = "regedge"  # bindings + import edges, NO refs (guard bodies)


@dataclass
class DeadwoodResult:
    """Outcome of one analyzer run over a submission tree."""

    # Total dead mass. None ⇔ a non-exempt file failed ast.parse (Phase 0
    # None-semantics: the caller persists unproductive_nodes=None and every
    # consumer skips the value).
    unproductive_nodes: int | None
    # (repo-relative path, qualname-or-None, nodes) — max 20, sorted desc by
    # nodes then path. qualname None ⇒ the whole file (Tier A) or a synthetic
    # bucket; otherwise the dead binding / block inside a reachable file.
    top_offenders: list[tuple[str, str | None, int]] = field(default_factory=list)
    # True ⇔ a non-exempt file failed ast.parse (see unproductive_nodes).
    unparseable: bool = False
    version: int = UNPRODUCTIVE_METRIC_VERSION


# ═══════════════════════════════════════════════════════════════════════════
#                         SMALL PURE HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _count_nodes(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _is_exempt(rel: str) -> bool:
    """Match a repo-relative posix path against EXEMPT_GLOBS."""
    base = rel.rsplit("/", 1)[-1]
    for pat in EXEMPT_GLOBS:
        if pat.startswith("**/"):
            if base == pat[3:]:
                return True
        elif pat.endswith("/**"):
            prefix = pat[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
        elif rel == pat:
            return True
    return False


def _statically_false(test: ast.expr) -> bool:
    """True for `if False:` / `if 0:` / `if TYPE_CHECKING:` and any test
    ast.literal_eval evaluates falsy. Anything unknown is NOT statically false."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    try:
        return not ast.literal_eval(test)
    except Exception:
        return False
    # NOTE: `if not TYPE_CHECKING:` etc. are deliberately NOT handled — only
    # obviously-never-true guards are non-edges/dead; anything else is live.


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if t is None:
        return ["<bare>"]
    out: list[str] = []
    for node in ([t] if not isinstance(t, ast.Tuple) else list(t.elts)):
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
    return out


def _qual(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


# ═══════════════════════════════════════════════════════════════════════════
#                         BINDING / FIXPOINT ENGINE
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _Binding:
    """One def/class binding or one module/class-level assign target."""

    rel: str                       # repo-relative file
    name: str                      # bare binding name
    qualname: str
    node: ast.AST                  # FunctionDef/AsyncFunctionDef/ClassDef/Assign/AnnAssign
    kind: str                      # "def" | "class" | "assign"
    live: bool = False
    # Deferred expressions (assign RHS etc.) walked only once the binding is
    # live — a write-only table's references must not propagate.
    deferred: list[ast.expr] = field(default_factory=list)
    parent_class: "_Binding | None" = None


class _Analyzer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.trees: dict[str, ast.Module] = {}       # non-exempt rel -> AST
        self.node_counts: dict[str, int] = {}
        self.mod_by_name: dict[str, str] = {}        # dotted module -> rel
        self.exempt_nodes = 0
        self.unparseable_files: list[str] = []

        self.reached: set[str] = set()
        self.text_read: set[str] = set()
        # (rel, mode) region-processing dedup for module tops.
        self._module_processed: dict[str, str] = {}

        self.referenced: set[str] = set()
        self.bindings_by_name: dict[str, list[_Binding]] = {}
        self.bindings_by_node: dict[int, list[_Binding]] = {}
        self._walked_assigns: set[int] = set()
        # from-import alias map: (rel, local alias) -> origin name.
        self.alias_origin: dict[tuple[str, str], str] = {}

        # Autoloader facts, collected only from LIVE walks (liveness-gated).
        self.autoload_callsite: set[str] = set()          # rel with callsite
        self.autoload_dirs: dict[str, set[str]] = {}      # rel -> dir rels
        self.autoload_pynames: dict[str, set[str]] = {}   # rel -> basenames
        self._autoload_admitted: set[tuple[str, str]] = set()
        self.autoload_roots: set[str] = set()             # files whose top classes seed

        self.unresolved_dynamic: list[str] = []

        self._queue: deque = deque()

    # ── file discovery + parse ────────────────────────────────────────────

    def scan(self) -> bool:
        """Parse the tree. Returns False when a NON-exempt file is unparseable."""
        for path in self._iter_py_files():
            rel = path.relative_to(self.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (SyntaxError, ValueError, OSError) as exc:
                if _is_exempt(rel):
                    continue  # unparseable exempt file contributes 0
                self.unparseable_files.append(rel)
                logger.warning("[deadwood] unparseable non-exempt file %s: %s", rel, exc)
                continue
            if _is_exempt(rel):
                self.exempt_nodes += _count_nodes(tree)
                continue
            self.trees[rel] = tree
            self.node_counts[rel] = _count_nodes(tree)
            self.mod_by_name[self._module_name(rel)] = rel
        return not self.unparseable_files

    def _iter_py_files(self) -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE_DIRS)
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                p = Path(dirpath) / fn
                if p.is_symlink():
                    continue
                out.append(p)
        return out

    @staticmethod
    def _module_name(rel: str) -> str:
        parts = rel[:-3].split("/")  # strip ".py"
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    # ── fixpoint driver ───────────────────────────────────────────────────

    def run(self) -> None:
        if _ENTRY_MODULE in self.trees:
            self._pend(self._reach, _ENTRY_MODULE)
        self._drain()

    def _pend(self, fn, *args) -> None:
        self._queue.append((fn, args))

    def _drain(self) -> None:
        while self._queue:
            fn, args = self._queue.popleft()
            fn(*args)

    # ── reach / text-read / enliven ───────────────────────────────────────

    def _reach(self, rel: str) -> None:
        if rel in self.reached or rel not in self.trees:
            return
        self.reached.add(rel)
        self._process_module(rel, _LIVE)
        if rel == _ENTRY_MODULE:
            # Seeds: solver.py's module-level roots are all live.
            for blist in self.bindings_by_node.values():
                for b in blist:
                    if b.rel == _ENTRY_MODULE and "." not in b.qualname:
                        self._pend(self._enliven, b)
        if rel in self.autoload_roots:
            self._seed_top_classes(rel)

    def _mark_text_read(self, rel: str) -> None:
        if rel in self.text_read:
            return
        self.text_read.add(rel)
        if rel not in self.reached:
            # Tier-A-exempt but NOT liveness-seeded: register bindings only so
            # Tier B can count its dead defs; no edges, no reference roots.
            self._process_module(rel, _REGISTER)

    def _process_module(self, rel: str, mode: str) -> None:
        prev = self._module_processed.get(rel)
        if prev == _LIVE or prev == mode:
            return
        self._module_processed[rel] = mode
        self._process_stmts(rel, list(self.trees[rel].body), "", "module", mode, False)

    def _enliven(self, b: _Binding) -> None:
        if b.live:
            return
        b.live = True
        if b.kind in ("def", "class"):
            child_kind = "class" if b.kind == "class" else "function"
            self._process_stmts(
                b.rel, list(b.node.body), b.qualname, child_kind, _LIVE, False,
            )
        elif b.kind == "assign":
            if id(b.node) not in self._walked_assigns:
                self._walked_assigns.add(id(b.node))
                for expr in b.deferred:
                    self._walk_refs(b.rel, expr)

    def _seed_top_classes(self, rel: str) -> None:
        tree = self.trees.get(rel)
        if tree is None:
            return
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                for b in self.bindings_by_node.get(id(stmt), []):
                    self._pend(self._enliven, b)

    # ── binding registration + name references ───────────────────────────

    def _register(
        self,
        rel: str,
        name: str,
        qualname: str,
        node: ast.AST,
        kind: str,
        parent_class: _Binding | None,
        deferred: list[ast.expr] | None = None,
    ) -> _Binding:
        for b in self.bindings_by_node.get(id(node), []):
            if b.name == name:
                return b
        b = _Binding(
            rel=rel, name=name, qualname=qualname, node=node, kind=kind,
            deferred=deferred or [], parent_class=parent_class,
        )
        self.bindings_by_name.setdefault(name, []).append(b)
        self.bindings_by_node.setdefault(id(node), []).append(b)
        if name in self.referenced:
            self._pend(self._enliven, b)
        # A LIVE class enlivens its dunders and HARNESS_SURFACE methods (the
        # harness/SDK calls them by name; no in-tree reference exists).
        if (
            kind == "def"
            and parent_class is not None
            and parent_class.live
            and (name in HARNESS_SURFACE or (name.startswith("__") and name.endswith("__")))
        ):
            self._pend(self._enliven, b)
        return b

    def _add_ref(self, rel: str, name: str) -> None:
        origin = self.alias_origin.get((rel, name))
        if origin is not None:
            self._add_ref_name(origin)
        self._add_ref_name(name)

    def _add_ref_name(self, name: str) -> None:
        if name in self.referenced:
            return
        self.referenced.add(name)
        for b in self.bindings_by_name.get(name, []):
            self._pend(self._enliven, b)

    # ── import edges ──────────────────────────────────────────────────────

    def _edge_dotted(self, dotted: str) -> None:
        """Edge to an in-repo module by dotted name, crediting parent __init__s."""
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            rel = self.mod_by_name.get(".".join(parts[:i]))
            if rel is not None:
                self._pend(self._reach, rel)

    def _package_parts(self, rel: str) -> list[str]:
        return rel.rsplit("/", 1)[0].split("/") if "/" in rel else []

    def _resolve_import_from(self, rel: str, node: ast.ImportFrom) -> list[str]:
        """Dotted target modules for an ImportFrom (module + imported submodules)."""
        if node.level == 0:
            base_parts = node.module.split(".") if node.module else []
        else:
            pkg = self._package_parts(rel)
            cut = node.level - 1
            if cut > len(pkg):
                return []
            base = pkg[: len(pkg) - cut] if cut else pkg
            base_parts = base + (node.module.split(".") if node.module else [])
        out: list[str] = []
        if base_parts:
            out.append(".".join(base_parts))
        # from-pkg-import-submodule: each name may itself be a module.
        for alias in node.names:
            if alias.name == "*":
                continue
            cand = ".".join(base_parts + [alias.name]) if base_parts else alias.name
            if cand in self.mod_by_name:
                out.append(cand)
        return out

    def _handle_import(self, rel: str, stmt: ast.stmt, edges: bool) -> None:
        """Record aliases; confer edges when allowed. Imports are NEVER references."""
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if edges:
                    self._edge_dotted(alias.name)
        elif isinstance(stmt, ast.ImportFrom):
            targets = self._resolve_import_from(rel, stmt)
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                # Origin binding goes LIVE iff the local alias is referenced
                # from a live region — the import itself is never a reference.
                self.alias_origin.setdefault((rel, local), alias.name)
            if edges:
                for dotted in targets:
                    self._edge_dotted(dotted)

    def _try_is_import_guard(self, rel: str, node: ast.Try) -> bool:
        """Module-level try whose body imports an in-repo module and whose
        handlers catch ImportError/ModuleNotFoundError/Exception/bare."""
        catches = any(
            any(n in _GUARD_EXCEPTIONS or n == "<bare>" for n in _handler_names(h))
            for h in node.handlers
        )
        if not catches:
            return False
        for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(stmt, ast.Import):
                if any(
                    ".".join(a.name.split(".")[: i + 1]) in self.mod_by_name
                    for a in stmt.names
                    for i in range(len(a.name.split(".")))
                ):
                    return True
            elif isinstance(stmt, ast.ImportFrom):
                if self._resolve_import_from(rel, stmt):
                    return True
        return False

    # ── statement/region processing ───────────────────────────────────────

    def _process_stmts(
        self,
        rel: str,
        stmts: list[ast.stmt],
        qual: str,
        kind: str,          # "module" | "class" | "function"
        mode: str,          # _LIVE | _REGISTER | _REGISTER_EDGES
        in_handler: bool,   # lexically inside an except handler ⇒ imports non-edge
    ) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                edges = mode in (_LIVE, _REGISTER_EDGES) and not in_handler
                self._handle_import(rel, stmt, edges)

            elif isinstance(stmt, _SCOPE_NODES):
                is_class = isinstance(stmt, ast.ClassDef)
                parent = None
                if kind == "class":
                    parent = self._enclosing_class_binding(rel, qual)
                # Body processing is deferred until the binding goes live.
                self._register(
                    rel, stmt.name, _qual(qual, stmt.name), stmt,
                    "class" if is_class else "def", parent,
                )
                if mode == _LIVE:
                    # Header expressions execute at definition time in a live
                    # region: decorator names, base-class names, defaults.
                    # Decoration/inheritance never enlivens STMT itself.
                    for dec in stmt.decorator_list:
                        self._walk_refs(rel, dec)
                    if is_class:
                        for base in stmt.bases:
                            self._walk_refs(rel, base)
                        for kw in stmt.keywords:
                            self._walk_refs(rel, kw.value)
                    else:
                        args = stmt.args
                        for d in list(args.defaults) + [
                            d for d in args.kw_defaults if d is not None
                        ]:
                            self._walk_refs(rel, d)
                        # Annotations (args/returns) confer NO references.

            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self._process_assign(rel, stmt, qual, kind, mode)

            elif isinstance(stmt, ast.AugAssign):
                if mode == _LIVE:
                    self._walk_refs(rel, stmt.target)
                    self._walk_refs(rel, stmt.value)

            elif isinstance(stmt, ast.If):
                if _statically_false(stmt.test):
                    # Dead block: nothing inside confers edges, refs, bindings.
                    self._process_stmts(rel, stmt.orelse, qual, kind, mode, in_handler)
                else:
                    if mode == _LIVE:
                        self._walk_refs(rel, stmt.test)
                    self._process_stmts(rel, stmt.body, qual, kind, mode, in_handler)
                    self._process_stmts(rel, stmt.orelse, qual, kind, mode, in_handler)

            elif isinstance(stmt, ast.Try):
                if (
                    kind == "module"
                    and mode == _LIVE
                    and self._try_is_import_guard(rel, stmt)
                ):
                    # Import-guard: body imports confer edges; nothing else in
                    # body/handlers confers reference roots (kills the
                    # try-import + keep-alive resurrection).
                    self._process_stmts(rel, stmt.body, qual, kind, _REGISTER_EDGES, in_handler)
                    for h in stmt.handlers:
                        self._process_stmts(rel, h.body, qual, kind, _REGISTER, True)
                else:
                    self._process_stmts(rel, stmt.body, qual, kind, mode, in_handler)
                    for h in stmt.handlers:
                        if mode == _LIVE and h.type is not None:
                            self._walk_refs(rel, h.type)
                        # Except-handler imports are NON-edges, always.
                        self._process_stmts(rel, h.body, qual, kind, mode, True)
                self._process_stmts(rel, stmt.orelse, qual, kind, mode, in_handler)
                self._process_stmts(rel, stmt.finalbody, qual, kind, mode, in_handler)

            elif isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
                continue

            else:
                # Generic statement (Expr/Return/For/While/With/Match/...):
                # recurse into nested statement lists (same region), walk
                # expressions for references in live mode.
                for _fname, value in ast.iter_fields(stmt):
                    if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                        self._process_stmts(rel, value, qual, kind, mode, in_handler)
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, ast.withitem):
                                if mode == _LIVE:
                                    self._walk_refs(rel, item.context_expr)
                                    if item.optional_vars is not None:
                                        self._walk_refs(rel, item.optional_vars)
                            elif isinstance(item, ast.expr) and mode == _LIVE:
                                self._walk_refs(rel, item)
                            elif isinstance(item, ast.match_case):
                                self._process_stmts(rel, item.body, qual, kind, mode, in_handler)
                    elif isinstance(value, ast.expr) and mode == _LIVE:
                        self._walk_refs(rel, value)

    def _enclosing_class_binding(self, rel: str, qual: str) -> _Binding | None:
        name = qual.rsplit(".", 1)[-1] if qual else ""
        for b in self.bindings_by_name.get(name, []):
            if b.rel == rel and b.qualname == qual and b.kind == "class":
                return b
        return None

    def _process_assign(
        self, rel: str, stmt: ast.Assign | ast.AnnAssign, qual: str, kind: str, mode: str,
    ) -> None:
        if isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            values = [stmt.value] if stmt.value is not None else []
            # stmt.annotation deliberately ignored: annotations confer nothing.
        else:
            targets = list(stmt.targets)
            values = [stmt.value]

        name_targets: list[ast.Name] = []
        other_targets: list[ast.expr] = []
        for t in targets:
            if isinstance(t, ast.Name):
                name_targets.append(t)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in ast.walk(t):
                    # Only Store-ctx Names are binding targets — a Name under a
                    # Subscript/Attribute target (`a, tbl[k] = ...`) is a read.
                    if isinstance(e, ast.Name) and isinstance(e.ctx, ast.Store):
                        name_targets.append(e)
            else:
                other_targets.append(t)

        if kind in ("module", "class") and name_targets:
            parent = self._enclosing_class_binding(rel, qual) if kind == "class" else None
            deferred = values + other_targets
            for t in name_targets:
                self._register(
                    rel, t.id, _qual(qual, t.id), stmt, "assign", parent,
                    deferred=deferred,
                )
            # RHS references propagate only once ≥1 target binding is live
            # (write-only tables stay inert). Nothing to walk now.
            return

        if mode != _LIVE:
            return
        # Pure subscript/attribute-store mutation (module-level table pokes)
        # or a function-local assign: a root — walk everything except plain
        # Name stores (a store is not a reference).
        for t in other_targets:
            self._walk_refs(rel, t)
        for v in values:
            self._walk_refs(rel, v)

    # ── expression reference walking ──────────────────────────────────────

    def _walk_refs(self, rel: str, expr: ast.AST) -> None:
        stack: list[ast.AST] = [expr]
        saw_autoload_fact = False
        while stack:
            node = stack.pop()

            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    self._add_ref(rel, node.id)
                continue

            if isinstance(node, ast.Attribute):
                # Class-insensitive: any attribute name references any binding
                # of that name (self._helper() keeps _helper live anywhere).
                self._add_ref_name(node.attr)
                stack.append(node.value)
                continue

            if isinstance(node, ast.Call):
                fname = self._callee_name(node.func)
                if fname in ("__import__", "import_module"):
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        self._edge_dotted(arg.value)
                    else:
                        self._note_unresolved(rel, node, fname)
                elif fname in ("spec_from_file_location", "exec_module"):
                    self.autoload_callsite.add(rel)
                    saw_autoload_fact = True
                elif fname in ("getattr", "setattr", "hasattr", "delattr"):
                    if len(node.args) >= 2:
                        a = node.args[1]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            self._add_ref_name(a.value)
                stack.extend(ast.iter_child_nodes(node))
                continue

            if isinstance(node, ast.Subscript):
                # globals()["name"] / vars()["name"] dispatch.
                v, s = node.value, node.slice
                if (
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id in ("globals", "vars")
                    and isinstance(s, ast.Constant)
                    and isinstance(s.value, str)
                ):
                    self._add_ref_name(s.value)
                stack.extend(ast.iter_child_nodes(node))
                continue

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if self._string_fact(rel, node.value):
                    saw_autoload_fact = True
                continue

            stack.extend(ast.iter_child_nodes(node))

        if saw_autoload_fact or rel in self.autoload_callsite:
            self._maybe_admit_autoload(rel)

    @staticmethod
    def _callee_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _note_unresolved(self, rel: str, node: ast.AST, fname: str) -> None:
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        self.unresolved_dynamic.append(f"{where} {fname}(<non-literal>)")

    def _string_fact(self, rel: str, lit: str) -> bool:
        """Record text-read / autoloader facts for a string literal seen in a
        live region. Plain string tokens are NEVER references."""
        found = False
        if (
            not lit
            or len(lit) > 200
            or "\n" in lit
            or "\x00" in lit
            or lit in (".", "..")
            or lit.startswith(("/", "\\"))
            or ".." in lit.split("/")
        ):
            return False
        if lit.endswith(".py"):
            # Text-read: a live region reading an in-repo .py as TEXT exempts
            # it from Tier A but does NOT seed liveness (Tier B still counts).
            for cand in (lit, f"{self._dir_of(rel)}/{lit}" if self._dir_of(rel) else lit):
                cand = cand.lstrip("./")
                if cand in self.trees:
                    self._pend(self._mark_text_read, cand)
            if "/" not in lit:
                # A bare *.py basename is an autoloader file pattern candidate.
                self.autoload_pynames.setdefault(rel, set()).add(lit)
                found = True
            return found
        # Qualifying directory literal (relative, no "..", names an existing
        # repo dir) — an autoloader dir candidate.
        cands = [lit]
        if self._dir_of(rel):
            cands.append(f"{self._dir_of(rel)}/{lit}")
        for cand in cands:
            cand = cand.strip("/")
            try:
                is_dir = bool(cand) and (self.root / cand).is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                self.autoload_dirs.setdefault(rel, set()).add(cand)
                found = True
                break
        return found

    @staticmethod
    def _dir_of(rel: str) -> str:
        return rel.rsplit("/", 1)[0] if "/" in rel else ""

    def _maybe_admit_autoload(self, rel: str) -> None:
        """The narrow autoloader-dir rule: a live spec_from_file_location /
        exec_module call site paired with a directory literal admits that
        directory's *.py files as reachable, and their top-level ClassDefs
        become liveness roots. When the call site also names a specific *.py
        basename (the ``<dir>/<app_*>/strategy.py`` loader shape) only files
        with that basename are admitted — dex-tree neighbours stay dead."""
        if rel not in self.autoload_callsite:
            return
        dirs = sorted(self.autoload_dirs.get(rel, ()))
        names = sorted(self.autoload_pynames.get(rel, ()))
        for d in dirs:
            key = (rel, d)
            if key in self._autoload_admitted:
                continue
            self._autoload_admitted.add(key)
            for target in sorted(self.trees):
                if not target.startswith(d + "/"):
                    continue
                base = target.rsplit("/", 1)[-1]
                if names and base not in names:
                    continue
                self.autoload_roots.add(target)
                if target in self.reached:
                    self._seed_top_classes(target)
                else:
                    self._pend(self._reach, target)

    # ══════════════════════════════════════════════════════════════════════
    #                          COUNTING (post-fixpoint)
    # ══════════════════════════════════════════════════════════════════════

    def _binding_live(self, node: ast.AST, name: str | None = None) -> bool:
        for b in self.bindings_by_node.get(id(node), []):
            if name is None or b.name == name:
                if b.live:
                    return True
        return False

    def _assign_dead(self, node: ast.AST) -> bool:
        blist = self.bindings_by_node.get(id(node), [])
        if not blist:
            return False  # never registered as module/class binding → not counted
        return not any(b.live for b in blist)

    def count(self) -> tuple[int, list[tuple[str, str | None, int]]]:
        offenders: list[tuple[str, str | None, int]] = []
        total = 0

        # Tier A — whole files outside the reachability closure.
        for rel in sorted(self.trees):
            if rel in self.reached or rel in self.text_read:
                continue
            n = self.node_counts[rel]
            total += n
            offenders.append((rel, None, n))

        # Tier B — maximal dead subtrees inside reachable / text-read files.
        for rel in sorted(self.reached | self.text_read):
            tree = self.trees.get(rel)
            if tree is None:
                continue
            entries = self._count_dead(rel, list(tree.body), "", "module")
            for path, qualname, n in entries:
                total += n
                offenders.append((path, qualname, n))

        # Exempt-tree overflow above the quarantine budget.
        excess = max(0, self.exempt_nodes - TEST_TREE_NODE_BUDGET)
        if excess:
            total += excess
            offenders.append(("<exempt-tree-overflow>", None, excess))

        offenders.sort(key=lambda e: (-e[2], e[0], e[1] or ""))
        return total, offenders[:MAX_TOP_OFFENDERS]

    def _count_dead(
        self, rel: str, stmts: list[ast.stmt], qual: str, kind: str,
    ) -> list[tuple[str, str, int]]:
        out: list[tuple[str, str, int]] = []
        for stmt in stmts:
            if isinstance(stmt, _SCOPE_NODES):
                q = _qual(qual, stmt.name)
                if self._binding_live(stmt, stmt.name):
                    child_kind = "class" if isinstance(stmt, ast.ClassDef) else "function"
                    out.extend(self._count_dead(rel, list(stmt.body), q, child_kind))
                else:
                    # Outermost dead root counted once, whole subtree.
                    out.append((rel, q, _count_nodes(stmt)))

            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and kind in ("module", "class"):
                if self._assign_dead(stmt):
                    blist = self.bindings_by_node.get(id(stmt), [])
                    q = _qual(qual, blist[0].name if blist else "<assign>")
                    out.append((rel, q, _count_nodes(stmt)))

            elif isinstance(stmt, ast.If) and _statically_false(stmt.test):
                n = sum(_count_nodes(s) for s in stmt.body)
                if n:
                    out.append((rel, _qual(qual, "<statically-false>"), n))
                out.extend(self._count_dead(rel, stmt.orelse, qual, kind))

            else:
                for _fname, value in ast.iter_fields(stmt):
                    if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                        out.extend(self._count_dead(rel, value, qual, kind))
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, ast.ExceptHandler):
                                out.extend(self._count_dead(rel, item.body, qual, kind))
                            elif isinstance(item, ast.match_case):
                                out.extend(self._count_dead(rel, item.body, qual, kind))
        return out


# ═══════════════════════════════════════════════════════════════════════════
#                                ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════


def unproductive_nodes(repo_path: str) -> DeadwoodResult:
    """Compute the deadwood metric for a submission tree.

    Pure function of the tree (stdlib only). Never raises on malformed input:
    an unparseable NON-exempt file yields Phase-0 None-semantics
    (``unproductive_nodes=None, unparseable=True``) so the caller persists
    None and every consumer skips the value.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return DeadwoodResult(unproductive_nodes=None, unparseable=True)

    analyzer = _Analyzer(root)
    if not analyzer.scan():
        logger.warning(
            "[deadwood] unproductive_nodes=None (unparseable non-exempt files: %s) "
            "version=%d (observe-only) repo=%s",
            ", ".join(analyzer.unparseable_files), UNPRODUCTIVE_METRIC_VERSION, repo_path,
        )
        return DeadwoodResult(unproductive_nodes=None, unparseable=True)

    analyzer.run()

    if analyzer.unresolved_dynamic:
        logger.info(
            "[deadwood] unresolvable dynamic imports (log-only in Phase 0): %s",
            "; ".join(sorted(analyzer.unresolved_dynamic)),
        )

    total, offenders = analyzer.count()
    return DeadwoodResult(unproductive_nodes=total, top_offenders=offenders)
