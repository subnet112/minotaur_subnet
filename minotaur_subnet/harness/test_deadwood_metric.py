"""Tests for the Phase-0 deadwood metric (harness/deadwood.unproductive_nodes).

The metric counts the AST-node mass of a submission that provably does no
work at runtime. These tests pin the properties the design depends on:

  1. DELETION WINS    — deleting a dead file / def lowers the count; nothing
                        else does.
  2. GOLF-NEUTRALITY  — minifying LIVE code moves the count by exactly zero
                        (live nodes never enter the count; no denominator).
  3. NO LAUNDERING    — keep-strings, no-op decorators, except-ImportError
                        fallbacks and internal cross-references do NOT
                        enliven dead code.
  4. NARROW HOLES     — the autoloader-dir rule admits real plugin loaders;
                        the exempt tree is budgeted, not unlimited.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from minotaur_subnet.harness.deadwood import (
    TEST_TREE_NODE_BUDGET,
    UNPRODUCTIVE_METRIC_VERSION,
    DeadwoodResult,
    unproductive_nodes,
)


def _write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))


def _cat(*srcs: str) -> str:
    """Join source fragments, dedenting EACH first (mixing module-level
    constants with test-local indented literals must not nest them)."""
    return "\n".join(textwrap.dedent(s) for s in srcs)


def _nodes(src: str) -> int:
    return sum(1 for _ in ast.walk(ast.parse(textwrap.dedent(src))))


def _run(root: Path) -> DeadwoodResult:
    return unproductive_nodes(str(root))


# A minimal all-live solver: every module-level binding of solver.py is a
# seed, `initialize`/`generate_plan` are HARNESS_SURFACE methods on the live
# exported class, and lib.py's helpers are referenced from live regions.
SOLVER = """
import lib

class Solver:
    def initialize(self, config):
        self.state = lib.prepare(config)

    def generate_plan(self, intent, state, snapshot=None):
        return lib.route(intent, self.state)

SOLVER_CLASS = Solver
"""

LIB_LIVE = """
def prepare(config):
    out = dict(config)
    out["ready"] = True
    return out

def route(intent, state):
    plan = [intent, state]
    return plan
"""

DEAD_DEF = """
def forgotten_helper(x):
    a = x + 1
    b = a * 2
    c = b - 3
    return c
"""


def _base_repo(root: Path) -> None:
    _write(root, "solver.py", SOLVER)
    _write(root, "lib.py", LIB_LIVE)


def test_all_live_repo_counts_zero(tmp_path):
    _base_repo(tmp_path)
    r = _run(tmp_path)
    assert r.unproductive_nodes == 0
    assert r.top_offenders == []
    assert r.unparseable is False
    assert r.version == UNPRODUCTIVE_METRIC_VERSION


def test_deleting_a_dead_file_lowers_the_count(tmp_path):
    _base_repo(tmp_path)
    _write(tmp_path, "attic.py", DEAD_DEF)  # never imported by anything
    with_attic = _run(tmp_path).unproductive_nodes

    (tmp_path / "attic.py").unlink()
    without_attic = _run(tmp_path).unproductive_nodes

    assert with_attic == _nodes(DEAD_DEF)  # whole file is Tier A
    assert without_attic == 0
    assert with_attic > without_attic


def test_adding_a_dead_def_raises_the_count(tmp_path):
    _base_repo(tmp_path)
    before = _run(tmp_path).unproductive_nodes

    _write(tmp_path, "lib.py", LIB_LIVE + DEAD_DEF)
    r = _run(tmp_path)

    assert r.unproductive_nodes > before
    assert ("lib.py", "forgotten_helper", _nodes(DEAD_DEF) - 1) in r.top_offenders


def test_minifying_a_live_function_changes_nothing(tmp_path):
    """Golfing LIVE code moves the metric by exactly zero — live nodes never
    enter the count and there is no denominator to shrink."""
    _base_repo(tmp_path)
    _write(tmp_path, "attic.py", DEAD_DEF)  # some dead mass so 0 == 0 is not trivial
    verbose = _run(tmp_path).unproductive_nodes

    golfed_lib = (
        "def prepare(config):\n"
        " out=dict(config);out['ready']=True;return out\n"
        "def route(intent, state):\n"
        " plan=[intent,state];return plan\n"
    )
    (tmp_path / "lib.py").write_text(golfed_lib)
    golfed = _run(tmp_path).unproductive_nodes

    assert verbose == golfed
    assert verbose == _nodes(DEAD_DEF)


def test_keep_string_does_not_enliven_a_dead_def(tmp_path):
    """A bare string token naming a dead def is NOT a reference — only
    dispatch-shaped call sites (getattr/globals()[...]) count strings."""
    _write(tmp_path, "lib.py", LIB_LIVE + DEAD_DEF)
    _write(
        tmp_path, "solver.py",
        SOLVER + '\n_KEEP = "forgotten_helper"  # tombstone laundering attempt\n',
    )
    r = _run(tmp_path)
    assert ("lib.py", "forgotten_helper", _nodes(DEAD_DEF) - 1) in r.top_offenders


def test_except_import_error_is_not_reachability(tmp_path):
    """The dead-fallback lineage: an import inside an except handler is a
    NON-edge, so the fallback module's whole file counts as Tier A."""
    _base_repo(tmp_path)
    _write(
        tmp_path, "solver.py",
        """
        try:
            import lib
        except ImportError:
            import lib_fallback as lib

        class Solver:
            def initialize(self, config):
                self.state = lib.prepare(config)

            def generate_plan(self, intent, state, snapshot=None):
                return lib.route(intent, self.state)

        SOLVER_CLASS = Solver
        """,
    )
    _write(tmp_path, "lib_fallback.py", LIB_LIVE)
    r = _run(tmp_path)
    fallback_nodes = _nodes(LIB_LIVE)
    assert ("lib_fallback.py", None, fallback_nodes) in r.top_offenders
    assert r.unproductive_nodes == fallback_nodes  # lib.py itself is fully live


