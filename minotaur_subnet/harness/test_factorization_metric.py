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

from minotaur_subnet.harness.screening import (
    _module_max_region,
    _solver_exec_command,
    banned_imports,
    max_region_nodes,
)


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
    assert hits == ["a.py:1 exec", "a.py:2 eval"]


def test_dynamic_code_calls_flags_compile_and_dunder_import(tmp_path):
    # compile() / __import__() were previously ALLOWED — the analyzability gate
    # bans them: both build/resolve code the AST can't otherwise follow.
    (tmp_path / "c.py").write_text(
        "code = compile('1', '<s>', 'eval')\n"
        "m = __import__('king_base')\n"
    )
    assert dynamic_code_calls(str(tmp_path)) == [
        "c.py:1 compile", "c.py:2 __import__",
    ]


def test_dynamic_code_calls_flags_dynamic_import_and_code_construction(tmp_path):
    # Attribute form, matched on the trailing name so an ALIASED importlib/types
    # is still caught: importlib.import_module(<var>) + types.FunctionType/CodeType.
    (tmp_path / "d.py").write_text(
        "import importlib as il, types\n"
        "base = il.import_module(_m)\n"       # d.py:2 import_module
        "fn = types.FunctionType(c, {})\n"    # d.py:3 FunctionType
        "co = types.CodeType()\n"             # d.py:4 CodeType
    )
    assert dynamic_code_calls(str(tmp_path)) == [
        "d.py:2 import_module", "d.py:3 FunctionType", "d.py:4 CodeType",
    ]


def test_dynamic_code_calls_flags_shim_import_indirection(tmp_path):
    # Regression on the LIVE obfuscator: james_base.py resolves its real base
    # module through `__import__(_m)` with a VARIABLE name — invisible to a
    # static import scan, the exact AST-blinding this gate closes.
    (tmp_path / "james_base.py").write_text(
        'import kb_a122b33 as base_module\n'
        'for _m in ("king_solver", "king_base"):\n'
        '    v = getattr(__import__(_m), "SOLVER_VERSION", "")\n'
    )
    assert dynamic_code_calls(str(tmp_path)) == ["james_base.py:3 __import__"]


def test_dynamic_code_calls_flags_spec_loader_exec_module(tmp_path):
    # v2: the spec-loader spelling of dynamic import — building the spec/module
    # is inert until `spec.loader.exec_module(mod)` RUNS it, so the executor is
    # the one name that kills the pattern (the champion `_apex_champ.py`
    # strategies-loader shape).
    (tmp_path / "loader.py").write_text(
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('strat', path)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
    )
    assert dynamic_code_calls(str(tmp_path)) == ["loader.py:4 exec_module"]


def test_dynamic_code_calls_ignores_benign_attribute_calls(tmp_path):
    # Attribute calls whose trailing name is NOT in the ban stay clean: re.compile
    # (the builtin `compile` is bare-name only) and a miner method named `eval`.
    (tmp_path / "b.py").write_text(
        "import re\n"
        "pat = re.compile('x')\n"       # attribute `.compile` — not the builtin
        "tree.eval(ctx)\n"              # attribute `.eval` — a miner method
    )
    assert dynamic_code_calls(str(tmp_path)) == []


def test_floor_cap_pinned_to_stage_a_backstop():
    # Stage-A backstop from the 2026-07-03..07 soak: champion-fork monoculture
    # at 4109 (== canonical main) with tweak outliers to 4163 — the cap blocks
    # only NEW bloat. Stage B ratchets to ~2000-2500 under FLOOR_VERSION=2
    # after the factor tie-break flips the throne and the fleet re-forks.
    assert _screening.MAX_REGION_NODES == 4200
    assert _screening.FLOOR_VERSION == 1


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


def test_dynamic_code_rejected_before_too_entangled(tmp_path, monkeypatch):
    # Analyzability takes precedence over the cap: even TINY code with exec
    # rejects as dynamic_code, never reaching the (huge) entanglement cap.
    repo = _valid_repo(tmp_path, "exec('x = 1')\n")
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", 10_000)
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "dynamic_code"
    assert "solver.py:1 exec" in res.details


def test_dynamic_code_rejects_even_when_floor_unarmed(tmp_path, monkeypatch):
    # DECOUPLED: the analyzability ban does NOT depend on MAX_REGION_NODES.
    # With the factor floor disarmed (None), __import__ indirection still rejects.
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", None)
    repo = _valid_repo(tmp_path, "m = __import__('king_base')\n")
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "dynamic_code"
    assert "__import__" in res.details


