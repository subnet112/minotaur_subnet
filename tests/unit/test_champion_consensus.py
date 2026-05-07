"""Unit tests for champion certification quorum logic."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.champion_manager import (
    ChampionConsensusManager,
    ChampionProposal,
)
from minotaur_subnet.consensus.eip712 import address_from_key


KEY_1 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
KEY_2 = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
KEY_3 = "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"

ADDR_1 = address_from_key(KEY_1)
ADDR_2 = address_from_key(KEY_2)
ADDR_3 = address_from_key(KEY_3)
VALIDATORS = [ADDR_1, ADDR_2, ADDR_3]


def _proposal(candidate_submission_id: str = "sub-final") -> ChampionProposal:
    return ChampionProposal(
        round_id="round-e42-n1",
        committee_hash="0x" + "ab" * 32,
        incumbent_image_id="sha256:" + "1" * 64,
        candidate_submission_id=candidate_submission_id,
        candidate_image_id="sha256:" + "2" * 64,
        benchmark_pack_hash="0x" + "cd" * 32,
        shadow_case_log_hash="0x" + "ef" * 32,
        effective_epoch=43,
    )


@pytest.mark.asyncio
async def test_champion_consensus_reaches_quorum():
    mgr_1 = ChampionConsensusManager(
        validator_id=ADDR_1,
        private_key=KEY_1,
        validators=VALIDATORS,
        quorum_bps=5000,
        timeout=0.2,
    )
    mgr_2 = ChampionConsensusManager(
        validator_id=ADDR_2,
        private_key=KEY_2,
        validators=VALIDATORS,
        quorum_bps=5000,
        timeout=0.2,
    )
    proposal = _proposal()

    task = asyncio.create_task(mgr_1.propose(proposal))
    await asyncio.sleep(0.01)
    mgr_1.receive_approval(mgr_2.sign_approval(proposal))
    result = await task

    assert result.reached is True
    assert result.quorum == 2
    assert result.collected == 2
    assert result.certificate is not None
    assert result.certificate.candidate_submission_id == "sub-final"
    assert len(result.certificate.approvals) == 2
    assert [a.validator_id.lower() for a in result.certificate.approvals] == sorted([
        ADDR_1.lower(),
        ADDR_2.lower(),
    ])


@pytest.mark.asyncio
async def test_champion_consensus_rejects_tuple_mismatch():
    mgr_1 = ChampionConsensusManager(
        validator_id=ADDR_1,
        private_key=KEY_1,
        validators=VALIDATORS,
        quorum_bps=5000,
        timeout=0.05,
    )
    mgr_2 = ChampionConsensusManager(
        validator_id=ADDR_2,
        private_key=KEY_2,
        validators=VALIDATORS,
        quorum_bps=5000,
        timeout=0.05,
    )
    proposal = _proposal()

    task = asyncio.create_task(mgr_1.propose(proposal))
    await asyncio.sleep(0.01)
    mismatched = mgr_2.sign_approval(_proposal(candidate_submission_id="sub-other"))
    assert mgr_1.receive_approval(mismatched) is None

    result = await task
    assert result.reached is False
    assert result.collected == 1