def test_noop_decorator_does_not_enliven(tmp_path):
    """Decoration never enlivens the decorated def (the decorator NAME itself
    is referenced — it executes — but the def still needs a real caller)."""
    _base_repo(tmp_path)
    dead_decorated = """
    def noop(fn):
        return fn

    @noop
    def stamped_but_dead(x):
        a = x + 1
        b = a * 2
        return b
    """
    _write(tmp_path, "lib.py", _cat(LIB_LIVE, dead_decorated))
    r = _run(tmp_path)
    dead = [q for (_p, q, _n) in r.top_offenders]
    assert "stamped_but_dead" in dead
    assert "noop" not in dead  # the decorator name IS referenced


def test_cross_referencing_dead_classes_stay_dead(tmp_path):
    """Two dead classes referencing each other from their method bodies do not
    resurrect each other — references propagate only FROM live regions."""
    _base_repo(tmp_path)
    dead_pair = """
    class DeadA:
        def make_b(self):
            return DeadB()

    class DeadB:
        def make_a(self):
            return DeadA()
    """
    _write(tmp_path, "lib.py", _cat(LIB_LIVE, dead_pair))
    r = _run(tmp_path)
    dead = [q for (_p, q, _n) in r.top_offenders]
    assert "DeadA" in dead
    assert "DeadB" in dead


def test_autoloader_dir_admits_its_plugins(tmp_path):
    """The narrow spec_from_file_location + directory-literal rule: the
    admitted plugin file is reachable and its top-level classes are liveness
    roots — but ONLY when a live region actually runs such a loader."""
    plugin = """
    class PluginStrategy:
        def accepts(self, app_id, intent_function=""):
            return self._match(app_id)

        def _match(self, app_id):
            return app_id.startswith("app_")
    """
    loader = """
    def load_plugins():
        import importlib.util
        base = "plugins"
        name = "impl.py"
        spec = importlib.util.spec_from_file_location("plugin", base + "/" + name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    PLUGINS = load_plugins()
    """
    loader_solver = _cat(SOLVER, loader)
    _base_repo(tmp_path)
    _write(tmp_path, "solver.py", loader_solver)
    _write(tmp_path, "plugins/impl.py", plugin)
    r = _run(tmp_path)
    # Admitted: not Tier A; class seeded live; accepts (HARNESS_SURFACE) and
    # its internally-referenced helper live too — zero dead mass.
    assert r.unproductive_nodes == 0

    # Contrast: without the loader, the plugin tree is plain Tier A deadwood.
    _write(tmp_path, "solver.py", SOLVER)
    r2 = _run(tmp_path)
    assert ("plugins/impl.py", None, _nodes(plugin)) in r2.top_offenders


def test_exempt_tests_under_budget_contribute_zero(tmp_path):
    _base_repo(tmp_path)
    _write(tmp_path, "tests/test_lib.py", DEAD_DEF * 3)  # well under 8000 nodes
    _write(tmp_path, "tests/conftest.py", "import lib\n")
    _write(tmp_path, "conftest.py", "X = 1\n")  # **/conftest.py matches root too
    assert _run(tmp_path).unproductive_nodes == 0


