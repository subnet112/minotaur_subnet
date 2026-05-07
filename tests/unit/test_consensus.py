"""Unit tests for the consensus module."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.consensus.signatures import (
    hash_plan,
    sign_plan_approval,
    verify_plan_approval,
)
from minotaur_subnet.consensus.eip712 import address_from_key, build_domain_separator
from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.shared.types import ExecutionPlan, Interaction


# ── Test keys (Anvil deterministic keys) ─────────────────────────────────────

VALIDATOR_1_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
VALIDATOR_1_ADDR = address_from_key(VALIDATOR_1_KEY)
VALIDATOR_2_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
VALIDATOR_2_ADDR = address_from_key(VALIDATOR_2_KEY)
VALIDATOR_3_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
VALIDATOR_3_ADDR = address_from_key(VALIDATOR_3_KEY)

# Default domain for tests
DOMAIN = build_domain_separator(31337, "0x" + "00" * 20)


@pytest.fixture
def sample_plan():
    return ExecutionPlan(
        intent_id="test_intent",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0xdeadbeef",
                chain_id=1,
            ),
        ],
        deadline=1700000000,
        nonce=1,
    )


class TestHashPlan:
    def test_deterministic(self, sample_plan):
        h1 = hash_plan(sample_plan)
        h2 = hash_plan(sample_plan)
        assert h1 == h2
        assert h1.startswith("0x")
        assert len(h1) == 66  # 0x + 64 hex chars

    def test_different_plans_different_hashes(self, sample_plan):
        h1 = hash_plan(sample_plan)

        plan2 = ExecutionPlan(
            intent_id="different",
            interactions=[
                Interaction(
                    target="0x" + "22" * 20,
                    value="0",
                    call_data="0xdeadbeef",
                    chain_id=1,
                ),
            ],
            deadline=sample_plan.deadline,
            nonce=sample_plan.nonce,
        )
        h2 = hash_plan(plan2)
        assert h1 != h2


class TestSignVerify:
    def test_sign_and_verify_roundtrip(self, sample_plan):
        plan_hash = hash_plan(sample_plan)
        sig = sign_plan_approval(
            VALIDATOR_1_KEY, "order_1", plan_hash, 0.85,
            domain_separator=DOMAIN,
        )
        assert isinstance(sig, str)
        assert len(sig) > 0

        # Verify the signature
        assert verify_plan_approval(
            VALIDATOR_1_ADDR, sig, "order_1", plan_hash, 0.85,
            domain_separator=DOMAIN,
        )

    def test_different_orders_different_sigs(self, sample_plan):
        plan_hash = hash_plan(sample_plan)
        sig1 = sign_plan_approval(
            VALIDATOR_1_KEY, "order_1", plan_hash, 0.85,
            domain_separator=DOMAIN,
        )
        sig2 = sign_plan_approval(
            VALIDATOR_1_KEY, "order_2", plan_hash, 0.85,
            domain_separator=DOMAIN,
        )
        assert sig1 != sig2

    def test_different_scores_different_sigs(self, sample_plan):
        plan_hash = hash_plan(sample_plan)
        sig1 = sign_plan_approval(
            VALIDATOR_1_KEY, "order_1", plan_hash, 0.50,
            domain_separator=DOMAIN,
        )
        sig2 = sign_plan_approval(
            VALIDATOR_1_KEY, "order_1", plan_hash, 0.60,
            domain_separator=DOMAIN,
        )
        assert sig1 != sig2


class TestConsensusManagerSingleValidator:
    @pytest.mark.asyncio
    async def test_single_validator_auto_approve(self, sample_plan):
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
        )
        plan_hash = hash_plan(sample_plan)
        result = await cm.propose("order_1", sample_plan, 0.85, plan_hash)

        assert result.reached is True
        assert result.collected == 1
        assert result.quorum == 1
        assert result.combined_score == 0.85
        assert len(result.approvals) == 1
        assert result.approvals[0].validator_id == VALIDATOR_1_ADDR

    @pytest.mark.asyncio
    async def test_approval_has_signature(self, sample_plan):
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
        )
        plan_hash = hash_plan(sample_plan)
        approval = cm.sign_approval("order_1", plan_hash, 0.85)
        assert approval.signature
        assert approval.order_id == "order_1"
        assert approval.plan_hash == plan_hash
        assert approval.score == 0.85


class TestConsensusManagerMultiValidator:
    @pytest.mark.asyncio
    async def test_quorum_not_reached_with_one(self, sample_plan):
        validators = [VALIDATOR_1_ADDR, VALIDATOR_2_ADDR, VALIDATOR_3_ADDR]
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
            quorum_bps=8000,  # 80% of 3 = 3 required (ceil)
            validators=validators,
        )
        plan_hash = hash_plan(sample_plan)
        result = await cm.propose("order_1", sample_plan, 0.85, plan_hash)

        # Only 1 of 3 signed, quorum requires 3
        assert result.reached is False
        assert result.collected == 1

    def test_quorum_calculation(self):
        cm = ConsensusManager(
            validator_id="v1",
            private_key=VALIDATOR_1_KEY,
            quorum_bps=8000,
            validators=["v1", "v2", "v3"],
        )
        # 80% of 3 = 2.4, ceil = 3
        assert cm.quorum_required == 3

        cm2 = ConsensusManager(
            validator_id="v1",
            private_key=VALIDATOR_1_KEY,
            quorum_bps=6700,  # 67%
            validators=["v1", "v2", "v3"],
        )
        # 67% of 3 = 2.01, ceil = 3
        assert cm2.quorum_required == 3

        cm3 = ConsensusManager(
            validator_id="v1",
            private_key=VALIDATOR_1_KEY,
            quorum_bps=5000,  # 50%
            validators=["v1", "v2", "v3", "v4"],
        )
        # 50% of 4 = 2
        assert cm3.quorum_required == 2


class TestPruneExpired:
    @pytest.mark.asyncio
    async def test_prune_removes_timed_out(self, sample_plan):
        validators = [VALIDATOR_1_ADDR, VALIDATOR_2_ADDR]
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
            quorum_bps=10000,
            validators=validators,
            timeout=0.1,  # Short timeout for fast test
        )
        plan_hash = hash_plan(sample_plan)
        # Multi-validator propose blocks until timeout (no quorum reached)
        result = await cm.propose("order_1", sample_plan, 0.85, plan_hash)
        assert not result.reached

        # After timeout, proposal is still in _pending and can be pruned
        expired = await cm.prune_expired()
        assert "order_1" in expired


class TestClearAllPending:
    """Tests for clear_all_pending (CON-15: leader change cleanup)."""

    @pytest.mark.asyncio
    async def test_clear_all_pending_removes_all(self, sample_plan):
        from minotaur_subnet.consensus.manager import _PendingProposal
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
        )
        # Manually add proposals
        cm._pending["order_A"] = _PendingProposal(
            order_id="order_A", plan_hash="0x00", score=0.5, quorum=3,
        )
        cm._pending["order_B"] = _PendingProposal(
            order_id="order_B", plan_hash="0x01", score=0.6, quorum=3,
        )
        assert len(cm._pending) == 2
        count = await cm.clear_all_pending()
        assert count == 2
        assert len(cm._pending) == 0

    @pytest.mark.asyncio
    async def test_clear_all_pending_empty(self):
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
        )
        count = await cm.clear_all_pending()
        assert count == 0


class TestReceiveApproval:
    @pytest.mark.asyncio
    async def test_receive_from_non_validator(self, sample_plan):
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
            validators=[VALIDATOR_1_ADDR],
        )
        plan_hash = hash_plan(sample_plan)
        await cm.propose("order_1", sample_plan, 0.85, plan_hash)

        # Create approval from unknown validator
        from minotaur_subnet.shared.types import SignedApproval
        fake = SignedApproval(
            validator_id="unknown",
            order_id="order_1",
            plan_hash=plan_hash,
            score=0.85,
            signature="fake",
        )
        result = await cm.receive_approval(fake)
        assert result is None

    @pytest.mark.asyncio
    async def test_receive_for_unknown_order(self, sample_plan):
        cm = ConsensusManager(
            validator_id=VALIDATOR_1_ADDR,
            private_key=VALIDATOR_1_KEY,
        )
        from minotaur_subnet.shared.types import SignedApproval
        fake = SignedApproval(
            validator_id=VALIDATOR_1_ADDR,
            order_id="nonexistent",
            plan_hash="0x" + "00" * 32,
            score=0.85,
            signature="fake",
        )
        result = await cm.receive_approval(fake)
        assert result is None
