"""At quorum>1 the leader must propose a CONTENT-ADDRESSED (digest) candidate so
followers can pull-by-digest to independently re-benchmark + vote. If the image push
was best-effort-skipped (image_digest unset), the proposal would carry the leader's
local ``{{.Id}}`` sha — unverifiable on any other host, so every follower would be
forced to REJECT and the round could never reach quorum. _build_champion_proposal_for_round
must fail CLOSED at quorum>1 rather than broadcast an un-poolable candidate. At
quorum<=1 (single leader voter benchmarks locally) the legacy id is fine; genesis/
builtin candidates carry no image and are exempt.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions import champion_consensus as C

BARE = "a" * 64                 # bare 64-hex digest D (content-addressed)
LEGACY = "sha256:" + "b" * 64   # local {{.Id}} — unverifiable cross-host


def _candidate(*, image_digest=None, image_id=LEGACY, hotkey="5Gminer",
               repo="https://github.com/miner/solver"):
    return SimpleNamespace(
        submission_id="sub_x", image_digest=image_digest, image_id=image_id,
        hotkey=hotkey, repo_url=repo, commit_hash="c" * 40,
    )


def _round(quorum):
    return SimpleNamespace(
        round_id="round-1", finalist_submission_id="sub_x", quorum_required=quorum,
        committee_hash="committee", benchmark_pack_hash="pack",
        effective_epoch=6, finalist_image_id=None, shadow_case_log_hash=None,
        incumbent_image_id=None,
    )


def _patch(monkeypatch, candidate, quorum):
    monkeypatch.setattr(C, "get_store", lambda: SimpleNamespace(get=lambda sid: candidate))
    monkeypatch.setattr(
        C, "get_champion_consensus_manager",
        lambda: SimpleNamespace(quorum_required=quorum, committee_hash="committee"),
    )


def test_quorum_gt1_no_digest_fails_closed(monkeypatch):
    _patch(monkeypatch, _candidate(image_digest=None), quorum=3)
    with pytest.raises(HTTPException) as ei:
        C._build_champion_proposal_for_round(_round(3))
    assert ei.value.status_code == 409
    assert "digest" in ei.value.detail.lower()


def test_quorum_gt1_with_digest_ok(monkeypatch):
    digest_ref = "ghcr.io/subnet112/minotaur-solver@sha256:" + BARE
    _patch(monkeypatch, _candidate(image_digest=digest_ref), quorum=3)
    proposal = C._build_champion_proposal_for_round(_round(3))[0]
    assert proposal.candidate_image_id == BARE  # the whole quorum signs the bare digest


def test_quorum1_no_digest_allowed(monkeypatch):
    _patch(monkeypatch, _candidate(image_digest=None), quorum=1)
    proposal = C._build_champion_proposal_for_round(_round(1))[0]
    assert proposal is not None  # single leader voter benchmarks locally → legacy id OK


def test_quorum_gt1_genesis_builtin_exempt(monkeypatch):
    cand = _candidate(image_digest=None, image_id=None,
                      hotkey="__genesis__", repo="builtin://genesis")
    _patch(monkeypatch, cand, quorum=3)
    proposal = C._build_champion_proposal_for_round(_round(3))[0]
    assert proposal is not None  # builtin/genesis carries no image → exempt


def test_quorum_gt1_prefers_real_digest_over_prefixed_passed_id(monkeypatch):
    # THE BUG: the leader's coordinator passes the PREFIXED local image id
    # (finalist_image_id = "sha256:<hex>") as candidate_image_id, while the candidate has
    # a REAL pushed digest. The old resolution used the prefixed id verbatim → is_bare_digest
    # False → wrongly fired the quorum>1 gate, blocking EVERY multi-validator cert. The fix
    # must prefer the real bare digest.
    digest_ref = "ghcr.io/subnet112/minotaur-solver@sha256:" + BARE
    _patch(monkeypatch, _candidate(image_digest=digest_ref), quorum=3)
    proposal = C._build_champion_proposal_for_round(_round(3), candidate_image_id=LEGACY)[0]
    assert proposal.candidate_image_id == BARE  # real digest wins over the prefixed local id


def test_quorum_gt1_prefixed_passed_id_no_real_digest_still_fails(monkeypatch):
    # Guard must STILL fail closed: a prefixed local id is NOT normalized into a passing
    # bare digest when there is no real pushed digest (followers couldn't pull it).
    _patch(monkeypatch, _candidate(image_digest=None), quorum=3)
    with pytest.raises(HTTPException) as ei:
        C._build_champion_proposal_for_round(_round(3), candidate_image_id=LEGACY)
    assert ei.value.status_code == 409


def test_follower_passed_bare_digest_used_verbatim(monkeypatch):
    # A follower reconstructs the leader's authoritative BARE digest (passed as
    # candidate_image_id) to verify the signature; it must be used verbatim even when the
    # follower's own candidate record has no image_digest, so leader+follower agree.
    _patch(monkeypatch, _candidate(image_digest=None), quorum=3)
    proposal = C._build_champion_proposal_for_round(_round(3), candidate_image_id=BARE)[0]
    assert proposal.candidate_image_id == BARE
