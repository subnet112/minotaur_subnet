"""Unit tests for the EIP-712 champion-approval v2 signed fields.

The ChampionApproval struct was extended (v2) with three replay/commit
binding fields that are part of the *signed* EIP-712 digest:

    commit_hash  -- binds the git SHA of the champion submission
    nonce        -- per-signer monotonic replay protection
    deadline     -- caps signature lifetime

These tests assert that a real signature genuinely BINDS each of those
fields (tampering any one breaks ``verify_approval`` even when the
envelope copy is tampered to match), and that an approval signed over a
DIFFERENT proposal tuple (e.g. a different ``benchmark_pack_hash`` or
``candidate_image_id``) fails verification.

The existing ``test_eip712.py`` only covers the *plan*-approval path
(``sign_plan_approval_eip712`` / ``verify_plan_approval_eip712``); it has
no champion-approval coverage. ``test_champion_consensus.py`` exercises
quorum collection and a single ``receive_approval`` tuple mismatch on
``candidate_submission_id`` -- but its ``_proposal`` helper never sets the
v2 fields and it never calls ``verify_approval`` directly to prove the
*signature* (not just the envelope-equality check) binds the tuple. These
tests fill that gap.

Pure Python crypto -- no Anvil, Docker, network, or chain.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.champion_manager import (
    CHAMPION_APPROVAL_TYPEHASH,
    ChampionConsensusManager,
    ChampionProposal,
    hash_champion_approval_struct,
)
from minotaur_subnet.consensus.eip712 import address_from_key

# ── Test keys (Anvil-derived, same pattern as test_champion_consensus.py) ─────

KEY_1 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
KEY_2 = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"

ADDR_1 = address_from_key(KEY_1)
ADDR_2 = address_from_key(KEY_2)
VALIDATORS = [ADDR_1, ADDR_2]


def _proposal(**overrides) -> ChampionProposal:
    """A fully-populated proposal, including the v2 fields."""
    base = dict(
        round_id="round-e42-n1",
        committee_hash="0x" + "ab" * 32,
        incumbent_image_id="sha256:" + "1" * 64,
        candidate_submission_id="sub-final",
        candidate_image_id="sha256:" + "2" * 64,
        benchmark_pack_hash="0x" + "cd" * 32,
        shadow_case_log_hash="0x" + "ef" * 32,
        effective_epoch=43,
        commit_hash="0x" + "aa" * 32,
        nonce=7,
        deadline=2_000_000_000,
    )
    base.update(overrides)
    return ChampionProposal(**base)


def _manager(validator_id: str, private_key: str) -> ChampionConsensusManager:
    return ChampionConsensusManager(
        validator_id=validator_id,
        private_key=private_key,
        validators=VALIDATORS,
        quorum_bps=5000,
        timeout=0.05,
    )


@pytest.fixture
def signer():
    """The validator that signs approvals (ADDR_2)."""
    return _manager(ADDR_2, KEY_2)


@pytest.fixture
def verifier():
    """A separate validator that verifies ADDR_2's approvals (ADDR_1)."""
    return _manager(ADDR_1, KEY_1)


# ── Typehash structure ───────────────────────────────────────────────────────


class TestChampionApprovalTypehash:
    def test_typehash_is_bytes32(self):
        assert isinstance(CHAMPION_APPROVAL_TYPEHASH, bytes)
        assert len(CHAMPION_APPROVAL_TYPEHASH) == 32

    def test_typehash_includes_v2_fields(self):
        """The v2 signed struct must commit to commitHash/nonce/deadline."""
        from eth_hash.auto import keccak

        # Recompute the typehash from the v2 field layout. If the source
        # drops or reorders a v2 field, this manual computation diverges.
        expected = keccak(
            b"ChampionApproval("
            b"bytes32 roundId,"
            b"bytes32 committeeHash,"
            b"bytes32 incumbentImageId,"
            b"bytes32 candidateSubmissionId,"
            b"bytes32 candidateImageId,"
            b"bytes32 benchmarkPackHash,"
            b"bytes32 shadowCaseLogHash,"
            b"uint256 effectiveEpoch,"
            b"bytes32 commitHash,"
            b"uint256 nonce,"
            b"uint256 deadline"
            b")"
        )
        assert CHAMPION_APPROVAL_TYPEHASH == expected


# ── Struct hash is sensitive to each v2 field ────────────────────────────────


class TestStructHashBindsV2Fields:
    """The hashed struct (pre-signing) must change when a v2 field changes."""

    def test_clean_hash_is_deterministic(self):
        h1 = hash_champion_approval_struct(_proposal())
        h2 = hash_champion_approval_struct(_proposal())
        assert h1 == h2
        assert len(h1) == 32

    def test_commit_hash_changes_struct_hash(self):
        h1 = hash_champion_approval_struct(_proposal(commit_hash="0x" + "aa" * 32))
        h2 = hash_champion_approval_struct(_proposal(commit_hash="0x" + "bb" * 32))
        assert h1 != h2

    def test_nonce_changes_struct_hash(self):
        h1 = hash_champion_approval_struct(_proposal(nonce=7))
        h2 = hash_champion_approval_struct(_proposal(nonce=8))
        assert h1 != h2

    def test_deadline_changes_struct_hash(self):
        h1 = hash_champion_approval_struct(_proposal(deadline=2_000_000_000))
        h2 = hash_champion_approval_struct(_proposal(deadline=2_000_000_001))
        assert h1 != h2


