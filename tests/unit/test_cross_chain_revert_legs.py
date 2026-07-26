"""Tests for solver-authored revert legs + the unified cross-chain quote.

Covers the additive CrossChainPlan.revert_legs schema (backward-compatible
serialization), the compiler's validation/compilation of solver revert legs,
and the quote-side dry-compile (api/services/cross_chain_quote.py).

Mocking policy (matches test_cross_chain_primitive.py):
  - Real types, real MockBridgeAdapter, real BridgeRegistry, real compiler
  - No RPC / no Anvil — the mock adapter quotes locally
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.cross_chain

from minotaur_subnet.api.services.cross_chain_quote import build_cross_chain_quote
from minotaur_subnet.bridge.compiler import CrossChainCompiler, CrossChainCompileError
from minotaur_subnet.bridge.mock import MockBridgeAdapter
from minotaur_subnet.bridge.registry import BridgeRegistry
from minotaur_subnet.shared.types import (
    BridgeRequest,
    ChainLeg,
    CrossChainPlan,
    Interaction,
)

WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USER = "0x" + "aa" * 20
CONTRACT = "0x" + "cc" * 20
ACROSS_DEPOSIT_SELECTOR = "7b939232"


def _run(coro):
    return asyncio.run(coro)


def _ix(selector: str = "a9059cbb", chain_id: int = 1) -> Interaction:
    return Interaction(
        target="0x" + "11" * 20,
        value="0",
        call_data=f"0x{selector}" + "00" * 28,
        chain_id=chain_id,
    )


def _plan(revert_legs: list[ChainLeg] | None = None) -> CrossChainPlan:
    """Two-leg Ethereum→Base plan with one bridge request."""
    return CrossChainPlan(
        legs=[
            ChainLeg(chain_id=1, interactions=[_ix(chain_id=1)]),
            ChainLeg(chain_id=8453, interactions=[_ix(chain_id=8453)]),
        ],
        bridge_requests=[
            BridgeRequest(
                token=WETH_ETH,
                amount=10**18,
                src_chain_id=1,
                dst_chain_id=8453,
                recipient=USER,
            ),
        ],
        revert_legs=revert_legs or [],
    )


def _revert_leg(chain_id: int = 8453, revert_for: int = 1) -> ChainLeg:
    """A destination-chain revert leg (swap back) for solver leg 1."""
    return ChainLeg(
        chain_id=chain_id,
        interactions=[_ix(chain_id=chain_id)],
        metadata={"revert_for_leg": revert_for},
    )


@pytest.fixture
def compiler():
    reg = BridgeRegistry()
    reg.register(MockBridgeAdapter())
    return CrossChainCompiler(reg)


async def _compile(compiler, plan):
    return await compiler.compile(
        plan, order_id="o1", user_address=USER,
        contract_address=CONTRACT, deadline=0,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Schema: additive + backward compatible
# ═══════════════════════════════════════════════════════════════════════════


class TestRevertLegSchema:
    def test_to_dict_omits_empty_revert_legs(self):
        # Wire shape stays byte-identical for solvers that don't emit them.
        assert "revert_legs" not in _plan().to_dict()

    def test_legacy_dict_parses(self):
        d = _plan().to_dict()
        d.pop("revert_legs", None)
        plan = CrossChainPlan.from_dict(d)
        assert plan.revert_legs == []

    def test_roundtrip_with_revert_legs(self):
        plan = _plan(revert_legs=[_revert_leg()])
        d = plan.to_dict()
        assert len(d["revert_legs"]) == 1
        restored = CrossChainPlan.from_dict(d)
        assert len(restored.revert_legs) == 1
        assert restored.revert_legs[0].metadata["revert_for_leg"] == 1
        assert restored.revert_legs[0].chain_id == 8453


# ═══════════════════════════════════════════════════════════════════════════
#  Compiler validation
# ═══════════════════════════════════════════════════════════════════════════


class TestRevertLegValidation:
    def test_bridge_selector_in_revert_leg_rejected(self, compiler):
        leg = ChainLeg(
            chain_id=8453,
            interactions=[_ix(selector=ACROSS_DEPOSIT_SELECTOR, chain_id=8453)],
            metadata={"revert_for_leg": 1},
        )
        with pytest.raises(CrossChainCompileError, match="bridge selector"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))

    def test_missing_revert_for_leg_rejected(self, compiler):
        leg = ChainLeg(chain_id=8453, interactions=[_ix(chain_id=8453)])
        with pytest.raises(CrossChainCompileError, match="revert_for_leg"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))

    def test_revert_for_first_leg_rejected(self, compiler):
        # Leg 0 failure is atomic (nothing bridged yet) — no revert leg allowed.
        leg = ChainLeg(
            chain_id=1, interactions=[_ix(chain_id=1)],
            metadata={"revert_for_leg": 0},
        )
        with pytest.raises(CrossChainCompileError, match="out of range"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))

    def test_out_of_range_rejected(self, compiler):
        leg = ChainLeg(
            chain_id=8453, interactions=[_ix(chain_id=8453)],
            metadata={"revert_for_leg": 5},
        )
        with pytest.raises(CrossChainCompileError, match="out of range"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))

    def test_bool_revert_for_leg_rejected(self, compiler):
        leg = ChainLeg(
            chain_id=8453, interactions=[_ix(chain_id=8453)],
            metadata={"revert_for_leg": True},
        )
        with pytest.raises(CrossChainCompileError, match="integer"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))

    def test_wrong_chain_rejected(self, compiler):
        # Revert for the Base leg must execute on Base (funds sit there).
        leg = ChainLeg(
            chain_id=1, interactions=[_ix(chain_id=1)],
            metadata={"revert_for_leg": 1},
        )
        with pytest.raises(CrossChainCompileError, match="chain"):
            _run(_compile(compiler, _plan(revert_legs=[leg])))


# ═══════════════════════════════════════════════════════════════════════════
#  Compiler output
# ═══════════════════════════════════════════════════════════════════════════


class TestRevertLegCompilation:
    def test_no_revert_legs_keeps_current_behavior(self, compiler):
        compiled = _run(_compile(compiler, _plan()))
        types = [l.metadata.get("type") for l in compiled.multi_leg_plan.rollback_legs]
        assert "rollback_solver" not in types
        assert "rollback_bridge" in types  # platform reverse-bridge fallback

    def test_solver_revert_compiled_into_rollback(self, compiler):
        compiled = _run(_compile(compiler, _plan(revert_legs=[_revert_leg()])))
        rollback = compiled.multi_leg_plan.rollback_legs
        solver_rb = [l for l in rollback if l.metadata.get("type") == "rollback_solver"]
        bridge_rb = [l for l in rollback if l.metadata.get("type") == "rollback_bridge"]
        assert len(solver_rb) == 1
        assert len(bridge_rb) == 1  # reverse-bridge stays as fallback

        leg = solver_rb[0]
        # Solver leg 1 sits at forward index 2 (leg0, bridge, leg1) — the
        # orchestrator's reverse-order rollback then runs the solver revert
        # (rollback_for=2) BEFORE the reverse bridge (rollback_for=1).
        assert leg.rollback_for == 2
        assert bridge_rb[0].rollback_for == 1
        assert leg.chain_id == 8453
        assert leg.metadata["_platform_compiled"] is True
        assert leg.leg_index >= 200

    def test_forward_legs_unchanged_by_revert_legs(self, compiler):
        plain = _run(_compile(compiler, _plan()))
        with_revert = _run(_compile(compiler, _plan(revert_legs=[_revert_leg()])))
        assert (
            [l.to_dict() for l in plain.multi_leg_plan.forward_legs]
            == [l.to_dict() for l in with_revert.multi_leg_plan.forward_legs]
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Unified cross-chain quote (dry-compile)
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossChainQuote:
    def _meta(self, plan: CrossChainPlan, **extra) -> dict:
        return {"cross_chain": True, "cross_chain_plan": plan.to_dict(), **extra}

    def test_quote_payload(self, compiler):
        meta = self._meta(_plan(revert_legs=[_revert_leg()]))
        payload = _run(build_cross_chain_quote(meta, compiler))
        assert payload is not None
        # Mock adapter fee = 10 bps
        amount = 10**18
        fee = amount * 10 // 10_000
        assert payload["bridges"][0]["protocol"] == "mock"
        assert payload["bridges"][0]["fee"] == fee
        assert payload["bridge_floor"] == amount - fee
        # No declared dst_amount → bridge floor is the estimate
        assert payload["estimated_output"] == amount - fee
        assert payload["simulated"] is False
        # 3 forward legs: solver leg, bridge, solver leg
        assert [l["type"] for l in payload["legs"]] == [
            "solver_leg", "bridge", "solver_leg",
        ]
        # Revert coverage surfaced: solver revert + platform reverse-bridge
        types = {r["type"] for r in payload["revert_plan"]}
        assert types == {"rollback_solver", "rollback_bridge"}
        assert payload["escrow_deadlines"]

    def test_declared_output_wins_when_present(self, compiler):
        meta = self._meta(_plan(), dst_amount=str(5 * 10**17))
        payload = _run(build_cross_chain_quote(meta, compiler))
        assert payload["estimated_output"] == 5 * 10**17

    def test_no_compiler_returns_none(self):
        meta = self._meta(_plan())
        assert _run(build_cross_chain_quote(meta, None)) is None

    def test_no_plan_returns_none(self, compiler):
        assert _run(build_cross_chain_quote({"cross_chain": True}, compiler)) is None

    def test_invalid_plan_returns_none_not_raises(self, compiler):
        meta = {
            "cross_chain": True,
            "cross_chain_plan": {"legs": [], "bridge_requests": []},
        }
        assert _run(build_cross_chain_quote(meta, compiler)) is None