def test_exempt_tree_overflow_above_budget_counts(tmp_path):
    _base_repo(tmp_path)
    big = "x = 1\n" * 4200  # Module + 4200*(Assign+Name+Constant) ≈ 12,601 nodes
    _write(tmp_path, "tests/test_big.py", big)
    exempt_total = _nodes(big)
    assert exempt_total > TEST_TREE_NODE_BUDGET
    r = _run(tmp_path)
    excess = exempt_total - TEST_TREE_NODE_BUDGET
    assert r.unproductive_nodes == excess
    assert ("<exempt-tree-overflow>", None, excess) in r.top_offenders


def test_empty_repo_is_zero(tmp_path):
    r = _run(tmp_path)
    assert r.unproductive_nodes == 0
    assert r.top_offenders == []
    assert r.unparseable is False


def test_unparseable_non_exempt_file_is_none_semantics(tmp_path):
    """Phase 0: an unparseable NON-exempt file cannot be measured — the result
    is None (the caller persists unproductive_nodes=None), never a guess."""
    _base_repo(tmp_path)
    _write(tmp_path, "broken.py", "def oops(:\n")
    r = _run(tmp_path)
    assert r.unproductive_nodes is None
    assert r.unparseable is True
    assert r.top_offenders == []


def test_unparseable_exempt_file_contributes_zero(tmp_path):
    """An unparseable EXEMPT file is an accepted inert-text micro-hole."""
    _base_repo(tmp_path)
    _write(tmp_path, "tests/test_broken.py", "def oops(:\n")
    r = _run(tmp_path)
    assert r.unproductive_nodes == 0
    assert r.unparseable is False


def test_missing_solver_entry_counts_everything(tmp_path):
    """No solver.py ⇒ no reachability root ⇒ every non-exempt file is Tier A
    (screening's REQUIRED_FILES check fires first in practice)."""
    _write(tmp_path, "lib.py", LIB_LIVE)
    r = _run(tmp_path)
    assert r.unproductive_nodes == _nodes(LIB_LIVE)


def test_write_only_table_counts_dead(tmp_path):
    """A module-level assign whose targets are never read is a dead subtree —
    and its RHS references must not enliven anything."""
    table = """
    _SHADOW_TABLE = {
        "a": forgotten_helper,
        "b": forgotten_helper,
    }
    """
    _base_repo(tmp_path)
    _write(tmp_path, "lib.py", _cat(LIB_LIVE, DEAD_DEF, table))
    r = _run(tmp_path)
    dead = [q for (_p, q, _n) in r.top_offenders]
    assert "_SHADOW_TABLE" in dead
    assert "forgotten_helper" in dead  # the table's RHS did not enliven it


# ── store round-trip for the three persisted fields ──────────────────────────


def test_store_round_trip_deadwood_fields(tmp_path):
    from minotaur_subnet.harness.submission_store import SubmissionStore

    persist = tmp_path / "subs.json"
    store1 = SubmissionStore(persist_path=persist)
    sub = store1.create(
        repo_url="https://github.com/miner/solver",
        commit_hash="abc123def456",
        epoch=7,
        hotkey="5GrwvaEF_test",
    )
    offenders = [["kb_a122b33.py", None, 6334], ["lib.py", "forgotten_helper", 25]]
    store1.set_deadwood_metric(
        sub.submission_id, 15155, UNPRODUCTIVE_METRIC_VERSION, offenders,
    )

    store2 = SubmissionStore(persist_path=persist)
    loaded = store2.get(sub.submission_id)
    assert loaded.unproductive_nodes == 15155
    assert loaded.unproductive_metric_version == UNPRODUCTIVE_METRIC_VERSION
    assert loaded.unproductive_top_offenders == offenders

    d = loaded.to_dict()
    assert d["unproductive_nodes"] == 15155
    assert d["unproductive_metric_version"] == UNPRODUCTIVE_METRIC_VERSION
    assert d["unproductive_top_offenders"] == offenders
    assert loaded.status_dict()["unproductive_nodes"] == 15155

    # None-semantics round-trip: unparseable ⇒ nodes None, version still set.
    store1.set_deadwood_metric(
        sub.submission_id, None, UNPRODUCTIVE_METRIC_VERSION, [],
    )
    store3 = SubmissionStore(persist_path=persist)
    loaded3 = store3.get(sub.submission_id)
    assert loaded3.unproductive_nodes is None
    assert loaded3.unproductive_metric_version == UNPRODUCTIVE_METRIC_VERSION
    assert loaded3.unproductive_top_offenders == []
