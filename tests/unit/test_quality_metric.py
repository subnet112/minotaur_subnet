"""App-declared quality metric — the seam that de-DEX-ifies the adoption gate.

The load-bearing property is the NEGATIVE one: an app that declares nothing
must be scored bit-identically to before, because the platform default IS the
DEX aggregator's delivered-output rule and it currently decides every champion
on the subnet.

The positive property is that an app whose orders legitimately move no tokens
(a rebalancing vault) becomes rankable at all — today
``has_delivered_value_rows`` REJECTS such a submission rather than scoring it.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.epoch import relative_scoring as rs
from minotaur_subnet.shared import quality_metric as qm

VAULT = {
    "source": "js_score", "field": "", "polarity": "higher_better",
    "scale": "ratio_1", "validity": "any_row",
}
TRACKING_ERROR = {
    "source": "js_metadata", "field": "tracking_error_bps",
    "polarity": "lower_better", "scale": "bps", "validity": "any_row",
}


def _row(intent_id="app:1", raw_output=None, metric=None):
    row = {"intent_id": intent_id, "raw_output": raw_output}
    if metric is not None:
        row["metric"] = metric
    return row


# ═══════════════════════════════════════════════════════════════════════════
#  Default contract == today's behaviour, exactly
# ═══════════════════════════════════════════════════════════════════════════


class TestDefaultUnchanged:
    def test_no_declaration_resolves_to_default(self):
        assert qm.resolve(None) is qm.DEFAULT
        assert qm.resolve({}) is qm.DEFAULT
        assert qm.resolve({"quality_metric": {}}) is qm.DEFAULT

    def test_default_is_the_dex_delivered_output_rule(self):
        assert qm.DEFAULT.source == "js_metadata"
        assert qm.DEFAULT.field == "raw_output"
        assert qm.DEFAULT.polarity == "higher_better"
        assert qm.DEFAULT.validity == "positive"

    def test_default_contract_is_not_persisted(self):
        # Zero added bytes on every existing row.
        assert qm.to_row(qm.DEFAULT) is None

    def test_legacy_row_without_metric_reads_as_default(self):
        assert qm.from_row({"raw_output": "5"}) == qm.DEFAULT
        assert qm.from_row(None) == qm.DEFAULT

    def test_wei_above_2_53_keeps_exact_precision(self):
        big = str(2**60 + 1)
        assert rs._comparable_of(_row(raw_output=big)) == 2**60 + 1

    def test_zero_delivery_still_rejected_on_default(self):
        # The DEX rule: delivering nothing is INVALID, not merely a zero score.
        assert rs.has_delivered_value_rows([_row(raw_output="0")]) is False
        assert rs.has_delivered_value_rows([_row(raw_output="1")]) is True

    def test_default_path_matches_the_original_helpers(self):
        for value in (None, "", "0", "1", "12345678901234567890", "junk"):
            row = _row(raw_output=value)
            assert rs._comparable_of(row) == rs._parse_output(value)
            assert rs._row_has_value(row) == rs._has_value(value)


# ═══════════════════════════════════════════════════════════════════════════
#  A no-output app becomes rankable
# ═══════════════════════════════════════════════════════════════════════════


class TestNoOutputApp:
    def test_zero_metric_is_valid_under_any_row(self):
        # The whole point: a vault that moved no tokens still ran, so it must
        # be scoreable rather than rejected.
        assert rs.has_delivered_value_rows([_row(raw_output="0", metric=VAULT)]) is True

    def test_absent_metric_is_still_invalid(self):
        # "any_row" relaxes the >0 rule, not the produced-a-number rule.
        assert rs.has_delivered_value_rows([_row(raw_output=None, metric=VAULT)]) is False

    def test_js_score_ratio_quantises_to_bps(self):
        assert rs._comparable_of(_row(raw_output="0.5", metric=VAULT)) == 5000
        assert rs._comparable_of(_row(raw_output="1", metric=VAULT)) == 10_000
        assert rs._comparable_of(_row(raw_output="0", metric=VAULT)) == 0

    def test_ratio_is_clamped_to_its_scale(self):
        assert rs._comparable_of(_row(raw_output="2.5", metric=VAULT)) == 10_000
        assert rs._comparable_of(_row(raw_output="-1", metric=VAULT)) == 0


class TestLowerIsBetter:
    def test_polarity_inverts_for_comparison(self):
        # 100 bps of tracking error must rank BELOW 10 bps.
        worse = rs._comparable_of(_row(raw_output="100", metric=TRACKING_ERROR))
        better = rs._comparable_of(_row(raw_output="10", metric=TRACKING_ERROR))
        assert better > worse
        assert better == 9_990 and worse == 9_900

    def test_a_challenger_with_less_error_wins(self):
        champ = [_row("app:1", "100", TRACKING_ERROR)]
        chal = [_row("app:1", "10", TRACKING_ERROR)]
        counts = rs.relative_counts(champ, chal)
        assert counts["better"] == 1 and counts["worse"] == 0
        assert counts["verdict"] == "dethrone"

    def test_a_challenger_with_more_error_regresses(self):
        champ = [_row("app:1", "10", TRACKING_ERROR)]
        chal = [_row("app:1", "100", TRACKING_ERROR)]
        counts = rs.relative_counts(champ, chal)
        assert counts["worse"] == 1 and counts["better"] == 0
        assert counts["verdict"] != "dethrone"

    def test_unbounded_lower_better_is_rejected_at_resolve(self):
        # No exact inverse for an unbounded integer — fail loud rather than
        # silently mis-rank a contest.
        with pytest.raises(qm.QualityMetricError, match="bounded scale"):
            qm.resolve({"quality_metric": {
                "polarity": "lower_better", "scale": "integer",
            }})


# ═══════════════════════════════════════════════════════════════════════════
#  Declaration validation — fail loud, never silently mis-rank
# ═══════════════════════════════════════════════════════════════════════════


class TestValidation:
    @pytest.mark.parametrize("bad", [
        {"source": "on_chain"},        # metric always comes from the app's JS
        {"polarity": "sideways"},
        {"scale": "furlongs"},
        {"validity": "sometimes"},
        {"source": "js_metadata", "field": ""},
    ])
    def test_invalid_declarations_raise(self, bad):
        with pytest.raises(qm.QualityMetricError):
            qm.resolve({"quality_metric": bad})

    def test_a_row_carrying_a_broken_contract_degrades_to_default(self):
        # Resolve-time is the gate; a bad persisted row must not crash a round.
        assert qm.from_row({"metric": {"polarity": "sideways"}}) == qm.DEFAULT


class TestExtract:
    class _Result:
        def __init__(self, score=None, metadata=None):
            self.score = score
            self.metadata = metadata or {}

    def test_js_metadata_is_verbatim(self):
        big = str(2**70)
        assert qm.extract(
            self._Result(metadata={"raw_output": big}), qm.DEFAULT,
        ) == big

    def test_js_score_quantises(self):
        m = qm.resolve({"quality_metric": VAULT})
        assert qm.extract(self._Result(score=0.75), m) == "7500"

    def test_missing_values_are_none(self):
        m = qm.resolve({"quality_metric": VAULT})
        assert qm.extract(self._Result(score=None), m) is None
        assert qm.extract(self._Result(metadata={}), qm.DEFAULT) is None
        assert qm.extract(None, qm.DEFAULT) is None

    def test_custom_metadata_field(self):
        m = qm.resolve({"quality_metric": TRACKING_ERROR})
        assert qm.extract(
            self._Result(metadata={"tracking_error_bps": 42}), m,
        ) == "42"


class TestOnChainScoreReachesJs:
    """The metric comes from JS — so an app that wants the contract's verdict
    needs scoreIntent's BPS visible inside its scorer."""

    def test_on_chain_score_is_exposed_in_context(self):
        from minotaur_subnet.engine.context import _simulation_to_dict
        from minotaur_subnet.shared.types import SimulationResult

        sim = SimulationResult(success=True, gas_used=1, error=None)
        sim.on_chain_score = 9_500
        d = _simulation_to_dict(sim)
        assert d["on_chain_score"] == 9_500
        assert d["onChainScore"] == 9_500

    def test_absent_when_not_measured(self):
        from minotaur_subnet.engine.context import _simulation_to_dict
        from minotaur_subnet.shared.types import SimulationResult

        d = _simulation_to_dict(SimulationResult(success=True, gas_used=1))
        assert "on_chain_score" not in d
