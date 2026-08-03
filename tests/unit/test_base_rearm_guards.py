"""Guards that must hold BEFORE re-enabling Base in the adoption verdict.

Clearing ``ADOPTION_SCORED_CHAINS`` doubles the gated corpus (57 chain-1 rows
-> 122 including 65 Base). Measured on the live leader across five rounds, that
has two effects, and BOTH are intended:

  * challengers with a genuine Base win become adoptable — five dethrones were
    suppressed by the gate over those rounds;
  * challengers that DROP a Base order the champion serves become vetoed —
    four did. Under the no-regressions rule that is a correct outcome, not a
    regression: the coverage failure exists either way, the gate merely hides it.

These tests pin the machinery that makes both work.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import minotaur_subnet
from minotaur_subnet.epoch.relative_scoring import (
    adoption_scored_chains,
    evaluate_relative_adoption,
)


def _row(intent_id: str, raw_output, chain_id: int):
    return {"intent_id": intent_id, "raw_output": raw_output, "chain_id": chain_id}


# ── 1. every call site must pass the gate ───────────────────────────────────
#
# THE BUG THIS EXISTS FOR: #1200 added `adoption_chains` and threaded it through
# 4 of 10 call sites. It defaults to None (= every chain counts), so the other
# six silently ran UNGATED — including the benchmark worker's "authoritative"
# shadow vote, the veto plane, and the miner-facing display. A fleet-uniform env
# cannot fix a code path that never reads the env.

def _call_sites():
    """(file, line, call-text) for every evaluate_relative_adoption call in the
    package, excluding the definition itself and test files."""
    root = pathlib.Path(minotaur_subnet.__file__).parent
    out = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if "test_" in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"evaluate_relative_adoption\s*\(", text):
            if text[:m.start()].rstrip().endswith("def"):
                continue  # the definition
            # the call's argument text, up to the balanced close paren
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            line = text[:m.start()].count("\n") + 1
            out.append((rel, line, text[m.start():i + 1]))
    return out


def test_every_call_site_passes_the_chain_gate():
    """A call that omits `adoption_chains` silently runs on a DIFFERENT rule
    than the leader's adoption verdict — invisibly, because both sides log a
    confident 'authoritative' result."""
    sites = _call_sites()
    assert sites, "found no call sites — the scanner is broken, not the code"
    missing = [
        f"{f}:{ln}" for f, ln, call in sites if "adoption_chains" not in call
    ]
    assert not missing, (
        "these evaluate_relative_adoption() calls omit adoption_chains and so "
        "run UNGATED (it defaults to None = every chain counts):\n  "
        + "\n  ".join(missing)
    )


def test_call_site_scanner_actually_finds_the_known_sites():
    """Guard the guard: if the scanner silently matches nothing, the test above
    passes vacuously forever."""
    sites = _call_sites()
    files = {f for f, _, _ in sites}
    assert len(sites) >= 8, f"expected the known call sites, found {len(sites)}"
    assert any("manager.py" in f for f in files)
    assert any("benchmark_worker.py" in f for f in files)


# ── 2. clearing the env must mean "every chain counts" ──────────────────────

def test_cleared_env_disables_the_gate(monkeypatch):
    """The rearm action itself: unset/empty -> None -> no chain is excluded."""
    monkeypatch.delenv("ADOPTION_SCORED_CHAINS", raising=False)
    assert adoption_scored_chains() is None
    monkeypatch.setenv("ADOPTION_SCORED_CHAINS", "")
    assert adoption_scored_chains() is None
    monkeypatch.setenv("ADOPTION_SCORED_CHAINS", "   ")
    assert adoption_scored_chains() is None


def test_gate_set_to_both_chains_equals_no_gate(monkeypatch):
    """Naming every scoring chain is equivalent to clearing — so an operator who
    prefers an explicit list gets identical verdicts, not a subtly different rule."""
    champ = [_row("e1", "100", 1), _row("b1", "100", 8453)]
    chal = [_row("e1", "150", 1), _row("b1", "150", 8453)]
    explicit = evaluate_relative_adoption(champ, chal, adoption_chains={1, 8453})
    cleared = evaluate_relative_adoption(champ, chal, adoption_chains=None)
    assert explicit["n_wins"] == cleared["n_wins"] == 2
    assert explicit["adopt"] == cleared["adopt"]


# ── 3. the two live-measured effects of counting Base ───────────────────────

def test_base_win_becomes_adoptable_when_the_gate_is_cleared():
    """The suppressed-dethrone case: five of these were observed live."""
    champ = [_row("e1", "100", 1), _row("b1", "100", 8453)]
    chal = [_row("e1", "100", 1), _row("b1", "300", 8453)]   # only win is on Base

    gated = evaluate_relative_adoption(champ, chal, adoption_chains={1})
    assert gated["n_wins"] == 0, "Base win must not count while Base is gated"
    assert gated["adopt"] is False

    cleared = evaluate_relative_adoption(champ, chal, adoption_chains=None)
    assert cleared["n_wins"] == 1
    assert cleared["adopt"] is True


def test_base_drop_becomes_a_veto_when_the_gate_is_cleared():
    """The other half, and it is CORRECT: a challenger that drops an order the
    champion serves must lose, whatever chain the order is on. The gate was
    hiding the failure, not preventing it."""
    champ = [_row("e1", "100", 1), _row("b1", "100", 8453)]
    chal = [_row("e1", "300", 1), _row("b1", None, 8453)]    # wins ETH, drops Base

    gated = evaluate_relative_adoption(champ, chal, adoption_chains={1})
    assert gated["n_dropped"] == 0
    assert gated["adopt"] is True, "gated, the Base drop is invisible"

    cleared = evaluate_relative_adoption(champ, chal, adoption_chains=None)
    assert cleared["n_dropped"] == 1
    assert cleared["adopt"] is False, "a dropped order is a hard veto, any chain"


def test_base_drop_is_not_netted_against_base_wins():
    """No amount of new Base coverage buys a dropped Base order."""
    champ = [_row(f"b{i}", "100", 8453) for i in range(5)]
    chal = [_row("b0", None, 8453)] + [_row(f"b{i}", "500", 8453) for i in range(1, 5)]
    v = evaluate_relative_adoption(champ, chal, adoption_chains=None)
    assert v["n_wins"] == 4 and v["n_dropped"] == 1
    assert v["adopt"] is False
