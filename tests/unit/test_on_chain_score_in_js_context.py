"""The App's own verdict must reach the JS scorer.

`_simulation_to_dict` exposed what MOVED (token_transfers, state_changes) but
never what the App RULED (scoreIntent's BPS). The DEX aggregator measures an
outcome — its plan executes real calls, tokens move, its scorer reads
token_transfers — so it never needed the verdict and nobody noticed the channel
was absent.

An App whose plan is DATA rather than code has no such observable.
AlphaYieldApp (chain 964) executes no solver calls at all, so token_transfers
and state_changes are both legitimately empty and scoreIntent's return is the
ONLY signal. Its scorer read `sim.score`, got undefined, and fell back to a flat
0.15 floor while the contract was returning a correctly graded verdict —
measured 2026-08-26 across the five allowlisted candidates:

    uid 230 -> 10000   uid 0 -> 1427   uid 190 -> 588   uid 231 -> 21   uid 48 -> 0

matching survey(112)'s ranking exactly.
"""
from __future__ import annotations

from minotaur_subnet.engine.context import _simulation_to_dict
from minotaur_subnet.shared.types import SimulationResult


def _sim(**kw):
    base = dict(success=True, gas_used=0)
    base.update(kw)
    return SimulationResult(**base)


def test_the_verdict_reaches_the_sandbox():
    d = _simulation_to_dict(_sim(on_chain_score=10000))
    assert d["on_chain_score"] == 10000
    assert d["onChainScore"] == 10000


def test_score_is_the_name_a_scorer_reaches_for_first():
    """AlphaYieldApp's `pick(sim, "score")` looks here before anywhere else."""
    assert _simulation_to_dict(_sim(on_chain_score=1427))["score"] == 1427


def test_a_graded_verdict_survives_intact():
    """The whole point: these must arrive distinguishable, not collapsed."""
    got = [_simulation_to_dict(_sim(on_chain_score=v))["score"]
           for v in (10000, 1427, 588, 21, 0)]
    assert got == [10000, 1427, 588, 21, 0]


def test_zero_is_a_verdict_not_an_absence():
    """0 BPS means 'the worst allowlisted choice', which is information."""
    d = _simulation_to_dict(_sim(on_chain_score=0))
    assert "on_chain_score" in d and d["on_chain_score"] == 0
    assert d["score"] == 0


def test_absent_when_there_is_no_verdict():
    """No scoreIntent path ran — the key must not appear at all, so a scorer
    can tell 'no verdict' from 'verdict of zero'."""
    d = _simulation_to_dict(_sim())
    for k in ("on_chain_score", "onChainScore", "score"):
        assert k not in d


def test_additive_only_for_a_scorer_that_ignores_it():
    """The DEX references neither key; its view must be bit-identical."""
    without = _simulation_to_dict(_sim())
    with_ = _simulation_to_dict(_sim(on_chain_score=10000))
    added = set(with_) - set(without)
    assert added == {"on_chain_score", "onChainScore", "score"}
    for k in without:
        assert without[k] == with_[k], f"{k} changed for an unrelated scorer"
