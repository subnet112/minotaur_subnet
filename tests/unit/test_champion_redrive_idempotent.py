"""Idempotent re-drive of champion adoption (on_champion_adopted_pr).

When a PRIOR finalize already landed the on-chain certify() and then failed at
publish (transient GitHub 5xx), a re-drive re-attests → "Nonce not increasing"
revert → tx_hash=None. The adoption must treat the already-on-chain cert as
attested so the retried publish COMPLETES the merge, rather than reporting
attest_failed and looping to a deadline abort (incident 2026-07-20 round-e29741775).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.relayer.solver_repo import MergeResult, on_champion_adopted_pr

_MOD = "minotaur_subnet.relayer.solver_repo"


def _sub():
    return SimpleNamespace(
        commit_hash="a" * 40, submission_id="sub-1", pr_number=42, is_private=False,
    )


def _run(*, attest, cert_binds, merge):
    with patch(f"{_MOD}.attest_champion_on_chain", return_value=attest), \
         patch(f"{_MOD}._onchain_cert_binds", return_value=cert_binds), \
         patch(f"{_MOD}.merge_miner_pr_when_certified", return_value=merge), \
         patch(f"{_MOD}.comment_on_pr", return_value=None), \
         patch(f"{_MOD}.close_stale_submission_prs", return_value=0), \
         patch(f"{_MOD}._canonical_main_head_sha", return_value="b" * 40), \
         patch("time.sleep", return_value=None):
        return on_champion_adopted_pr(_sub(), round_id="round-x", certificate=object())


def test_redrive_landed_cert_completes_on_retried_publish():
    # Attest reverts (None) BUT the cert is already on-chain and publish now succeeds.
    res = _run(attest=None, cert_binds=True, merge=MergeResult(True, main_sha="b" * 40))
    assert res.ok is True


def test_genuine_attest_failure_still_aborts():
    # Attest None AND no on-chain cert → a REAL attest failure, unchanged.
    res = _run(attest=None, cert_binds=False, merge=MergeResult(False, "x", "merge"))
    assert res.ok is False
    assert res.code == "attest_failed"
    assert res.stage == "attest"


def test_redrive_landed_cert_but_publish_still_fails_reports_publish_reason():
    # Attest None, cert on-chain, publish STILL failing → report publish_failed
    # (deferrable), NOT the misleading attest_failed — so the round keeps deferring.
    res = _run(attest=None, cert_binds=True,
               merge=MergeResult(False, "publish_failed", "merge"))
    assert res.ok is False
    assert res.code == "publish_failed"


def test_fresh_attest_and_merge_unchanged():
    # Normal path (fresh attest tx) is unaffected; the cert-read fallback isn't used.
    res = _run(attest="0xabc", cert_binds=False, merge=MergeResult(True, main_sha="b" * 40))
    assert res.ok is True