# ── verify_approval round-trip + signature binding of v2 fields ──────────────


class TestVerifyApprovalBindsV2Fields:
    """A real signature must BIND commit_hash / nonce / deadline.

    Each test tampers the value in BOTH the envelope copy and the proposal
    passed to verify_approval, so the envelope-equality pre-check
    (``_approval_matches_proposal``) PASSES -- isolating the *signature*
    recovery as the thing that must reject the tamper. If verification
    only checked envelope equality, these would (wrongly) pass.
    """

    def test_clean_roundtrip_verifies(self, signer, verifier):
        proposal = _proposal()
        approval = signer.sign_approval(proposal)
        assert verifier.verify_approval(approval, proposal) is True

    def test_tampered_commit_hash_breaks_signature(self, signer, verifier):
        proposal = _proposal()
        approval = signer.sign_approval(proposal)

        forged_proposal = _proposal(commit_hash="0x" + "bb" * 32)
        forged_approval = dataclasses.replace(approval, commit_hash="0x" + "bb" * 32)

        assert verifier.verify_approval(forged_approval, forged_proposal) is False

    def test_tampered_nonce_breaks_signature(self, signer, verifier):
        proposal = _proposal()
        approval = signer.sign_approval(proposal)

        forged_proposal = _proposal(nonce=99)
        forged_approval = dataclasses.replace(approval, nonce=99)

        assert verifier.verify_approval(forged_approval, forged_proposal) is False

    def test_tampered_deadline_breaks_signature(self, signer, verifier):
        proposal = _proposal()
        approval = signer.sign_approval(proposal)

        new_deadline = proposal.deadline + 12345
        forged_proposal = _proposal(deadline=new_deadline)
        forged_approval = dataclasses.replace(approval, deadline=new_deadline)

        assert verifier.verify_approval(forged_approval, forged_proposal) is False


# ── Approval over a DIFFERENT proposal tuple fails verify_approval ───────────


class TestVerifyApprovalRejectsDifferentProposal:
    """An approval signed for proposal A must not verify against proposal B."""

    def test_different_benchmark_pack_hash_fails(self, signer, verifier):
        signed_for = _proposal(benchmark_pack_hash="0x" + "cd" * 32)
        approval = signer.sign_approval(signed_for)

        other = _proposal(benchmark_pack_hash="0x" + "11" * 32)
        assert verifier.verify_approval(approval, other) is False

    def test_different_candidate_image_id_fails(self, signer, verifier):
        signed_for = _proposal(candidate_image_id="sha256:" + "2" * 64)
        approval = signer.sign_approval(signed_for)

        other = _proposal(candidate_image_id="sha256:" + "9" * 64)
        assert verifier.verify_approval(approval, other) is False

    def test_different_candidate_submission_id_fails(self, signer, verifier):
        signed_for = _proposal(candidate_submission_id="sub-final")
        approval = signer.sign_approval(signed_for)

        other = _proposal(candidate_submission_id="sub-evil")
        assert verifier.verify_approval(approval, other) is False

    def test_different_effective_epoch_fails(self, signer, verifier):
        signed_for = _proposal(effective_epoch=43)
        approval = signer.sign_approval(signed_for)

        other = _proposal(effective_epoch=44)
        assert verifier.verify_approval(approval, other) is False


# ── verify_approval reconstructs the tuple from the envelope when no
#    proposal is passed (proposal_from_approval path) ────────────────────────


class TestVerifyApprovalEnvelopeOnly:
    """With no explicit proposal, verify_approval rebuilds it from the
    approval envelope -- the v2 fields must survive that round-trip."""

    def test_envelope_only_roundtrip_verifies(self, signer, verifier):
        proposal = _proposal(commit_hash="0x" + "3c" * 32, nonce=11, deadline=1_900_000_000)
        approval = signer.sign_approval(proposal)
        # No proposal arg -> proposal_from_approval(approval) is used.
        assert verifier.verify_approval(approval) is True

    def test_envelope_only_with_tampered_signed_field_fails(self, signer, verifier):
        """Mutating a signed v2 field on the envelope alone (so the
        reconstructed proposal carries the lie) must fail signature
        recovery -- the signature was minted over the original value."""
        proposal = _proposal(nonce=11)
        approval = signer.sign_approval(proposal)

        # The reconstructed proposal will carry nonce=999, but the signature
        # was produced over nonce=11 -> recovery yields a different address.
        forged_approval = dataclasses.replace(approval, nonce=999)
        assert verifier.verify_approval(forged_approval) is False
