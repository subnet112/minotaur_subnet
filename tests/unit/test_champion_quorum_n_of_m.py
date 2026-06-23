"""Real N-of-M champion quorum on a LARGER validator set (audit blocker #2).

The existing champion-consensus tests only exercise 2-of-3 / 3-of-3. This proves a
real **4-of-5** (and 3-of-5) quorum forms from INDEPENDENT follower signatures —
each follower signs the leader's exact proposal tuple with its own key, the leader
collects via the real ``receive_approval`` path, and a real ``ChampionCertificate``
is built only when the threshold is met. Fully in-process: real eth keys + real
EIP-712 sign/verify, no Docker/Anvil (the validator set + quorum are pinned, so no
on-chain ValidatorRegistry read is needed).

Mirrors tests/unit/test_champion_consensus.py's construction, scaled to 5 signers
and extended with the dissent / unauthorized / tamper / dedup failure modes.
"""

from __future__ import annotations

import asyncio

import pytest

from minotaur_subnet.consensus.champion_manager import (
    ChampionConsensusManager,
    ChampionProposal,
)
from minotaur_subnet.consensus.eip712 import address_from_key


# Five standard Anvil keys (a real 5-validator set) + a sixth OUTSIDER not in it.
KEYS = [
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da4dc3dd9b21d8c6da4e0e7f31b7b584f7",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e8175e8fff7f3",
]
ADDRS = [address_from_key(k) for k in KEYS]
VALIDATORS = list(ADDRS)

OUTSIDER_KEY = "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e"
OUTSIDER_ADDR = address_from_key(OUTSIDER_KEY)

# quorum_required = ceil(N * bps / 10000); for N=5: 7000 -> 4, 6000 -> 3.
BPS_4_OF_5 = 7000
BPS_3_OF_5 = 6000


def _managers(quorum_bps: int, timeout: float = 0.3) -> list[ChampionConsensusManager]:
    return [
        ChampionConsensusManager(
            validator_id=addr, private_key=key,
            validators=VALIDATORS, quorum_bps=quorum_bps, timeout=timeout,
        )
        for addr, key in zip(ADDRS, KEYS)
    ]


def _proposal(pack: str = "cd") -> ChampionProposal:
    return ChampionProposal(
        round_id="round-nofm",
        committee_hash="0x" + "ab" * 32,
        incumbent_image_id="sha256:" + "1" * 64,
        candidate_submission_id="sub-final",
        candidate_image_id="sha256:" + "2" * 64,
        benchmark_pack_hash="0x" + pack * 32,
        shadow_case_log_hash="0x" + "ef" * 32,
        effective_epoch=43,
    )


async def _drive(managers, proposal, signer_idxs):
    """Leader (idx 0) proposes; followers in signer_idxs sign + are fed in. Returns
    (result, per_feed_results) where per_feed is receive_approval's return each call."""
    leader = managers[0]
    task = asyncio.create_task(leader.propose(proposal))
    await asyncio.sleep(0.01)  # let propose() register the pending entry + self-approve
    feeds = []
    for i in signer_idxs:
        feeds.append(leader.receive_approval(managers[i].sign_approval(proposal)))
    return await task, feeds


# ── threshold behavior on a 5-validator set ─────────────────────────────────


@pytest.mark.asyncio
async def test_four_of_five_reaches_quorum():
    mgrs = _managers(BPS_4_OF_5)
    proposal = _proposal()
    result, _ = await _drive(mgrs, proposal, signer_idxs=[1, 2, 3])  # leader + 3 = 4

    assert result.reached is True
    assert result.quorum == 4
    assert result.collected == 4
    assert result.certificate is not None
    assert len(result.certificate.approvals) == 4
    # every collected approval independently verifies against the proposal
    leader = mgrs[0]
    assert all(leader.verify_approval(a, proposal) for a in result.certificate.approvals)


@pytest.mark.asyncio
async def test_three_of_five_below_threshold_does_not_reach():
    mgrs = _managers(BPS_4_OF_5)  # needs 4
    proposal = _proposal()
    result, _ = await _drive(mgrs, proposal, signer_idxs=[1, 2])  # leader + 2 = 3 < 4

    assert result.reached is False
    assert result.quorum == 4
    assert result.collected == 3
    assert result.certificate is None


