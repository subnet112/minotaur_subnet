"""On-chain order execution module for OIF solvers.

This module handles:
1. EIP-712 signature verification
2. Settlement contract interaction
3. Order execution and status tracking

The Settlement contract interface is based on the OIF Settlement standard.
"""

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from eth_abi import encode as abi_encode
from eth_abi.packed import encode_packed
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_bytes, to_checksum_address

try:
    from web3 import Web3
    from web3.exceptions import ContractLogicError, TransactionNotFound
    WEB3_AVAILABLE = True
except ImportError:
    Web3 = None
    ContractLogicError = Exception
    TransactionNotFound = Exception
    WEB3_AVAILABLE = False


class OrderStatus(Enum):
    """Order execution status"""
    RECEIVED = "received"
    PENDING = "pending"
    EXECUTING = "executing"
    EXECUTED = "executed"
    SETTLED = "settled"
    FINALIZED = "finalized"
    FAILED = "failed"


class PermitType(Enum):
    """Permit types for token approval"""
    NONE = 0
    EIP2612 = 1
    EIP3009 = 2
    STANDARD_APPROVAL = 3
    CUSTOM = 4


@dataclass
class ExecutedOrder:
    """Represents an executed order with transaction details"""
    order_id: str
    quote_id: str
    status: OrderStatus
    created_at: int
    updated_at: int
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    gas_used: Optional[int] = None
    error_message: Optional[str] = None
    execution_plan: Optional[Dict] = None
    intent_data: Optional[Dict] = None


# Settlement contract ABI - the entry point for order execution
SETTLEMENT_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "quoteId", "type": "bytes32"},
                    {"name": "user", "type": "address"},
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "minAmountOut", "type": "uint256"},
                    {"name": "receiver", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {
                        "components": [
                            {"name": "permitType", "type": "uint8"},
                            {"name": "permitCall", "type": "bytes"},
                            {"name": "amount", "type": "uint256"},
                            {"name": "deadline", "type": "uint256"},
                        ],
                        "name": "permit",
                        "type": "tuple"
                    },
                    {"name": "interactionsHash", "type": "bytes32"},
                    {"name": "callValue", "type": "uint256"},
                    {"name": "gasEstimate", "type": "uint256"},
                    {"name": "userSignature", "type": "bytes"},
                ],
                "name": "intent",
                "type": "tuple"
            },
            {
                "components": [
                    {"name": "blockNumber", "type": "uint256"},
                    {
                        "components": [
                            {"name": "target", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "callData", "type": "bytes"},
                        ],
                        "name": "preInteractions",
                        "type": "tuple[]"
                    },
                    {
                        "components": [
                            {"name": "target", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "callData", "type": "bytes"},
                        ],
                        "name": "interactions",
                        "type": "tuple[]"
                    },
                    {
                        "components": [
                            {"name": "target", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "callData", "type": "bytes"},
                        ],
                        "name": "postInteractions",
                        "type": "tuple[]"
                    },
                ],
                "name": "plan",
                "type": "tuple"
            }
        ],
        "name": "executeOrder",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "nonce", "type": "uint256"}
        ],
        "name": "isNonceUsed",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "quoteId", "type": "bytes32"},
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "tokenIn", "type": "address"},
            {"indexed": False, "name": "amountIn", "type": "uint256"},
            {"indexed": False, "name": "tokenOut", "type": "address"},
            {"indexed": False, "name": "amountOut", "type": "uint256"},
            {"indexed": False, "name": "feeAmount", "type": "uint256"},
            {"indexed": False, "name": "gasEstimate", "type": "uint256"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
        ],
        "name": "SwapSettled",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "quoteId", "type": "bytes32"},
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "reason", "type": "string"},
        ],
        "name": "SwapFailed",
        "type": "event"
    }
]

