"""Shared DexAggregator E2E test helpers.

These helpers keep the flagship swap-order encoding/signing/funding flow in one
place so E2E tests do not drift from the current DexAggregatorApp contract
layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak
from web3 import Web3

from minotaur_subnet.consensus.eip712 import address_from_key, sign_user_order
from minotaur_subnet.shared.types import AppStatus, DeploymentResult, ExecutionPlan

ZERO_BYTES32 = b"\x00" * 32
DEX_SWAP_INTENT_PARAM_TYPES = [
    "address",
    "address",
    "uint256",
    "uint256",
    "address",
    "uint256",
    "uint8",
    "bytes32",
    "bytes32",
]


@dataclass(frozen=True)
class SignedDexSwapOrder:
    order_id_bytes: bytes
    swap_selector: bytes
    intent_params: bytes
    user_signature: bytes
    deadline: int
    user_nonce: int


def send_signed_tx(
    w3: Web3,
    fn: Any,
    private_key: str,
    *,
    chain_id: int,
    gas: int = 500_000,
) -> dict[str, Any]:
    """Build, sign, send, and wait for a contract-function transaction."""
    addr = w3.to_checksum_address(address_from_key(private_key))
    tx = fn.build_transaction(
        {
            "from": addr,
            "nonce": w3.eth.get_transaction_count(addr),
            "gas": gas,
            "chainId": chain_id,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)


def fund_and_approve_erc20(
    w3: Web3,
    *,
    token_address: str,
    token_abi: list[dict[str, Any]],
    recipient: str,
    spender: str,
    amount: int,
    funder_key: str,
    owner_key: str,
    chain_id: int,
) -> None:
    """Mint ERC-20 test funds to a user and approve the app to pull them."""
    token = w3.eth.contract(address=token_address, abi=token_abi)
    send_signed_tx(
        w3,
        token.functions.mint(recipient, amount),
        funder_key,
        chain_id=chain_id,
    )
    send_signed_tx(
        w3,
        token.functions.approve(spender, amount),
        owner_key,
        chain_id=chain_id,
    )


def build_dex_swap_intent_params(
    *,
    input_token: str,
    output_token: str,
    input_amount: int,
    min_output_amount: int,
    receiver: str,
    permit_deadline: int = 0,
    permit_v: int = 0,
    permit_r: bytes = ZERO_BYTES32,
    permit_s: bytes = ZERO_BYTES32,
) -> bytes:
    """Encode the current 9-field DexAggregatorApp intent payload."""
    return abi_encode(
        DEX_SWAP_INTENT_PARAM_TYPES,
        [
            input_token,
            output_token,
            input_amount,
            min_output_amount,
            receiver,
            permit_deadline,
            permit_v,
            permit_r,
            permit_s,
        ],
    )


def build_orderbook_dex_swap_params(
    *,
    input_token: str,
    output_token: str,
    input_amount: int,
    min_output_amount: int,
    app_address: str,
) -> dict[str, str]:
    """Build the common raw params payload used in orderbook-backed E2E tests."""
    return {
        "input_token": input_token,
        "output_token": output_token,
        "input_amount": str(input_amount),
        "min_output_amount": str(min_output_amount),
        "output_amount": str(min_output_amount),
        "app_address": app_address,
    }


def save_active_deployment(
    app_store: Any,
    *,
    app_id: str,
    contract_address: str,
    chain_id: int,
) -> None:
    """Record an active on-chain deployment for an E2E app fixture."""
    app_store.save_deployment(
        DeploymentResult(
            app_id=app_id,
            status=AppStatus.ACTIVE,
            contract_address=contract_address,
            chain_id=chain_id,
        )
    )


def sign_dex_swap_order(
    *,
    w3: Web3,
    app_address: str,
    app_abi: list[dict[str, Any]],
    user_key: str,
    submitted_by: str,
    domain_separator: bytes,
    chain_id: int,
    order_id_bytes: bytes,
    user_nonce: int,
    input_token: str,
    output_token: str,
    input_amount: int,
    min_output_amount: int,
    receiver: str,
    perpetual: bool = False,
    max_executions: int = 1,
    cooldown: int = 0,
) -> SignedDexSwapOrder:
    """Build the current DexAggregator payload and sign the user order."""
    swap_selector = w3.eth.contract(
        address=app_address,
        abi=app_abi,
    ).functions.SWAP_SELECTOR().call()
    intent_params = build_dex_swap_intent_params(
        input_token=input_token,
        output_token=output_token,
        input_amount=input_amount,
        min_output_amount=min_output_amount,
        receiver=receiver,
    )
    deadline = w3.eth.get_block("latest")["timestamp"] + 3600
    user_signature = sign_user_order(
        private_key=user_key,
        order_id=order_id_bytes,
        app=app_address,
        intent_selector=swap_selector,
        intent_params=intent_params,
        submitted_by=submitted_by,
        chain_id=chain_id,
        deadline=deadline,
        nonce=user_nonce,
        perpetual=perpetual,
        max_executions=max_executions,
        cooldown=cooldown,
        domain_separator=domain_separator,
    )
    return SignedDexSwapOrder(
        order_id_bytes=order_id_bytes,
        swap_selector=swap_selector,
        intent_params=intent_params,
        user_signature=user_signature,
        deadline=deadline,
        user_nonce=user_nonce,
    )


def submit_and_sign_dex_swap_order(
    *,
    w3: Web3,
    orderbook: Any,
    app_id: str,
    app_address: str,
    app_abi: list[dict[str, Any]],
    user_key: str,
    submitted_by: str,
    domain_separator: bytes,
    chain_id: int,
    user_nonce: int,
    input_token: str,
    output_token: str,
    input_amount: int,
    min_output_amount: int,
    perpetual: bool = False,
    max_executions: int = 1,
    cooldown: int = 0,
    intent_function: str = "execute",
) -> tuple[Any, SignedDexSwapOrder]:
    """Submit an orderbook swap, then sign with its real keccak(order_id)."""
    placeholder_deadline = w3.eth.get_block("latest")["timestamp"] + 3600
    order = orderbook.submit(
        app_id=app_id,
        intent_function=intent_function,
        params=build_orderbook_dex_swap_params(
            input_token=input_token,
            output_token=output_token,
            input_amount=input_amount,
            min_output_amount=min_output_amount,
            app_address=app_address,
        ),
        submitted_by=submitted_by,
        chain_id=chain_id,
        deadline=float(placeholder_deadline),
        perpetual=perpetual,
        max_executions=max_executions,
        cooldown=cooldown,
    )

    signed = sign_dex_swap_order(
        w3=w3,
        app_address=app_address,
        app_abi=app_abi,
        user_key=user_key,
        submitted_by=submitted_by,
        domain_separator=domain_separator,
        chain_id=chain_id,
        order_id_bytes=keccak(order.order_id.encode()),
        user_nonce=user_nonce,
        input_token=input_token,
        output_token=output_token,
        input_amount=input_amount,
        min_output_amount=min_output_amount,
        receiver=submitted_by,
        perpetual=perpetual,
        max_executions=max_executions,
        cooldown=cooldown,
    )

    orderbook.update_order(order.order_id, user_signature=signed.user_signature.hex())
    order.params["intent_selector"] = signed.swap_selector.hex()
    order.params["intent_params_hex"] = signed.intent_params.hex()
    order.params["user_nonce"] = signed.user_nonce
    order.deadline = float(signed.deadline)
    return order, signed


class StaticMockSolver:
    """Minimal solver for mock-relayer lifecycle tests."""

    def generate_plan(self, app: Any, state: Any, snapshot: Any) -> ExecutionPlan:
        deadline = int(getattr(snapshot, "timestamp", 0) or 0) + 300
        return ExecutionPlan(
            intent_id=app.app_id,
            interactions=[],
            deadline=deadline,
            nonce=0,
            metadata={},
        )