@pytest.mark.asyncio
async def test_five_of_five_unanimous():
    mgrs = _managers(BPS_4_OF_5)
    proposal = _proposal()
    result, _ = await _drive(mgrs, proposal, signer_idxs=[1, 2, 3, 4])  # all 5

    assert result.reached is True
    assert result.collected == 5
    assert len(result.certificate.approvals) == 5


@pytest.mark.asyncio
async def test_three_of_five_threshold_reaches_at_exactly_three():
    mgrs = _managers(BPS_3_OF_5)  # needs 3
    proposal = _proposal()
    result, _ = await _drive(mgrs, proposal, signer_idxs=[1, 2])  # leader + 2 = 3

    assert result.reached is True
    assert result.quorum == 3
    assert result.collected == 3


# ── independent-signature integrity: who counts toward quorum ───────────────


@pytest.mark.asyncio
async def test_unauthorized_signer_does_not_count_toward_quorum():
    # leader + 2 in-set followers (=3) + an OUTSIDER not in the validator set.
    # The outsider's signature is cryptographically valid but the signer is not
    # authorized -> rejected -> quorum (4) NOT reached.
    mgrs = _managers(BPS_4_OF_5)
    proposal = _proposal()
    leader = mgrs[0]
    outsider = ChampionConsensusManager(
        validator_id=OUTSIDER_ADDR, private_key=OUTSIDER_KEY,
        validators=[OUTSIDER_ADDR], quorum_bps=5000, timeout=0.3,
    )
    task = asyncio.create_task(leader.propose(proposal))
    await asyncio.sleep(0.01)
    leader.receive_approval(mgrs[1].sign_approval(proposal))
    leader.receive_approval(mgrs[2].sign_approval(proposal))
    outsider_feed = leader.receive_approval(outsider.sign_approval(proposal))
    result = await task

    assert outsider_feed is None, "an unauthorized signer must be rejected"
    assert result.reached is False
    assert result.collected == 3  # leader + 2 authorized; outsider not counted
    assert not leader.verify_approval(outsider.sign_approval(proposal), proposal)


@pytest.mark.asyncio
async def test_tampered_tuple_signature_does_not_count():
    # A follower signs a DIFFERENT proposal (different benchmark_pack_hash). It must
    # not count toward the leader's proposal -> with one tampered follower the 4th
    # vote is missing -> quorum not reached.
    mgrs = _managers(BPS_4_OF_5)
    leader = mgrs[0]
    proposal = _proposal(pack="cd")
    tampered = _proposal(pack="99")  # same round_id, different pack hash
    task = asyncio.create_task(leader.propose(proposal))
    await asyncio.sleep(0.01)
    leader.receive_approval(mgrs[1].sign_approval(proposal))
    leader.receive_approval(mgrs[2].sign_approval(proposal))
    tamper_feed = leader.receive_approval(mgrs[3].sign_approval(tampered))
    result = await task

    assert tamper_feed is None, "an approval over a different tuple must be rejected"
    assert result.reached is False
    assert result.collected == 3


@pytest.mark.asyncio
async def test_duplicate_signer_is_deduped():
    # The same follower signs twice; the second must not inflate the tally.
    mgrs = _managers(BPS_4_OF_5)
    leader = mgrs[0]
    proposal = _proposal()
    task = asyncio.create_task(leader.propose(proposal))
    await asyncio.sleep(0.01)
    leader.receive_approval(mgrs[1].sign_approval(proposal))
    leader.receive_approval(mgrs[1].sign_approval(proposal))  # duplicate signer
    result = await task

    assert result.reached is False
    assert result.collected == 2  # leader + mgr1 once, NOT 3


# ── certificate integrity ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_certificate_approvals_sorted_and_bound_to_proposal():
    mgrs = _managers(BPS_4_OF_5)
    proposal = _proposal()
    result, _ = await _drive(mgrs, proposal, signer_idxs=[3, 1, 2])  # fed out of order

    ids = [a.validator_id.lower() for a in result.certificate.approvals]
    assert ids == sorted(ids), "certificate approvals must be address-ascending"
    assert result.certificate.candidate_submission_id == "sub-final"
    assert result.certificate.benchmark_pack_hash == proposal.benchmark_pack_hash
    # a certificate approval must NOT verify against an unrelated proposal
    other = _proposal(pack="77")
    leader = mgrs[0]
    assert not leader.verify_approval(result.certificate.approvals[0], other)