def test_dynamic_code_ban_can_be_disarmed(tmp_path, monkeypatch):
    # Escape hatch: DYNAMIC_CODE_ARMED=False → observe-only (logs, never rejects),
    # even with the factor floor also disarmed. Proves the arming is what gates.
    monkeypatch.setattr(_screening, "DYNAMIC_CODE_ARMED", False)
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", None)
    repo = _valid_repo(tmp_path, "code = compile('1', '<s>', 'eval')\n")
    res = run_stage_1(str(repo))
    assert res.passed is True


def test_dynamic_code_armed_by_default():
    # Ships ENFORCING (like the now-armed import ban), version-stamped.
    assert _screening.DYNAMIC_CODE_ARMED is True
    assert _screening.DYNAMIC_CODE_VERSION == 2  # v2 adds exec_module


# ── Banned-import scan (defence-in-depth PREVENT layer) ───────────────────────


def test_banned_imports_catches_nested_urllib_gadget(tmp_path):
    # The chain-killer "putty" class: `import urllib.request` NESTED in a function
    # (invisible to a tree.body-only scan). ast.walk must catch it.
    (tmp_path / "a.py").write_text(
        "import json\n"
        "def _quote():\n"
        "    import urllib.request as u\n"
        "    return u\n"
    )
    hits = banned_imports(str(tmp_path))
    assert hits == ["a.py:3 urllib.request"]


def test_banned_imports_flags_from_and_socket(tmp_path):
    (tmp_path / "b.py").write_text(
        "import socket\n"
        "from http.client import HTTPConnection\n"
    )
    mods = {h.split()[1] for h in banned_imports(str(tmp_path))}
    assert mods == {"socket", "http.client"}


def test_banned_imports_ignores_relative_and_legit(tmp_path):
    # Relative imports are in-tree; web3/eth_abi/json/os are legitimate.
    (tmp_path / "c.py").write_text(
        "from . import helper\n"
        "from .strategies import router\n"
        "import json, os\n"
        "from eth_abi import encode\n"
        "from minotaur_subnet.sdk import intent_solver\n"
    )
    assert banned_imports(str(tmp_path)) == []


def test_banned_imports_skips_unparseable(tmp_path):
    (tmp_path / "ok.py").write_text("import socket\n")
    (tmp_path / "broken.py").write_text("def (:\n")  # SyntaxError — skipped
    assert banned_imports(str(tmp_path)) == ["ok.py:1 socket"]


def test_banned_imports_armed_by_default(tmp_path):
    # ARMED v2 after the observe-only soak (257/257 hits = urllib.request, zero
    # benign dotted names): a banned import now rejects out of the box.
    assert _screening.BANNED_IMPORTS_ARMED is True
    assert _screening.BANNED_IMPORTS_VERSION == 2
    repo = _valid_repo(tmp_path, "import socket\ndef f():\n    return 1\n")
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "banned_import"


def test_banned_imports_allowlists_urllib_parse_spellings(tmp_path):
    # Every spelling that binds ONLY the benign submodule passes…
    (tmp_path / "a.py").write_text(
        "import urllib.parse\n"
        "import urllib.parse as up\n"
        "from urllib.parse import urlparse\n"
        "from urllib import parse\n"
    )
    assert banned_imports(str(tmp_path)) == []


def test_banned_imports_allowlist_does_not_leak_to_request(tmp_path):
    # …while the gadget spellings — including the mixed from-import — still flag.
    (tmp_path / "b.py").write_text(
        "import urllib\n"
        "import urllib.request\n"
        "from urllib import parse, request\n"
        "from urllib.request import urlopen\n"
    )
    mods = {h.split()[1] for h in banned_imports(str(tmp_path))}
    assert mods == {"urllib", "urllib.request"}


def test_banned_imports_armed_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(_screening, "BANNED_IMPORTS_ARMED", True)
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", 10_000)
    repo = _valid_repo(tmp_path, "import socket\ndef f():\n    return 1\n")
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "banned_import"
    assert "socket" in res.details
    # persist-on-reject: metrics still ride the StageResult.
    assert isinstance(res.max_region_nodes, int)


