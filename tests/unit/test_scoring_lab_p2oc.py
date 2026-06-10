"""Phase 2c — on-chain co-ranked adoption (P2OcAdoptRule).

p2ref ranks the dethrone on the (gas-polluted) JS surplus and uses on-chain only as a veto,
so it rejects BOTH a gas-gaming challenger that delivers less AND a genuine more-output
challenger that costs gas. p2oc ranks on the unfakeable on-chain OUTPUT surplus, keeping the
same vetoes — output decides, gas can't manufacture or block a win.
"""
import types

from minotaur_subnet.harness.scoring_lab.model import LabConfig
from minotaur_subnet.harness.scoring_lab.stages import P2OcAdoptRule, P2RefAdoptRule


def _card(scores):
    return types.SimpleNamespace(app_scores=scores)


def _eval(rule, champ_js, chal_js, champ_oc, chal_oc, **kw):
    cfg = LabConfig(dethrone_margin=kw.pop("margin", 0.005),
                    max_app_regression=kw.pop("max_reg", 0.10),
                    on_chain_floor=kw.pop("floor", None),
                    max_extra_sandbag=kw.pop("max_sandbag", None))
    return rule.evaluate(_card(champ_js), _card(chal_js), champ_oc, chal_oc, cfg,
                         champ_qa=kw.pop("champ_qa", None), chal_qa=kw.pop("chal_qa", None))


# ── core ranking on on-chain output ──────────────────────────────────────────

def test_p2oc_adopts_meaningful_onchain_gain():
    # +100 BPS = ~+1% output above min, over the +50 BPS (0.005) margin.
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.50}, {"A": 0.55}, {"A": [5000]}, {"A": [5100]})
    assert adopt is True
    assert rec.outputs["net_onchain_bps"] == 100.0


def test_p2oc_rejects_small_onchain_gain_below_margin():
    # +11 BPS (~0.1% more output) is real but below the meaningful-improvement margin → no churn.
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.53}, {"A": 0.54}, {"A": [5026]}, {"A": [5037]})
    assert adopt is False
    assert any("<= margin" in r for r in rec.outputs["reasons"])


def test_p2oc_rejects_onchain_output_regression():
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.54}, {"A": 0.53}, {"A": [5026]}, {"A": [5015]})
    assert adopt is False


# ── the two cases p2ref got wrong, now correct ───────────────────────────────

def test_p2oc_gas_gaming_cannot_win():
    # Challenger has HIGHER JS (gas savings) but LOWER on-chain output → REJECT.
    adopt, _ = _eval(P2OcAdoptRule(), {"A": 0.53}, {"A": 0.5384}, {"A": [5026]}, {"A": [5015]})
    assert adopt is False


def test_p2oc_adopts_real_improvement_that_p2ref_wrongly_rejects():
    # Challenger delivers MORE output (+100 BPS) but LOWER JS (it costs more gas).
    cj, hj = {"A": 0.54}, {"A": 0.53}
    co, ho = {"A": [5000]}, {"A": [5100]}
    # p2ref ranks on JS surplus (-0.01) → REJECT despite more output.
    p2ref_adopt, _ = _eval(P2RefAdoptRule(), cj, hj, co, ho)
    assert p2ref_adopt is False
    # p2oc ranks on on-chain output surplus (+100 BPS) → ADOPT.
    p2oc_adopt, rec = _eval(P2OcAdoptRule(), cj, hj, co, ho)
    assert p2oc_adopt is True
    assert rec.outputs["net_onchain_bps"] == 100.0


# ── vetoes preserved ─────────────────────────────────────────────────────────

def test_p2oc_floor_blocks_below_floor():
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.50}, {"A": 0.60}, {"A": [5000]}, {"A": [3000]}, floor=5000)
    assert adopt is False
    assert any("floor" in r for r in rec.outputs["reasons"])


def test_p2oc_blocks_dropped_app():
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.5, "B": 0.5}, {"A": 0.9},
                       {"A": [5000], "B": [5000]}, {"A": [6000]})
    assert adopt is False
    assert any("dropped" in r for r in rec.outputs["reasons"])


def test_p2oc_blocks_catastrophic_js_regression_despite_onchain_gain():
    # Huge on-chain gain (+500) but JS collapses >10% (gas blow-up) → the safety net blocks it.
    adopt, rec = _eval(P2OcAdoptRule(), {"A": 0.60}, {"A": 0.50}, {"A": [5000]}, {"A": [5500]})
    assert adopt is False
    assert any("JS regress" in r for r in rec.outputs["reasons"])


def test_p2oc_real_fork_numbers_both_directions_stable():
    # Real fork data (block 46904887): baseline=more-output-more-gas, V3only=less-output-less-gas.
    BASE, V3 = ({"dex": 0.5305}, {"dex": [5026]}), ({"dex": 0.5384}, {"dex": [5015]})
    # baseline -> V3only: on-chain -11 BPS → REJECT (gas-gaming can't win).
    a1, _ = _eval(P2OcAdoptRule(), BASE[0], V3[0], BASE[1], V3[1])
    # V3only -> baseline: on-chain +11 BPS, below the +50 margin → REJECT (no noise churn).
    a2, _ = _eval(P2OcAdoptRule(), V3[0], BASE[0], V3[1], BASE[1])
    assert a1 is False and a2 is False  # champion stays either way — stable, no gas-gaming win
