"""Unit tests for the ABI encoder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.shared.types import ExecutionPlan, Interaction
from minotaur_subnet.relayer.encoder import (
    encode_execution_plan,
    encode_execute_intent_calldata,
    hash_execution_plan,
)


class TestEncodeExecutionPlan:
    def test_basic_plan(self):
        plan = ExecutionPlan(
            intent_id="test",
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

        result = encode_execution_plan(plan)
        calls, deadline, nonce, metadata = result

        assert len(calls) == 1
        assert calls[0][0] == "0x" + "11" * 20  # target
        assert calls[0][1] == 0  # value
        assert calls[0][2] == bytes.fromhex("deadbeef")  # calldata
        assert deadline == 1700000000
        assert nonce == 1

    def test_empty_calldata(self):
        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "00" * 20, value="0", call_data="0x"),
            ],
            deadline=0,
            nonce=0,
        )
        result = encode_execution_plan(plan)
        assert result[0][0][2] == b""

    def test_multiple_interactions(self):
        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "11" * 20, value="100", call_data="0xaa"),
                Interaction(target="0x" + "22" * 20, value="200", call_data="0xbb"),
            ],
            deadline=999,
            nonce=5,
        )
        result = encode_execution_plan(plan)
        assert len(result[0]) == 2
        assert result[0][0][1] == 100
        assert result[0][1][1] == 200

    def test_target_checksum_normalized(self):
        """Solver targets with wrong/mixed casing must be normalized.

        Regression for the 2026-07-07 Base incident: this exact
        wrong-checksum target (lowercase 'c' in '5E9bc251') made web3
        reject every executeIntent build for the order.
        """
        bad = "0x1601843c5E9bc251A3272907010AFa41Fa18347E"   # invalid EIP-55
        good = "0x1601843c5E9bC251A3272907010AFa41Fa18347E"  # canonical
        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target=bad, value="0", call_data="0x"),
                Interaction(target=good.lower(), value="0", call_data="0x"),
            ],
            deadline=0,
            nonce=0,
        )
        calls = encode_execution_plan(plan)[0]
        assert calls[0][0] == good
        assert calls[1][0] == good

    def test_target_checksum_same_plan_hash(self):
        """Normalizing target casing must not change the signed plan hash."""
        bad = "0x1601843c5E9bc251A3272907010AFa41Fa18347E"
        good = "0x1601843c5E9bC251A3272907010AFa41Fa18347E"
        mk = lambda t: ExecutionPlan(
            intent_id="test",
            interactions=[Interaction(target=t, value="0", call_data="0xaa")],
            deadline=1,
            nonce=1,
        )
        assert hash_execution_plan(mk(bad)) == hash_execution_plan(mk(good))


class TestHashExecutionPlan:
    def test_deterministic(self):
        plan = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "ab" * 20, value="0", call_data="0x"),
            ],
            deadline=1700000000,
            nonce=1,
        )
        h1 = hash_execution_plan(plan)
        h2 = hash_execution_plan(plan)
        assert h1 == h2
        assert h1.startswith("0x")

    def test_different_plans_different_hashes(self):
        plan1 = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "ab" * 20, value="0", call_data="0x"),
            ],
            deadline=1700000000,
            nonce=1,
        )
        plan2 = ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(target="0x" + "cd" * 20, value="0", call_data="0x"),
            ],
            deadline=1700000000,
            nonce=1,
        )
        assert hash_execution_plan(plan1) != hash_execution_plan(plan2)


class _StubOrder:
    """Minimal Order stand-in for encoder tests.

    The encoder only accesses a handful of attributes — this stub keeps the
    test free of SQLite/orderbook/test-fixture setup.
    """

    def __init__(
        self,
        order_id="ord_test",
        submitted_by=None,
        chain_id=8453,
        deadline=1700000000,
        params=None,
    ):
        from web3 import Web3
        self.order_id = order_id
        self.submitted_by = submitted_by or Web3.to_checksum_address("0x" + "ab" * 20)
        self.chain_id = chain_id
        self.deadline = deadline
        self.perpetual = False
        self.max_executions = 1
        self.cooldown = 0
        self.params = params or {
            "app_address": Web3.to_checksum_address("0x" + "cd" * 20),
            "intent_selector": "11223344",
            "intent_params_hex": "",
        }


class TestEncodeExecuteIntentCalldata:
    """The direct-submit flow depends on this encoder producing byte-exact
    calldata that matches what web3.py's contract.functions.executeIntent(...)
    would produce — any mismatch means the user's TX reverts on chain."""

    def _plan(self):
        from web3 import Web3
        return ExecutionPlan(
            intent_id="test",
            interactions=[
                Interaction(
                    target=Web3.to_checksum_address("0x" + "22" * 20),
                    value="0",
                    call_data="0xdeadbeef",
                    chain_id=8453,
                ),
            ],
            deadline=1700000000,
            nonce=0,
        )

    def test_returns_hex_string_with_selector(self):
        order = _StubOrder()
        plan = self._plan()
        user_sig = b"\x00" * 65
        validator_sigs = [b"\x11" * 65, b"\x22" * 65]

        calldata = encode_execute_intent_calldata(
            order, plan, user_sig, validator_sigs,
        )

        assert isinstance(calldata, str)
        assert calldata.startswith("0x")
        # 4-byte selector + encoded args → at least 10 hex chars for the
        # selector plus a lot more for the args.
        assert len(calldata) > 10
        # The first 4 bytes must be the keccak256 of the function signature.
        # Hardcoded so any accidental ABI drift is caught immediately.
        from eth_hash.auto import keccak
        expected_selector = keccak(
            (
                "executeIntent("
                "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),"
                "((address,uint256,bytes)[],uint256,uint256,bytes),"
                "bytes,bytes[])"
            ).encode()
        )[:4]
        assert calldata[2:10] == expected_selector.hex()

    def test_matches_web3_contract_encoding(self):
        """Golden test: our calldata must equal web3.py's own encoder output.

        If this ever drifts, the direct-submit path will revert on chain.
        """
        from web3 import Web3

        order = _StubOrder()
        plan = self._plan()
        user_sig = b"\xaa" * 65
        validator_sigs = [b"\xbb" * 65]

        ours = encode_execute_intent_calldata(order, plan, user_sig, validator_sigs)

        # Build the same calldata via web3.py contract.encodeABI (the
        # canonical way a dapp would construct it).
        EXECUTE_INTENT_ABI = [{
            "name": "executeIntent",
            "type": "function",
            "stateMutability": "payable",
            "inputs": [
                {
                    "name": "order", "type": "tuple",
                    "components": [
                        {"name": "orderId", "type": "bytes32"},
                        {"name": "app", "type": "address"},
                        {"name": "intentSelector", "type": "bytes4"},
                        {"name": "intentParams", "type": "bytes"},
                        {"name": "submittedBy", "type": "address"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "perpetual", "type": "bool"},
                        {"name": "maxExecutions", "type": "uint256"},
                        {"name": "cooldown", "type": "uint256"},
                    ],
                },
                {
                    "name": "plan", "type": "tuple",
                    "components": [
                        {
                            "name": "calls", "type": "tuple[]",
                            "components": [
                                {"name": "target", "type": "address"},
                                {"name": "value", "type": "uint256"},
                                {"name": "callData", "type": "bytes"},
                            ],
                        },
                        {"name": "deadline", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "metadata", "type": "bytes"},
                    ],
                },
                {"name": "userSignature", "type": "bytes"},
                {"name": "validatorSignatures", "type": "bytes[]"},
            ],
            "outputs": [],
        }]

        w3 = Web3()
        contract = w3.eth.contract(abi=EXECUTE_INTENT_ABI)

        from minotaur_subnet.relayer.encoder import (
            encode_intent_order, encode_execution_plan,
        )
        order_tuple = encode_intent_order(order)
        plan_tuple = encode_execution_plan(plan)

        web3_calldata = contract.encode_abi(
            abi_element_identifier="executeIntent",
            args=[order_tuple, plan_tuple, user_sig, validator_sigs],
        )

        assert ours == web3_calldata, (
            f"Encoder drift!\nours:  {ours}\nweb3:  {web3_calldata}"
        )

    def test_validator_sigs_order_matters(self):
        """Swapping two validator sigs must produce different calldata.

        Sort order is enforced on the server side before calling the encoder;
        the encoder must preserve whatever order it's given.
        """
        order = _StubOrder()
        plan = self._plan()
        user_sig = b"\x00" * 65

        sig_a = b"\x11" * 65
        sig_b = b"\x22" * 65
        ab = encode_execute_intent_calldata(order, plan, user_sig, [sig_a, sig_b])
        ba = encode_execute_intent_calldata(order, plan, user_sig, [sig_b, sig_a])

        assert ab != ba
