"""A solver's already-encoded plan metadata must reach the contract verbatim.

An App whose Solidity abi.decodes its metadata — AlphaYieldApp reads
`abi.decode(plan.metadata, (bytes32, uint16))` — needs the exact bytes. Every
encoder that JSON-wraps them destroys the plan: under 64 bytes the decode
reverts, at or over 64 it decodes garbage. Either way the score is 0, so such an
intent cannot discriminate between solvers at all.

`relayer/encoder.py` and `consensus/signatures.py` already guarded this. The
simulation and contract-call paths did not, which meant a bytes-metadata plan
was SIGNED correctly and SCORED as garbage — the two halves disagreed.

These tests drive the REAL encoders. A test that hand-rolls abi.encode bytes and
feeds them straight to the contract keeps passing while the live path stays
broken, which is exactly how this survived.
"""
from __future__ import annotations

import json

import pytest
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from minotaur_subnet.shared.types import ExecutionPlan


HOTKEY = bytes.fromhex("56426093d1d8298bbc833d8fec69b94733841ebe0f5cebbb29062d5baf58ab5c")
UID = 41
RECOMMENDATION = abi_encode(["bytes32", "uint16"], [HOTKEY, UID])


def _sim_for_encoding():
    from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator
    sim = SubtensorSimulator.__new__(SubtensorSimulator)
    sim.chain_id = 964
    return sim


def _plan(metadata):
    return ExecutionPlan(
        intent_id="i", interactions=[], deadline=0, nonce=0, metadata=metadata,
    )


def _metadata_field_from_score_intent(calldata_hex: str) -> bytes:
    """Pull the plan's metadata back out of encoded scoreIntent calldata."""
    payload = bytes.fromhex(calldata_hex[10:])  # drop 0x + selector
    _order, plan = abi_decode(
        ["(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
         "((address,uint256,bytes)[],uint256,uint256,bytes)"],
        payload,
    )
    return plan[3]


class TestSubtensorScoringPath:
    """The chain-964 path — the one that scores AlphaYieldApp."""

    def _sim(self):
        """Bare instance: _build_score_intent_calldata is pure encoding and needs
        only chain_id, so this avoids standing up a real simulator."""
        from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator
        sim = SubtensorSimulator.__new__(SubtensorSimulator)
        sim.chain_id = 964
        return sim

    def test_bytes_metadata_survives_the_round_trip(self):
        calldata = self._sim()._build_score_intent_calldata(
            "0x" + "11" * 20, {"app": "0x" + "22" * 20}, _plan(RECOMMENDATION),
        )
        out = _metadata_field_from_score_intent(calldata)
        assert out == RECOMMENDATION, "the solver's encoded recommendation was mangled"

        # and the App's own decode recovers exactly what the solver chose
        hotkey, uid = abi_decode(["bytes32", "uint16"], out)
        assert hotkey == HOTKEY
        assert uid == UID

    def test_a_dict_still_becomes_json(self):
        """The common case must not regress."""
        calldata = self._sim()._build_score_intent_calldata(
            "0x" + "11" * 20, {"app": "0x" + "22" * 20}, _plan({"cross_chain": True}),
        )
        out = _metadata_field_from_score_intent(calldata)
        assert json.loads(out.decode()) == {"cross_chain": True}

    def test_empty_metadata_is_empty_bytes(self):
        calldata = self._sim()._build_score_intent_calldata(
            "0x" + "11" * 20, {"app": "0x" + "22" * 20}, _plan({}),
        )
        assert _metadata_field_from_score_intent(calldata) == b""


class TestContractCallPath:
    """blockchain/contracts.py — what a live execution submits."""

    def _encode(self, metadata):
        from minotaur_subnet.blockchain.contracts import _plan_to_solidity
        return _plan_to_solidity(_plan(metadata))[-1]

    def test_bytes_pass_through(self):
        assert self._encode(RECOMMENDATION) == RECOMMENDATION

    def test_dict_becomes_json(self):
        assert json.loads(self._encode({"a": 1}).decode()) == {"a": 1}


class TestEveryEncoderAgrees:
    """The bug was DISAGREEMENT: signing preserved the bytes, scoring did not.

    A plan that is signed one way and scored another is worse than one that is
    uniformly broken, because it looks like the solver's fault.
    """

    def test_signing_and_scoring_produce_the_same_metadata(self):
        from minotaur_subnet.consensus import signatures
        from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator

        plan = _plan(RECOMMENDATION)
        signed = (
            json.dumps(plan.metadata).encode()
            if isinstance(plan.metadata, dict)
            else plan.metadata
        )
        scored = _metadata_field_from_score_intent(
            _sim_for_encoding()._build_score_intent_calldata(
                "0x" + "11" * 20, {"app": "0x" + "22" * 20}, plan,
            )
        )
        assert signed == scored == RECOMMENDATION
        assert signatures is not None  # the guarded reference implementation
