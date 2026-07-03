"""Tests for the Phase-0 factorization metric (harness/screening.max_region_nodes).

The metric is the golf-immune ruler behind both the (future) clean-code floor and
the saturated-tie dethrone tie-break. These tests pin the three properties the
design depends on:

  1. GOLF-IMMUNITY   — minifying code does not change the count (AST, not LOC).
  2. FACTORING WINS  — splitting a god-region into named helpers lowers the max.
  3. NO RELOCATION   — hiding logic in a lambda/comprehension/literal does NOT
                       lower it (those do not start a new region).
"""

from __future__ import annotations

import ast
import textwrap

from minotaur_subnet.harness.screening import _module_max_region, max_region_nodes


def _mrn(src: str) -> int:
    return _module_max_region(ast.parse(textwrap.dedent(src)))


# A god-function: one big region.
GOD = """
def solve(x):
    a = x + 1
    b = a * 2
    c = b - 3
    d = c / 4
    e = d ** 2
    return e
"""

# Byte-for-byte the same logic, minified onto one line with `;` separators.
GOLFED = "def solve(x):\n a=x+1;b=a*2;c=b-3;d=c/4;e=d**2;return e\n"

# The same work split into named helpers — `solve`'s region shrinks to a call.
FACTORED = """
def _inc(x): return x + 1
def _dbl(a): return a * 2
def _sub3(b): return b - 3
def _div4(c): return c / 4
def _sq(d): return d ** 2
def solve(x):
    return _sq(_div4(_sub3(_dbl(_inc(x)))))
"""


def test_golf_immunity():
    """Minification must not change the metric — it counts AST nodes, not lines."""
    assert _mrn(GOD) == _mrn(GOLFED)


def test_factoring_lowers_the_max_region():
    """Extracting the god body into named helpers strictly lowers the max region."""
    assert _mrn(FACTORED) < _mrn(GOD)


def test_lambda_relocation_is_not_a_dodge():
    """Relocating a body into a module-level lambda gives NO reduction: the
    lambda's nodes count into the enclosing (module) region, not a new one."""
    expr = "(((x + 1) * 2 - 3) / 4) ** 2 + (x * x) - (x / 7)"
    in_function = f"def solve(x):\n    return {expr}\n"
    in_lambda = f"solve = lambda x: {expr}\n"
    # The lambda form is not LOWER than the function form (same expression, now
    # in the module region) — so it is not a cheaper hiding place...
    assert _mrn(in_lambda) >= _mrn(in_function)
    # ...and it stays far above what genuine factoring into named helpers buys.
    assert _mrn(in_lambda) > _mrn(FACTORED)


def test_comprehension_relocation_is_not_a_dodge():
    """A big comprehension at module level counts into the module region."""
    small = "xs = data\n"
    big = "xs = [((a + 1) * 2 - 3) ** 2 for a in data if a > 0 if a < 100]\n"
    assert _mrn(big) > _mrn(small)


def test_nested_def_body_leaves_parent_region():
    """An inner def's body forms its OWN region; the outer region drops when the
    work moves inside the inner def."""
    flat = """
    def outer(x):
        a = x + 1
        b = a * 2
        c = b - 3
        return c
    """
    nested = """
    def outer(x):
        def inner():
            a = x + 1
            b = a * 2
            c = b - 3
            return c
        return inner()
    """
    # `outer`'s own region shrinks once the body moves into `inner` — but the max
    # over the module is dominated by whichever single region is largest.
    assert _mrn(nested) <= _mrn(flat) + 2  # inner ≈ flat body, outer now tiny


def _class_with(n_methods: int) -> str:
    """A class of `n_methods` identical, non-trivial methods (no leading indent,
    so textwrap.dedent is a no-op)."""
    method = (
        "    def m{i}(self):\n"
        "        a = self.v + 1\n"
        "        b = a * 2\n"
        "        c = b - 3\n"
        "        d = c / 4\n"
        "        return d\n"
    )
    return "class Solver:\n" + "".join(method.format(i=i) for i in range(n_methods))


