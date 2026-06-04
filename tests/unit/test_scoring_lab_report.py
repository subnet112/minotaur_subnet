"""Phase 3 — the Sealed Report hash + diff (scoring_lab.report)."""
import types

from minotaur_subnet.harness.scoring_lab.model import Scenario
from minotaur_subnet.harness.scoring_lab.report import (
    canonical_case, compute_report_hash, diff_reports, scorer_digest,
)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def _cases(min1: str = "1800000000"):
    s1 = Scenario("WETH_to_USDC", WETH, USDC, "1000000000000000000", min1, app_id="dex")
    s2 = Scenario("USDC_to_WETH", USDC, WETH, "2000000", "500000000000000", app_id="dex")
    return [canonical_case(s1, 100), canonical_case(s2, 100)]


def test_report_hash_deterministic():
    assert compute_report_hash(_cases(), 100, "sha256:abc", 50) == \
           compute_report_hash(_cases(), 100, "sha256:abc", 50)


def test_report_hash_is_case_order_independent():
    c = _cases()
    assert compute_report_hash(c, 100, "d", 50) == compute_report_hash(list(reversed(c)), 100, "d", 50)


def test_report_hash_changes_with_min():
    # the quote-derived min is part of the seal — a different min is a different test
    assert compute_report_hash(_cases("1800000000"), 100, "d", 50) != \
           compute_report_hash(_cases("875575"), 100, "d", 50)


def test_report_hash_binds_block_scorer_and_slippage():
    base = compute_report_hash(_cases(), 100, "d", 50)
    assert base != compute_report_hash(_cases(), 101, "d", 50)     # fork block
    assert base != compute_report_hash(_cases(), 100, "d2", 50)    # scorer digest
    assert base != compute_report_hash(_cases(), 100, "d", 75)     # slippage


def test_diff_reports_comparable_only_when_seals_match():
    def rep(h, lbl):
        return {"seal": {"report_hash": h}, "solver": {"label": lbl}}
    rec = types.SimpleNamespace(outputs={"adopt": True})
    d = diff_reports(rep("X", "champ"), rep("X", "chal"), True, rec)
    assert d["comparable"] is True and d["verdict"] == "ADOPT"
    d2 = diff_reports(rep("X", "champ"), rep("Y", "chal"), False, rec)
    assert d2["comparable"] is False and d2["verdict"] == "REJECT"


def test_scorer_digest_stable_and_prefixed():
    d = scorer_digest("module.exports.score = () => ({score:1})")  # inline JS path
    assert d.startswith("sha256:")
    assert d == scorer_digest("module.exports.score = () => ({score:1})")