def test_banned_imports_armed_passes_clean_solver(tmp_path, monkeypatch):
    monkeypatch.setattr(_screening, "BANNED_IMPORTS_ARMED", True)
    monkeypatch.setattr(_screening, "MAX_REGION_NODES", 10_000)
    repo = _valid_repo(
        tmp_path,
        "import json, os\nfrom eth_abi import encode\ndef f():\n    return encode\n",
    )
    res = run_stage_1(str(repo))
    assert res.passed is True


# ── Stage-2 exec container hardening (import/init run untrusted code) ──────────


def test_solver_exec_command_is_hardened():
    """The import/init containers — the FIRST place solver code executes — must
    carry the same containment as the benchmark/live runs (orchestrator's
    DOCKER_SECURITY_OPTS): cap-drop, no-new-privileges, pids-limit, plus the
    pre-existing network/fs/mem/cpu caps."""
    cmd = _solver_exec_command("solver-img:screening", "print('x')")
    joined = " ".join(cmd)
    for flag in (
        "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=256",
        "--memory=2g", "--cpus=1.0",
    ):
        assert flag in cmd, f"missing hardening flag: {flag}"
    # runs the passed script under an explicit python entrypoint on the image
    assert cmd[-3:] == ["solver-img:screening", "-c", "print('x')"]
    assert "--entrypoint" in cmd and "python" in cmd


# ── Deadwood floor (PREVENT layer) ────────────────────────────────────────────

from minotaur_subnet.harness import deadwood as _deadwood


def _dead_lineage_repo(tmp_path):
    """A repo whose solver.py is clean but which carries an UNREACHABLE
    dead-lineage module — the shim/superseded-generation obfuscation shape that
    deadwood Tier A counts (imported from nowhere ⇒ whole-file dead mass)."""
    repo = _valid_repo(tmp_path, "SOLVER_CLASS = object\n")
    (tmp_path / "kb_dead_lineage.py").write_text(
        "".join(
            f"def _f{i}():\n    x = {i}\n    y = x + 1\n    return y\n"
            for i in range(60)
        )
    )
    return repo


def test_deadwood_floor_pinned_to_gap_cut():
    # Pinned to the 2026-07 soak valley (clean lineages ≤3871, obfuscated ≥5560).
    assert _deadwood.UNPRODUCTIVE_NODES_MAX == 4600


def test_deadwood_floor_rejects_dead_lineage_over_cap(tmp_path, monkeypatch):
    repo = _dead_lineage_repo(tmp_path)
    measured = _deadwood.unproductive_nodes(str(repo)).unproductive_nodes
    assert isinstance(measured, int) and measured > 0
    monkeypatch.setattr(_deadwood, "UNPRODUCTIVE_NODES_MAX", measured - 1)
    res = run_stage_1(str(repo))
    assert res.passed is False
    assert res.error_code == "too_much_deadwood"
    # The rejected value + offenders still ride on the StageResult (persisted so
    # the miner sees exactly which dead files to delete).
    assert res.unproductive_nodes == measured
    assert res.unproductive_top_offenders


def test_deadwood_floor_passes_under_cap(tmp_path, monkeypatch):
    repo = _dead_lineage_repo(tmp_path)
    measured = _deadwood.unproductive_nodes(str(repo)).unproductive_nodes
    monkeypatch.setattr(_deadwood, "UNPRODUCTIVE_NODES_MAX", measured + 1)
    res = run_stage_1(str(repo))
    assert res.passed is True


def test_deadwood_floor_disarmed_observes_only(tmp_path, monkeypatch):
    # None ⇒ the dead mass is measured + persisted but never gates.
    repo = _dead_lineage_repo(tmp_path)
    monkeypatch.setattr(_deadwood, "UNPRODUCTIVE_NODES_MAX", None)
    res = run_stage_1(str(repo))
    assert res.passed is True
    assert isinstance(res.unproductive_nodes, int) and res.unproductive_nodes > 0


def test_deadwood_floor_skips_none_value(tmp_path, monkeypatch):
    # An unparseable non-exempt file ⇒ unproductive_nodes=None; the armed floor
    # must NOT reject on None (stage 2's import check backstops unparseable code).
    repo = _valid_repo(tmp_path, "SOLVER_CLASS = object\n")
    (tmp_path / "broken.py").write_text("def (:\n")  # SyntaxError
    monkeypatch.setattr(_deadwood, "UNPRODUCTIVE_NODES_MAX", 0)
    res = run_stage_1(str(repo))
    assert res.passed is True
    assert res.unproductive_nodes is None