# ERC20 ABI for token approvals
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class EIP712Verifier:
    """Verifies EIP-712 typed data signatures for order intents."""
    
    # EIP-712 type hashes (must match the Settlement contract)
    PERMIT_DATA_TYPEHASH = keccak(
        b"PermitData(uint8 permitType,bytes permitCall,uint256 amount,uint256 deadline)"
    )
    
    ORDER_INTENT_TYPEHASH = keccak(
        b"OrderIntent(bytes32 quoteId,address user,address tokenIn,address tokenOut,"
        b"uint256 amountIn,uint256 minAmountOut,address receiver,uint256 deadline,"
        b"uint256 nonce,bytes32 interactionsHash,uint256 callValue,uint256 gasEstimate,"
        b"PermitData permit)PermitData(uint8 permitType,bytes permitCall,uint256 amount,uint256 deadline)"
    )
    
    def __init__(
        self,
        settlement_address: str,
        chain_id: int,
        name: str = "OIF Settlement",
        version: str = "1"
    ):
        self.settlement_address = to_checksum_address(settlement_address)
        self.chain_id = chain_id
        self.name = name
        self.version = version
        
        # Compute domain separator
        self.domain_separator = self._compute_domain_separator()
    
    def _compute_domain_separator(self) -> bytes:
        """Compute the EIP-712 domain separator"""
        domain_typehash = keccak(
            b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    domain_typehash,
                    keccak(self.name.encode()),
                    keccak(self.version.encode()),
                    self.chain_id,
                    to_bytes(hexstr=self.settlement_address)
                ]
            )
        )
    
    def _hash_permit_data(self, permit: Dict) -> bytes:
        """Hash the PermitData struct"""
        permit_type = int(permit.get("permitType", 3))  # Default to STANDARD_APPROVAL
        permit_call = to_bytes(hexstr=permit.get("permitCall", "0x"))
        amount = int(permit.get("amount", 0))
        deadline = int(permit.get("deadline", 0))
        
        return keccak(
            abi_encode(
                ["bytes32", "uint8", "bytes32", "uint256", "uint256"],
                [
                    self.PERMIT_DATA_TYPEHASH,
                    permit_type,
                    keccak(permit_call),
                    amount,
                    deadline
                ]
            )
        )
    
    def _hash_order_intent(self, intent: Dict) -> bytes:
        """Hash the OrderIntent struct for EIP-712 signing"""
        permit = intent.get("permit", {})
        permit_hash = self._hash_permit_data(permit)
        
        # Convert quoteId to bytes32
        quote_id = intent.get("quoteId", "")
        if isinstance(quote_id, str):
            if quote_id.startswith("0x"):
                quote_id_bytes = to_bytes(hexstr=quote_id.ljust(66, '0')[:66])
            else:
                quote_id_bytes = keccak(quote_id.encode())
        else:
            quote_id_bytes = quote_id
        
        # Convert interactionsHash to bytes32
        interactions_hash = intent.get("interactionsHash", "0x" + "0" * 64)
        interactions_hash_bytes = to_bytes(hexstr=interactions_hash)
        
        return keccak(
            abi_encode(
                [
                    "bytes32", "bytes32", "address", "address", "address",
                    "uint256", "uint256", "address", "uint256", "uint256",
                    "bytes32", "uint256", "uint256", "bytes32"
                ],
                [
                    self.ORDER_INTENT_TYPEHASH,
                    quote_id_bytes,
                    to_checksum_address(intent["user"]),
                    to_checksum_address(intent["tokenIn"]),
                    to_checksum_address(intent["tokenOut"]),
                    int(intent["amountIn"]),
                    int(intent["minAmountOut"]),
                    to_checksum_address(intent["receiver"]),
                    int(intent["deadline"]),
                    int(intent["nonce"]),
                    interactions_hash_bytes,
                    int(intent.get("callValue", 0)),
                    int(intent.get("gasEstimate", 0)),
                    permit_hash
                ]
            )
        )
    
    def get_typed_data_hash(self, intent: Dict) -> bytes:
        """Get the full EIP-712 hash for signing"""
        struct_hash = self._hash_order_intent(intent)
        return keccak(b"\x19\x01" + self.domain_separator + struct_hash)
    
    def verify_signature(self, intent: Dict, signature: str) -> Tuple[bool, str]:
        """
        Verify an EIP-712 signature for an order intent.
        
        Returns:
            (is_valid, recovered_address or error_message)
        """
        try:
            # Get the hash to sign
            message_hash = self.get_typed_data_hash(intent)
            
            # Parse signature
            sig_bytes = to_bytes(hexstr=signature)
            if len(sig_bytes) != 65:
                return False, f"Invalid signature length: {len(sig_bytes)} (expected 65)"
            
            # Extract r, s, v
            r = int.from_bytes(sig_bytes[:32], 'big')
            s = int.from_bytes(sig_bytes[32:64], 'big')
            v = sig_bytes[64]
            
            # Normalize v value (some wallets use 0/1, some use 27/28)
            if v < 27:
                v += 27
            
            # Recover the signer address
            from eth_account._utils.signing import signature_from_rsv
            from eth_keys import KeyAPI
            
            signature_obj = KeyAPI().Signature(vrs=(v, r, s))
            public_key = signature_obj.recover_public_key_from_msg_hash(message_hash)
            recovered_address = public_key.to_checksum_address()
            
            # Check if recovered address matches the intent user
            expected_user = to_checksum_address(intent["user"])
            if recovered_address.lower() == expected_user.lower():
                return True, recovered_address
            else:
                return False, f"Signature mismatch: expected {expected_user}, got {recovered_address}"
                
        except Exception as e:
            return False, f"Signature verification failed: {str(e)}"