def test_class_methods_do_not_accumulate():
    """A class with many methods is NOT charged the SUM of their bodies — each
    method body is its own region, so the max is dominated by one body, not N."""
    one = _mrn(_class_with(1))
    five = _mrn(_class_with(5))
    # The dominant region is a single (identical) method body, so adding methods
    # barely moves the max (only the class-header region grows by a few nodes),
    # nowhere near 5×.
    assert five <= one + 2
    assert five < one * 2


def test_async_def_is_a_region():
    """AsyncFunctionDef bodies spin their own region just like def."""
    src = """
    async def solve(x):
        a = x + 1
        b = a * 2
        return b
    """
    assert _mrn(src) > 0


def test_repo_scan_takes_max_and_skips_git(tmp_path):
    """max_region_nodes = the largest region across all in-tree *.py, with .git
    excluded and unparseable files skipped."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "huge.py").write_text("x = 1\n" * 2000)  # must be ignored
    (tmp_path / "solver.py").write_text(textwrap.dedent(GOD))
    (tmp_path / "common.py").write_text(textwrap.dedent(FACTORED))
    (tmp_path / "broken.py").write_text("def oops(:\n")  # unparseable → skipped

    got = max_region_nodes(str(tmp_path))
    assert got == _mrn(GOD)  # GOD dominates; .git and broken.py excluded


def test_empty_repo_is_zero(tmp_path):
    """No parseable Python ⇒ 0 (never raises)."""
    assert max_region_nodes(str(tmp_path)) == 0


# ── Phase 1: dynamic-code ban + armed floor gate ──────────────────────────────

from minotaur_subnet.harness import screening as _screening
from minotaur_subnet.harness.screening import dynamic_code_calls, run_stage_1


def _valid_repo(tmp_path, solver_src: str):
    """A repo that passes every pre-metric stage-1 check."""
    (tmp_path / "Dockerfile").write_text(
        "FROM ghcr.io/subnet112/solver-base:v1\nCOPY . /app\n"
    )
    (tmp_path / "README.md").write_text("# solver\n")
    (tmp_path / "solver.py").write_text(solver_src)
    return tmp_path


def test_dynamic_code_calls_flags_bare_exec_eval(tmp_path):
    (tmp_path / "a.py").write_text(
        "exec('x = 1')\n"
        "y = eval('2 + 2')\n"
    )
    hits = dynamic_code_calls(str(tmp_path))
    assert hits == ["a.py:1", "a.py:2"]


def test_dynamic_code_calls_ignores_attribute_calls_and_compile(tmp_path):
    (tmp_path / "b.py").write_text(
        "import re\n"
        "pat = re.compile('x')\n"       # attribute call — not flagged
        "tree.eval(ctx)\n"              # attribute call — not flagged
        "code = compile('1', '<s>', 'eval')\n"  # compile not banned
    )
    assert dynamic_code_calls(str(tmp_path)) == []


def test_floor_unarmed_observes_only(tmp_path, monkeypatch):
    # MAX_REGION_NODES=None (Phase 0): even a huge region passes.
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", None)
    repo = _valid_repo(tmp_path, textwrap.dedent(GOD))
    res = run_stage_1(str(repo))
    assert res.passed is True
    assert isinstance(res.max_region_nodes, int) and res.max_region_nodes > 0


def test_floor_armed_rejects_too_entangled(tmp_path, monkeypatch):
    repo = _valid_repo(tmp_path, textwrap.dedent(GOD))
    god_nodes = _mrn(GOD)
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", god_nodes - 1)
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "too_entangled"
    # The rejected value still rides on the StageResult (persisted for miners).
    assert res.max_region_nodes == god_nodes


def test_floor_armed_passes_clean_code(tmp_path, monkeypatch):
    repo = _valid_repo(tmp_path, textwrap.dedent(FACTORED))
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", _mrn(GOD))
    res = run_stage_1(str(repo))
    assert res.passed is True


def test_floor_armed_rejects_dynamic_code_first(tmp_path, monkeypatch):
    # exec/eval is checked before the cap: even TINY code with exec rejects.
    repo = _valid_repo(tmp_path, "exec('x = 1')\n")
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", 10_000)
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "dynamic_code"
    assert "solver.py:1" in res.details
