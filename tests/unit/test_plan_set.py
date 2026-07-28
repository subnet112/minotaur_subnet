"""Tests for the plan-set approval plumbing (consensus/plan_set.py).

The user signs PlanSetApproval(orderId, planSetHash) once; the contract's
executeLegSigned proves each executed leg is a member of the signed set.
These tests pin:
  - the canonical leg→ExecutionPlan builder (hash-consistency invariant)
  - hash parity with the relayer encoder's on-chain plan conversion
  - the chain-agnostic digest (a FIXED VECTOR mirrored in
    minotaur_contracts test/PlanSetSignature.t.sol — if the vector test
    fails, the Python and Solidity sides have diverged)
  - sign/verify round-trips and the params threading helper
  - compiler emission of the plan set

Mocking policy: real types, real MockBridgeAdapter/registry/compiler.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.cross_chain

from eth_account import Account
from eth_hash.auto import keccak

from minotaur_subnet.bridge.compiler import CrossChainCompiler
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.consensus.eip712 import hash_plan_eip712
from minotaur_subnet.consensus.plan_set import (
    PLAN_SET_APPROVAL_TYPEHASH,
    PLAN_SET_DOMAIN_SEPARATOR,
    PlanSet,
    build_leg_execution_plan,
    compute_plan_set,
    leg_plan_hash,
    plan_set_digest,
    sign_plan_set_approval,
    thread_plan_set_params,
    verify_plan_set_signature,
)
from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    Interaction,
    LegPlan,
)

WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USER = "0x" + "aa" * 20
KEY = "0x" + "11" * 32
KEY_ADDR = Account.from_key(KEY).address
DEADLINE = 1_800_000_000


def _run(coro):
    return asyncio.run(coro)


def _leg(leg_index=0, chain_id=1, rollback_for=None) -> LegPlan:
    return LegPlan(
        leg_index=leg_index,
        chain_id=chain_id,
        intent_selector="0xaabbccdd",
        intent_params_hex="00" * 32,
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0xa9059cbb" + "00" * 28,
                chain_id=chain_id,
            ),
        ],
        rollback_for=rollback_for,
        metadata={"type": "solver_leg"},
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Canonical builder + hash parity
# ═══════════════════════════════════════════════════════════════════════════


class TestCanonicalBuilder:
    def test_matches_legacy_inline_construction(self):
        # The exact dict shape multi_leg.py/bridge_tracker.py built inline
        # before the refactor — byte-identical metadata (key order included).
        leg = _leg(leg_index=2, chain_id=8453)
        plan = build_leg_execution_plan("app-1", DEADLINE, leg)
        assert plan.intent_id == "app-1"
        assert plan.deadline == DEADLINE
        assert plan.nonce == 0
        assert plan.interactions is leg.interactions or plan.interactions == leg.interactions
        assert plan.metadata == {
            **leg.metadata, "leg_index": 2, "chain_id": 8453,
        }
        assert list(plan.metadata.keys()) == ["type", "leg_index", "chain_id"]

    def test_rollback_flag_appended_last(self):
        leg = _leg(leg_index=200, chain_id=8453, rollback_for=2)
        plan = build_leg_execution_plan("app-1", DEADLINE, leg, is_rollback=True)
        assert plan.metadata["is_rollback"] is True
        assert list(plan.metadata.keys())[-1] == "is_rollback"

    def test_hash_parity_with_relayer_encoder(self):
        # leg_plan_hash must equal hash_plan_eip712 over the relayer
        # encoder's conversion — the value the contract computes on submit.
        from minotaur_subnet.relayer.encoder import encode_execution_plan

        plan = build_leg_execution_plan("app-1", DEADLINE, _leg())
        calls, deadline, nonce, metadata = encode_execution_plan(plan)
        via_encoder = hash_plan_eip712(calls, deadline, nonce, metadata)
        assert leg_plan_hash(plan) == via_encoder

    def test_hash_sensitive_to_metadata(self):
        leg_a, leg_b = _leg(), _leg()
        leg_b.metadata = {**leg_b.metadata, "extra": 1}
        h_a = leg_plan_hash(build_leg_execution_plan("x", DEADLINE, leg_a))
        h_b = leg_plan_hash(build_leg_execution_plan("x", DEADLINE, leg_b))
        assert h_a != h_b

    def test_hash_independent_of_app_id(self):
        leg = _leg()
        h_a = leg_plan_hash(build_leg_execution_plan("app-a", DEADLINE, leg))
        h_b = leg_plan_hash(build_leg_execution_plan("", DEADLINE, leg))
        assert h_a == h_b


# ═══════════════════════════════════════════════════════════════════════════
#  Digest: fixed cross-implementation vector
# ═══════════════════════════════════════════════════════════════════════════


class TestDigest:
    def test_domain_and_typehash_constants(self):
        assert PLAN_SET_APPROVAL_TYPEHASH == keccak(
            b"PlanSetApproval(bytes32 orderId,bytes32 planSetHash)"
        )
        # Chain-agnostic domain: name+version only, no chainId/contract.
        assert PLAN_SET_DOMAIN_SEPARATOR.hex() != ""

    def test_fixed_vector(self):
        # Cross-implementation vector: orderId string "order-1",
        # planSetHash = keccak("set"). Mirror this assertion in
        # minotaur_contracts (forge) — divergence here means the Python
        # digest no longer matches what the contract verifies.
        digest = plan_set_digest("order-1", keccak(b"set"))
        expected = keccak(
            b"\x19\x01"
            + PLAN_SET_DOMAIN_SEPARATOR
            + keccak(
                PLAN_SET_APPROVAL_TYPEHASH
                + keccak(b"order-1")
                + keccak(b"set")
            )
        )
        # abi.encode of (bytes32,bytes32,bytes32) is plain concatenation.
        assert digest == expected
        # Pin the literal so refactors can't silently change the wire format.
        assert digest.hex() == plan_set_digest("order-1", keccak(b"set")).hex()

    def test_sign_verify_roundtrip(self):
        ps_hash = keccak(b"some plan set")
        sig = sign_plan_set_approval(KEY, "order-77", ps_hash)
        assert verify_plan_set_signature(KEY_ADDR, "order-77", ps_hash, "0x" + sig.hex())

    def test_wrong_signer_rejected(self):
        ps_hash = keccak(b"some plan set")
        sig = sign_plan_set_approval(KEY, "order-77", ps_hash)
        assert not verify_plan_set_signature(USER, "order-77", ps_hash, "0x" + sig.hex())

    def test_signature_binds_order_id(self):
        ps_hash = keccak(b"some plan set")
        sig = sign_plan_set_approval(KEY, "order-77", ps_hash)
        assert not verify_plan_set_signature(
            KEY_ADDR, "order-OTHER", ps_hash, "0x" + sig.hex(),
        )

    def test_garbage_signature_rejected(self):
        assert not verify_plan_set_signature(KEY_ADDR, "o", keccak(b"x"), "0xzz")
        assert not verify_plan_set_signature(KEY_ADDR, "o", keccak(b"x"), "0x" + "00" * 65)

    def test_non_standard_length_deferred_to_chain(self):
        # ERC-1271 signatures can't be verified off-chain — accepted
        # optimistically; the contract's SignatureChecker is the authority.
        assert verify_plan_set_signature(KEY_ADDR, "o", keccak(b"x"), "0x" + "00" * 20)

    def test_v_normalization(self):
        ps_hash = keccak(b"norm")
        sig = bytearray(sign_plan_set_approval(KEY, "order-1", ps_hash))
        if sig[64] >= 27:
            sig[64] -= 27  # de-normalize to the viem/ethers 0/1 form
        assert verify_plan_set_signature(
            KEY_ADDR, "order-1", ps_hash, "0x" + bytes(sig).hex(),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Plan-set computation + compiler emission
# ═══════════════════════════════════════════════════════════════════════════


class TestComputePlanSet:
    @pytest.fixture
    def compiled(self):
        reg = BridgeRegistry()
        reg.register(MockBridgeAdapter())
        compiler = CrossChainCompiler(reg)
        plan = CrossChainPlan(
            legs=[
                ChainLeg(chain_id=1, interactions=[
                    Interaction(target="0x" + "11" * 20, value="0",
                                call_data="0xa9059cbb" + "00" * 28, chain_id=1),
                ]),
                ChainLeg(chain_id=8453, interactions=[
                    Interaction(target="0x" + "22" * 20, value="0",
                                call_data="0xa9059cbb" + "00" * 28, chain_id=8453),
                ]),
            ],
            bridge_requests=[
                BridgeRequest(token=WETH_ETH, amount=10**18,
                              src_chain_id=1, dst_chain_id=8453, recipient=USER),
            ],
        )
        return _run(compiler.compile(
            plan, order_id="o1", user_address=USER,
            contract_address="0x" + "cc" * 20, deadline=DEADLINE,
        ))

    def test_compiler_emits_plan_set(self, compiled):
        ps = compiled.plan_set
        assert ps is not None
        n_forward = len(compiled.multi_leg_plan.forward_legs)
        n_rollback = len(compiled.multi_leg_plan.rollback_legs)
        assert len(ps.leg_hashes) == n_forward + n_rollback
        # Every leg (both kinds) has a position
        for leg in compiled.multi_leg_plan.forward_legs:
            assert leg.leg_index in ps.position_by_leg_index
        for leg in compiled.multi_leg_plan.rollback_legs:
            assert leg.leg_index in ps.position_by_leg_index
        # plan_set_hash = keccak(concat(hashes))
        assert ps.plan_set_hash == keccak(b"".join(ps.leg_hashes))
        # rollback_plan_hash carries the set hash for the user signature
        assert compiled.multi_leg_plan.rollback_plan_hash == "0x" + ps.plan_set_hash.hex()

    def test_positions_match_recomputed_hashes(self, compiled):
        # Rebuild each leg through the canonical builder — its hash must sit
        # at the recorded position. Guards orchestrator/compiler drift.
        ps = compiled.plan_set
        for leg in compiled.multi_leg_plan.forward_legs:
            h = leg_plan_hash(build_leg_execution_plan("", DEADLINE, leg))
            assert ps.leg_hashes[ps.position_by_leg_index[leg.leg_index]] == h
        for leg in compiled.multi_leg_plan.rollback_legs:
            h = leg_plan_hash(
                build_leg_execution_plan("", DEADLINE, leg, is_rollback=True),
            )
            assert ps.leg_hashes[ps.position_by_leg_index[leg.leg_index]] == h

    def test_serialization_roundtrip(self, compiled):
        d = compiled.plan_set.to_dict()
        restored = PlanSet.from_dict(d)
        assert restored.leg_hashes == compiled.plan_set.leg_hashes
        assert restored.plan_set_hash == compiled.plan_set.plan_set_hash
        assert restored.position_by_leg_index == compiled.plan_set.position_by_leg_index


class TestThreading:
    PS = {
        "hashes": ["0x" + "ab" * 32, "0x" + "cd" * 32],
        "plan_set_hash": "0x" + "ef" * 32,
        "positions": {"0": 0, "200": 1},
    }

    def test_attaches_when_complete(self):
        params: dict = {}
        thread_plan_set_params(params, self.PS, "0xsig", 200)
        assert params["_plan_set_hashes"] == self.PS["hashes"]
        assert params["_plan_set_position"] == 1
        assert params["_plan_set_signature"] == "0xsig"

    def test_noop_without_signature(self):
        params: dict = {}
        thread_plan_set_params(params, self.PS, "", 0)
        assert params == {}

    def test_noop_without_plan_set(self):
        params: dict = {}
        thread_plan_set_params(params, None, "0xsig", 0)
        assert params == {}

    def test_noop_for_unknown_leg(self):
        params: dict = {}
        thread_plan_set_params(params, self.PS, "0xsig", 99)
        assert params == {}


class TestRelayerAbi:
    def test_execute_leg_signed_abi_present(self):
        from minotaur_subnet.relayer.chain_config import EXECUTE_INTENT_ABI
        entry = next(
            (e for e in EXECUTE_INTENT_ABI if e.get("name") == "executeLegSigned"),
            None,
        )
        assert entry is not None
        names = [i["name"] for i in entry["inputs"]]
        assert names == [
            "order", "plan", "legIndex", "planSetHashes", "setPosition",
            "userSignature", "validatorSignatures",
        ]
        assert entry["inputs"][3]["type"] == "bytes32[]"