class OnchainExecutor:
    """
    Executes orders on-chain via the Settlement contract.
    
    This executor handles the full lifecycle:
    1. Signature verification
    2. Pre-flight checks (nonce, allowance)
    3. Transaction submission
    4. Status tracking and receipt monitoring
    """
    
    def __init__(
        self,
        web3: "Web3",
        settlement_address: str,
        chain_id: int,
        private_key: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        dry_run: bool = False
    ):
        """
        Initialize the on-chain executor.
        
        Args:
            web3: Web3 instance connected to the target chain
            settlement_address: Address of the Settlement contract
            chain_id: Chain ID (e.g., 1 for mainnet, 8453 for Base)
            private_key: Private key for signing transactions (optional for dry-run)
            logger: Logger instance
            dry_run: If True, simulate but don't actually submit transactions
        """
        self.web3 = web3
        self.settlement_address = to_checksum_address(settlement_address)
        self.chain_id = chain_id
        self.private_key = private_key
        self.logger = logger or logging.getLogger(__name__)
        self.dry_run = dry_run
        
        # Initialize contracts
        self.settlement_contract = web3.eth.contract(
            address=self.settlement_address,
            abi=SETTLEMENT_ABI
        )
        
        # Initialize signature verifier
        self.verifier = EIP712Verifier(
            settlement_address=settlement_address,
            chain_id=chain_id
        )
        
        # Get executor account if private key provided
        self.executor_account = None
        if private_key:
            self.executor_account = Account.from_key(private_key)
            self.logger.info(f"Executor account: {self.executor_account.address}")
        
        # Order tracking
        self.orders: Dict[str, ExecutedOrder] = {}
        self._order_lock = threading.Lock()
    
    def _extract_eth_address(self, interop_address: str) -> str:
        """Extract raw Ethereum address from ERC-7930 interop address format"""
        if not interop_address:
            return "0x" + "0" * 40
        
        if len(interop_address) == 42 and interop_address.startswith("0x"):
            # Already a plain Ethereum address
            return to_checksum_address(interop_address)
        
        # Parse ERC-7930 interop format: 0x[version][chainType][chainRefLen][addrLen][chainRef][address]
        try:
            data = bytes.fromhex(interop_address[2:] if interop_address.startswith("0x") else interop_address)
            
            if len(data) < 5:
                return "0x" + "0" * 40
            
            # Parse header
            chain_ref_len = data[3]
            addr_len = data[4]
            
            # Extract address (last addr_len bytes)
            if len(data) < 5 + chain_ref_len + addr_len:
                return "0x" + "0" * 40
            
            addr_bytes = data[5 + chain_ref_len:5 + chain_ref_len + addr_len]
            return to_checksum_address("0x" + addr_bytes.hex())
            
        except Exception as e:
            self.logger.warning(f"Failed to parse interop address {interop_address}: {e}")
            return "0x" + "0" * 40
    
    def _prepare_order_intent(self, quote_data: Dict, signature: str) -> Dict:
        """Prepare the OrderIntent struct for the settlement contract"""
        details = quote_data.get("details", {})
        settlement = quote_data.get("settlement", {})
        
        available_inputs = details.get("availableInputs", [{}])
        requested_outputs = details.get("requestedOutputs", [{}])
        
        input_data = available_inputs[0] if available_inputs else {}
        output_data = requested_outputs[0] if requested_outputs else {}
        
        # Extract addresses from interop format
        user_address = self._extract_eth_address(input_data.get("user", ""))
        token_in = self._extract_eth_address(input_data.get("asset", ""))
        token_out = self._extract_eth_address(output_data.get("asset", ""))
        receiver = self._extract_eth_address(output_data.get("receiver", user_address))
        
        # Parse amounts
        amount_in = int(input_data.get("amount", 0))
        min_amount_out = int(output_data.get("amount", 0))
        
        # Get settlement parameters
        deadline = int(settlement.get("deadline", int(time.time()) + 3600))
        nonce = settlement.get("nonce", "0x" + secrets.token_hex(16))
        if isinstance(nonce, str) and nonce.startswith("0x"):
            nonce_int = int(nonce, 16)
        else:
            nonce_int = int(nonce)
        
        call_value = int(settlement.get("callValue", 0))
        gas_estimate = int(settlement.get("gasEstimate", 200000))
        interactions_hash = settlement.get("interactionsHash", "0x" + "0" * 64)
        
        # Build permit data
        permit_data = settlement.get("permit", {})
        permit_type_str = permit_data.get("permitType", "standard_approval").lower()
        permit_type_map = {
            "none": 0,
            "eip2612": 1,
            "eip3009": 2,
            "standard_approval": 3,
            "custom": 4
        }
        permit_type = permit_type_map.get(permit_type_str, 3)
        
        # Get quoteId as bytes32
        quote_id = quote_data.get("quoteId", "")
        if isinstance(quote_id, str):
            if len(quote_id) <= 66 and quote_id.startswith("0x"):
                quote_id_bytes32 = quote_id.ljust(66, '0')[:66]
            else:
                # Hash the quoteId string to get bytes32
                quote_id_bytes32 = "0x" + keccak(quote_id.encode()).hex()
        else:
            quote_id_bytes32 = "0x" + keccak(str(quote_id).encode()).hex()
        
        return {
            "quoteId": quote_id_bytes32,
            "user": user_address,
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amountIn": amount_in,
            "minAmountOut": min_amount_out,
            "receiver": receiver,
            "deadline": deadline,
            "nonce": nonce_int,
            "permit": {
                "permitType": permit_type,
                "permitCall": to_bytes(hexstr=permit_data.get("permitCall", "0x")),
                "amount": int(permit_data.get("amount", amount_in)),
                "deadline": int(permit_data.get("deadline", deadline))
            },
            "interactionsHash": to_bytes(hexstr=interactions_hash),
            "callValue": call_value,
            "gasEstimate": gas_estimate,
            "userSignature": to_bytes(hexstr=signature)
        }
    
    def _prepare_execution_plan(self, settlement: Dict) -> Dict:
        """Prepare the ExecutionPlan struct for the settlement contract"""
        exec_plan = settlement.get("executionPlan", {})
        
        def parse_interactions(interactions: List[Dict]) -> List[tuple]:
            result = []
            for inter in interactions:
                target = self._extract_eth_address(inter.get("target", ""))
                value = int(inter.get("value", 0))
                call_data = to_bytes(hexstr=inter.get("callData", "0x"))
                result.append((target, value, call_data))
            return result
        
        block_number = exec_plan.get("blockNumber")
        if block_number:
            block_number = int(block_number)
        else:
            block_number = self.web3.eth.block_number
        
        return {
            "blockNumber": block_number,
            "preInteractions": parse_interactions(exec_plan.get("preInteractions", [])),
            "interactions": parse_interactions(exec_plan.get("interactions", [])),
            "postInteractions": parse_interactions(exec_plan.get("postInteractions", []))
        }
    
    def verify_order_signature(
        self,
        quote_data: Dict,
        signature: str
    ) -> Tuple[bool, str]:
        """
        Verify that the signature matches the order intent.
        
        Args:
            quote_data: The quote details from the cache
            signature: The user's EIP-712 signature
            
        Returns:
            (is_valid, message)
        """
        try:
            intent = self._prepare_order_intent(quote_data, signature)
            
            # Build the intent dict for verification (without signature)
            intent_for_verification = {
                "quoteId": intent["quoteId"],
                "user": intent["user"],
                "tokenIn": intent["tokenIn"],
                "tokenOut": intent["tokenOut"],
                "amountIn": intent["amountIn"],
                "minAmountOut": intent["minAmountOut"],
                "receiver": intent["receiver"],
                "deadline": intent["deadline"],
                "nonce": intent["nonce"],
                "permit": {
                    "permitType": intent["permit"]["permitType"],
                    "permitCall": "0x" + intent["permit"]["permitCall"].hex(),
                    "amount": intent["permit"]["amount"],
                    "deadline": intent["permit"]["deadline"]
                },
                "interactionsHash": "0x" + intent["interactionsHash"].hex(),
                "callValue": intent["callValue"],
                "gasEstimate": intent["gasEstimate"]
            }
            
            return self.verifier.verify_signature(intent_for_verification, signature)
            
        except Exception as e:
            return False, f"Verification error: {str(e)}"
    
    async def check_nonce_used(self, user: str, nonce: int) -> bool:
        """Check if a nonce has been used"""
        try:
            return self.settlement_contract.functions.isNonceUsed(
                to_checksum_address(user),
                nonce
            ).call()
        except Exception as e:
            self.logger.warning(f"Failed to check nonce: {e}")
            return False
    
    def execute_order(
        self,
        order_id: str,
        quote_data: Dict,
        signature: str,
        skip_verification: bool = False
    ) -> ExecutedOrder:
        """
        Execute an order on-chain.
        
        Args:
            order_id: Unique order identifier
            quote_data: The cached quote data
            signature: The user's EIP-712 signature
            skip_verification: Skip signature verification (use with caution)
            
        Returns:
            ExecutedOrder with execution status
        """
        now = int(time.time())
        
        # Create initial order record
        order = ExecutedOrder(
            order_id=order_id,
            quote_id=quote_data.get("quoteId", ""),
            status=OrderStatus.RECEIVED,
            created_at=now,
            updated_at=now,
            execution_plan=quote_data.get("settlement", {}).get("executionPlan")
        )
        
        with self._order_lock:
            self.orders[order_id] = order
        
        try:
            # Step 1: Verify signature (unless skipped)
            if not skip_verification:
                is_valid, msg = self.verify_order_signature(quote_data, signature)
                if not is_valid:
                    order.status = OrderStatus.FAILED
                    order.error_message = f"Signature verification failed: {msg}"
                    order.updated_at = int(time.time())
                    self.logger.error(f"Order {order_id}: {order.error_message}")
                    return order
                self.logger.info(f"Order {order_id}: Signature verified for user {msg}")
            
            order.status = OrderStatus.PENDING
            order.updated_at = int(time.time())
            
            # Step 2: Prepare contract call data
            intent = self._prepare_order_intent(quote_data, signature)
            plan = self._prepare_execution_plan(quote_data.get("settlement", {}))
            
            order.intent_data = {
                "user": intent["user"],
                "tokenIn": intent["tokenIn"],
                "tokenOut": intent["tokenOut"],
                "amountIn": intent["amountIn"],
                "minAmountOut": intent["minAmountOut"]
            }
            
            # Step 3: Execute on-chain (or dry run)
            if self.dry_run:
                self.logger.info(f"Order {order_id}: DRY RUN - would execute on-chain")
                order.status = OrderStatus.FINALIZED
                order.tx_hash = "0x" + secrets.token_hex(32)  # Fake hash
                order.block_number = self.web3.eth.block_number
                order.gas_used = intent["gasEstimate"]
                order.updated_at = int(time.time())
                return order
            
            if not self.executor_account:
                order.status = OrderStatus.FAILED
                order.error_message = "No executor account configured"
                order.updated_at = int(time.time())
                return order
            
            order.status = OrderStatus.EXECUTING
            order.updated_at = int(time.time())
            
            # Build contract transaction
            call_value = intent["callValue"]
            
            # Convert intent to contract format
            intent_tuple = (
                to_bytes(hexstr=intent["quoteId"]),
                to_checksum_address(intent["user"]),
                to_checksum_address(intent["tokenIn"]),
                to_checksum_address(intent["tokenOut"]),
                intent["amountIn"],
                intent["minAmountOut"],
                to_checksum_address(intent["receiver"]),
                intent["deadline"],
                intent["nonce"],
                (
                    intent["permit"]["permitType"],
                    intent["permit"]["permitCall"],
                    intent["permit"]["amount"],
                    intent["permit"]["deadline"]
                ),
                intent["interactionsHash"],
                intent["callValue"],
                intent["gasEstimate"],
                intent["userSignature"]
            )
            
            plan_tuple = (
                plan["blockNumber"],
                plan["preInteractions"],
                plan["interactions"],
                plan["postInteractions"]
            )
            
            # Estimate gas
            try:
                estimated_gas = self.settlement_contract.functions.executeOrder(
                    intent_tuple,
                    plan_tuple
                ).estimate_gas({
                    "from": self.executor_account.address,
                    "value": call_value
                })
                # Add 20% buffer
                gas_limit = int(estimated_gas * 1.2)
            except Exception as e:
                self.logger.warning(f"Gas estimation failed: {e}, using fallback")
                gas_limit = intent["gasEstimate"] * 2
            
            # Build transaction
            tx = self.settlement_contract.functions.executeOrder(
                intent_tuple,
                plan_tuple
            ).build_transaction({
                "from": self.executor_account.address,
                "value": call_value,
                "gas": gas_limit,
                "nonce": self.web3.eth.get_transaction_count(self.executor_account.address),
                "maxFeePerGas": self.web3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.web3.to_wei(1, "gwei")
            })
            
            # Sign and send transaction
            signed_tx = self.web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            order.tx_hash = tx_hash.hex()
            order.updated_at = int(time.time())
            
            self.logger.info(f"Order {order_id}: Transaction submitted: {order.tx_hash}")
            
            # Wait for receipt
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            order.block_number = receipt["blockNumber"]
            order.gas_used = receipt["gasUsed"]
            order.updated_at = int(time.time())
            
            if receipt["status"] == 1:
                order.status = OrderStatus.FINALIZED
                self.logger.info(
                    f"Order {order_id}: Execution successful! "
                    f"Block: {order.block_number}, Gas: {order.gas_used}"
                )
            else:
                order.status = OrderStatus.FAILED
                order.error_message = "Transaction reverted"
                self.logger.error(f"Order {order_id}: Transaction reverted")
            
        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error_message = str(e)
            order.updated_at = int(time.time())
            self.logger.error(f"Order {order_id}: Execution failed: {e}")
        
        return order
    
    def execute_order_async(
        self,
        order_id: str,
        quote_data: Dict,
        signature: str,
        skip_verification: bool = False
    ) -> ExecutedOrder:
        """
        Execute an order asynchronously in a background thread.
        
        Returns immediately with RECEIVED status. Use get_order() to poll for updates.
        """
        now = int(time.time())
        
        # Create initial order record
        order = ExecutedOrder(
            order_id=order_id,
            quote_id=quote_data.get("quoteId", ""),
            status=OrderStatus.RECEIVED,
            created_at=now,
            updated_at=now
        )
        
        with self._order_lock:
            self.orders[order_id] = order
        
        # Execute in background thread
        def run_execution():
            self.execute_order(order_id, quote_data, signature, skip_verification)
        
        thread = threading.Thread(target=run_execution, daemon=True)
        thread.start()
        
        return order
    
    def get_order(self, order_id: str) -> Optional[ExecutedOrder]:
        """Get order status by ID"""
        with self._order_lock:
            return self.orders.get(order_id)
    
    def to_order_response(self, order: ExecutedOrder) -> Dict:
        """Convert ExecutedOrder to API response format"""
        response = {
            "orderId": order.order_id,
            "status": order.status.value,
            "order": {
                "createdAt": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(order.created_at)
                ),
                "updatedAt": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(order.updated_at)
                ),
            }
        }
        
        if order.tx_hash:
            response["order"]["fillTransaction"] = {
                "txHash": order.tx_hash,
                "blockNumber": order.block_number,
                "gasUsed": order.gas_used
            }
        
        if order.error_message:
            response["error"] = order.error_message
        
        return response

