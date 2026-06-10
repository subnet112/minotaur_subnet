"""Phase 2a — reference-anchored adoption rule (scoring_lab P2RefAdoptRule).

Decision = per-app SURPLUS (challenger − champion app score) above the on-chain floor,
with a usage-weighted (equal for now) net surplus over the dethrone margin.
"""
import types

from minotaur_subnet.harness.scoring_lab.model import LabConfig
from minotaur_subnet.harness.scoring_lab.stages import P2RefAdoptRule


def _card(scores: dict[str, float]):
    return types.SimpleNamespace(app_scores=scores)


def _eval(champ, chal, champ_oc=None, chal_oc=None, champ_qa=None, chal_qa=None, **cfg_kw):
    cfg = LabConfig(dethrone_margin=cfg_kw.pop("margin", 0.005),
                    max_app_regression=cfg_kw.pop("max_reg", 0.10),
                    on_chain_floor=cfg_kw.pop("floor", None),
                    max_extra_sandbag=cfg_kw.pop("max_sandbag", None))
    adopt, rec = P2RefAdoptRule().evaluate(
        _card(champ), _card(chal), champ_oc or {}, chal_oc or {}, cfg,
        champ_qa=champ_qa, chal_qa=chal_qa)
    return adopt, rec


def test_p2ref_adopts_positive_surplus():
    adopt, rec = _eval({"A": 0.50, "B": 0.50}, {"A": 0.55, "B": 0.55})
    assert adopt is True
    assert rec.outputs["net_surplus"] > 0


def test_p2ref_rejects_negative_surplus():
    # worse but not a >10% per-app regression — rejected purely on net surplus
    adopt, rec = _eval({"A": 0.50, "B": 0.50}, {"A": 0.48, "B": 0.48})
    assert adopt is False
    assert rec.outputs["net_surplus"] < 0


def test_p2ref_onchain_floor_blocks_a_js_better_challenger():
    adopt, rec = _eval(
        {"A": 0.50}, {"A": 0.60},
        chal_oc={"A": [3000]}, floor=5000,  # JS-better but below the on-chain floor
    )
    assert adopt is False
    assert any("floor" in r for r in rec.outputs["reasons"])


def test_p2ref_blocks_per_app_regression_despite_net_gain():
    # huge gain on A, >10% drop on B → blocked even though the mean surplus is positive
    adopt, rec = _eval({"A": 0.50, "B": 0.50}, {"A": 0.90, "B": 0.40})
    assert adopt is False
    assert any("regress" in r for r in rec.outputs["reasons"])


def test_p2ref_blocks_dropping_a_champion_app():
    adopt, rec = _eval({"A": 0.50, "B": 0.50}, {"A": 0.90})  # no B
    assert adopt is False
    assert any("dropped" in r for r in rec.outputs["reasons"])


def test_p2ref_flags_sandbag_without_gate():
    # JS-better challenger that under-quotes 10% more than the champion: flagged, not blocked
    adopt, rec = _eval({"A": 0.50}, {"A": 0.60},
                       champ_qa={"A": {"mean_err": 0.0}}, chal_qa={"A": {"mean_err": 0.10}})
    assert adopt is True
    assert rec.outputs["flags"]
    assert rec.outputs["per_app_diff"]["A"]["extra_sandbag"] == 0.10


def test_p2ref_blocks_sandbag_with_gate():
    adopt, rec = _eval({"A": 0.50}, {"A": 0.60},
                       champ_qa={"A": {"mean_err": 0.0}}, chal_qa={"A": {"mean_err": 0.10}},
                       max_sandbag=0.05)
    assert adopt is False
    assert any("sandbag" in r for r in rec.outputs["reasons"])


def test_p2ref_rejects_onchain_output_regression_despite_higher_js():
    # The V3-only case in miniature: higher JS (gas-inflated) but lower on-chain
    # (delivers users less output) → REJECT. This is the showstopper-fix regression.
    adopt, rec = _eval({"A": 0.50}, {"A": 0.60},
                       champ_oc={"A": [5029]}, chal_oc={"A": [5017]})
    assert adopt is False
    assert any("on-chain output regresses" in r for r in rec.outputs["reasons"])
    assert rec.outputs["per_app_diff"]["A"]["onchain"] == {"champion": 5029.0, "challenger": 5017.0}


def test_p2ref_adopts_when_js_and_onchain_both_improve():
    adopt, rec = _eval({"A": 0.50}, {"A": 0.60},
                       champ_oc={"A": [5000]}, chal_oc={"A": [5100]})
    assert adopt is True
