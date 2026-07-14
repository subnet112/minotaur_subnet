"""
Smart contract deployment and interaction for the App Intents system.

``ContractManager`` provides methods to:

1. Deploy a pre-compiled contract to a supported chain.
2. Read state from deployed contracts (``call_contract``).
3. Execute an ``ExecutionPlan`` against a deployed AppIntent contract.

For the MVP, Solidity source is *not* compiled on the fly.  Instead,
callers supply pre-compiled bytecode and ABI.  The ``APP_INTENT_BASE_ABI``
constant captures the on-chain interface defined in ``AppIntentBase.sol``
so that ``execute_plan`` can encode calls without requiring the caller to
pass an ABI every time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from web3 import Web3
from web3.contract import Contract
from web3.types import TxParams, TxReceipt

from minotaur_subnet.blockchain.chains import get_web3, get_tx_url
from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
    ExecutionPlan,
    Interaction,
    IntentState,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Pre-compiled ABI for the IAppIntent / AppIntentBase interface
# ═══════════════════════════════════════════════════════════════════════════════
#
# This mirrors the Solidity structs and functions from
# subnet112/minotaur_contracts (src/AppIntentBase.sol +
# src/interfaces/IAppIntentBase.sol). Only functions that
# ``ContractManager`` needs to call are included.

# Solidity struct types used in the ABI (for reference in tuple encoding)
_INTERACTION_TUPLE = {
    "components": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "callData", "type": "bytes"},
    ],
    "name": "interactions",
    "type": "tuple[]",
}

_EXECUTION_PLAN_TUPLE = {
    "components": [
        {"name": "intentId", "type": "bytes32"},
        _INTERACTION_TUPLE,
        {"name": "deadline", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "metadata", "type": "bytes"},
    ],
    "type": "tuple",
}

_SCORE_RESULT_TUPLE = {
    "components": [
        {"name": "score", "type": "uint256"},
        {"name": "valid", "type": "bool"},
        {"name": "reason", "type": "string"},
    ],
    "type": "tuple",
}


APP_INTENT_BASE_ABI: list[dict[str, Any]] = [
    # --- execute(ExecutionPlan) ---
    {
        "inputs": [
            {
                "name": "plan",
                **_EXECUTION_PLAN_TUPLE,
            }
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    # --- canExecute(ExecutionPlan) -> (bool, string) ---
    {
        "inputs": [
            {
                "name": "plan",
                **_EXECUTION_PLAN_TUPLE,
            }
        ],
        "name": "canExecute",
        "outputs": [
            {"name": "canExecute", "type": "bool"},
            {"name": "reason", "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # --- score(ExecutionPlan) -> ScoreResult ---
    {
        "inputs": [
            {
                "name": "plan",
                **_EXECUTION_PLAN_TUPLE,
            }
        ],
        "name": "score",
        "outputs": [
            {
                "name": "result",
                **_SCORE_RESULT_TUPLE,
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # --- nonce() -> uint256 ---
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- scoreThreshold() -> uint256 ---
    {
        "inputs": [],
        "name": "scoreThreshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- owner() -> address ---
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- intentType() -> string ---
    {
        "inputs": [],
        "name": "intentType",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- version() -> string ---
    {
        "inputs": [],
        "name": "version",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- hashPlan(ExecutionPlan) -> bytes32 ---
    {
        "inputs": [
            {
                "name": "plan",
                **_EXECUTION_PLAN_TUPLE,
            }
        ],
        "name": "hashPlan",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "pure",
        "type": "function",
    },
    # --- isExecuted(bytes32) -> bool ---
    {
        "inputs": [{"name": "planHash", "type": "bytes32"}],
        "name": "isExecuted",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    # --- Events ---
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "intentId", "type": "bytes32"},
            {"indexed": True, "name": "solver", "type": "address"},
            {"indexed": False, "name": "score", "type": "uint256"},
            {"indexed": False, "name": "planHash", "type": "bytes32"},
        ],
        "name": "Executed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "intentId", "type": "bytes32"},
            {"indexed": True, "name": "solver", "type": "address"},
            {"indexed": False, "name": "score", "type": "uint256"},
            {"indexed": False, "name": "reason", "type": "string"},
        ],
        "name": "Rejected",
        "type": "event",
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Retry configuration
# ═══════════════════════════════════════════════════════════════════════════════

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0


async def _retry_rpc(fn, description: str = "RPC call"):
    """
    Run a synchronous Web3 call in an executor with retries.

    Up to ``MAX_RETRIES`` attempts are made with exponential backoff.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                    description,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description,
                    MAX_RETRIES,
                    exc,
                )

    raise last_exc  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
#  Helper: convert shared-types plan to Solidity-compatible tuple
# ═══════════════════════════════════════════════════════════════════════════════


def _plan_to_solidity(plan: ExecutionPlan) -> tuple:
    """
    Convert an ``ExecutionPlan`` dataclass to a tuple matching the
    Solidity ``ExecutionPlan`` struct layout expected by ``web3.py``
    contract calls.

    Returns a tuple of::

        (intentId_bytes32, interactions_list, deadline, nonce, metadata_bytes)
    """
    # intentId -> bytes32 (pad or hash if needed)
    intent_id_bytes = _to_bytes32(plan.intent_id)

    # Interactions -> list of (target, value, callData)
    interactions = []
    for ix in plan.interactions:
        interactions.append((
            Web3.to_checksum_address(ix.target),
            int(ix.value),
            bytes.fromhex(ix.call_data[2:]) if ix.call_data.startswith("0x") else bytes.fromhex(ix.call_data),
        ))

    # metadata -> bytes (encode as JSON bytes for simplicity)
    import json as _json
    metadata_bytes = _json.dumps(plan.metadata).encode() if plan.metadata else b""

    return (intent_id_bytes, interactions, plan.deadline, plan.nonce, metadata_bytes)


def _to_bytes32(value: str) -> bytes:
    """
    Convert a string to ``bytes32``.

    - If *value* looks like a ``0x``-prefixed hex string of length 66, decode it.
    - Otherwise, UTF-8 encode and right-pad / keccak-hash.
    """
    if value.startswith("0x") and len(value) == 66:
        return bytes.fromhex(value[2:])

    raw = value.encode("utf-8")
    if len(raw) <= 32:
        return raw.ljust(32, b"\x00")

    # Longer strings: use keccak256 to fit in bytes32.
    return Web3.keccak(raw)


# ═══════════════════════════════════════════════════════════════════════════════
#  ContractManager
# ═══════════════════════════════════════════════════════════════════════════════


class ContractManager:
    """
    Manage smart contract deployment and interaction for App Intents.

    All write operations require a ``WalletManager`` to sign transactions.
    Read operations only need RPC access (no wallet).
    """

    def __init__(self, wallet_manager=None):
        """
        Parameters
        ----------
        wallet_manager:
            A ``WalletManager`` instance used for signing deployment and
            execution transactions.  Required for write operations
            (``deploy_contract``, ``execute_plan``).  Read-only calls
            (``call_contract``, ``get_intent_state``) do not need it.
        """
        self._wallet_manager = wallet_manager

    # -------------------------------------------------------------------
    # Deploy
    # -------------------------------------------------------------------

    async def deploy_contract(
        self,
        wallet_address: str,
        bytecode: str,
        abi: list[dict[str, Any]],
        constructor_args: list[Any],
        chain_id: int,
    ) -> DeploymentResult:
        """
        Deploy a contract using *wallet_address* as the sender.

        Parameters
        ----------
        wallet_address:
            Address of the deploying wallet (must exist in the wallet manager).
        bytecode:
            Hex-encoded compiled bytecode (with or without ``0x`` prefix).
        abi:
            Contract ABI (JSON-decoded).
        constructor_args:
            Positional constructor arguments.
        chain_id:
            Target chain.

        Returns
        -------
        DeploymentResult
            Contains the deployed contract address and status.
        """
        if self._wallet_manager is None:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error="No wallet manager configured -- cannot sign deployment tx.",
            )

        try:
            w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        except ValueError as exc:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error=str(exc),
            )

        sender = Web3.to_checksum_address(wallet_address)

        # Build the deployment transaction
        contract = w3.eth.contract(abi=abi, bytecode=bytecode)
        try:
            construct_txn = contract.constructor(*constructor_args)
        except Exception as exc:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error=f"Failed to encode constructor: {exc}",
            )

        try:
            # Estimate gas
            nonce = await _retry_rpc(
                lambda: w3.eth.get_transaction_count(sender),
                "get_transaction_count",
            )

            gas_price = await _retry_rpc(
                lambda: w3.eth.gas_price,
                "gas_price",
            )

            tx: TxParams = construct_txn.build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
                "gasPrice": gas_price,
            })

            # Estimate gas with a safety margin
            gas_estimate = await _retry_rpc(
                lambda: w3.eth.estimate_gas(tx),
                "estimate_gas",
            )
            tx["gas"] = int(gas_estimate * 1.2)  # 20% buffer

        except Exception as exc:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error=f"Failed to build deployment tx: {exc}",
            )

        # Sign and send
        try:
            signed_hex = await self._wallet_manager.sign_transaction(wallet_address, dict(tx))
            tx_hash = await _retry_rpc(
                lambda: w3.eth.send_raw_transaction(bytes.fromhex(
                    signed_hex[2:] if signed_hex.startswith("0x") else signed_hex
                )),
                "send_raw_transaction",
            )

            logger.info(
                "Deployment tx sent: %s (%s)",
                tx_hash.hex(),
                get_tx_url(chain_id, tx_hash.hex()),
            )

            # Wait for receipt
            receipt: TxReceipt = await _retry_rpc(
                lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120),
                "wait_for_transaction_receipt",
            )

        except Exception as exc:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error=f"Deployment transaction failed: {exc}",
            )

        if receipt["status"] != 1:
            return DeploymentResult(
                app_id="",
                status=AppStatus.DRAFT,
                error=f"Deployment reverted. TX: {tx_hash.hex()}",
            )

        contract_address = receipt["contractAddress"]
        logger.info(
            "Contract deployed at %s on chain %d (%s)",
            contract_address,
            chain_id,
            get_tx_url(chain_id, tx_hash.hex()),
        )

        return DeploymentResult(
            app_id="",
            status=AppStatus.SOLVING,
            contract_address=contract_address,
            chain_id=chain_id,
        )

    # -------------------------------------------------------------------
    # Read (call)
    # -------------------------------------------------------------------

    async def call_contract(
        self,
        contract_address: str,
        abi: list[dict[str, Any]],
        function_name: str,
        args: list[Any] | None = None,
        chain_id: int = 1,
    ) -> Any:
        """
        Call a read-only (``view`` / ``pure``) function on a contract.

        Parameters
        ----------
        contract_address:
            Deployed contract address.
        abi:
            Contract ABI containing the function.
        function_name:
            Name of the function to call.
        args:
            Positional arguments for the function call.
        chain_id:
            Chain where the contract lives.

        Returns
        -------
        Any
            The return value decoded according to the ABI.
        """
        w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        checksum = Web3.to_checksum_address(contract_address)
        contract = w3.eth.contract(address=checksum, abi=abi)

        fn = contract.functions[function_name](*(args or []))
        result = await _retry_rpc(fn.call, f"call {function_name}")
        return result

    # -------------------------------------------------------------------
    # Execute plan
    # -------------------------------------------------------------------

    async def execute_plan(
        self,
        contract_address: str,
        plan: ExecutionPlan,
        wallet_address: str,
        chain_id: int,
        value_wei: int = 0,
    ) -> str:
        """
        Execute an ``ExecutionPlan`` on an AppIntent contract.

        Encodes the plan into the ``execute(ExecutionPlan)`` function
        defined in ``AppIntentBase`` and sends a signed transaction.

        Parameters
        ----------
        contract_address:
            Address of the deployed AppIntent contract.
        plan:
            The execution plan to submit.
        wallet_address:
            Address of the wallet that will sign the transaction.  Must be
            the contract's ``owner`` (multisig).
        chain_id:
            Target chain.
        value_wei:
            Amount of ETH (in wei) to send with the transaction.  Required
            when the plan's interactions include ETH transfers.

        Returns
        -------
        str
            The transaction hash as a ``0x``-prefixed hex string.
        """
        if self._wallet_manager is None:
            raise RuntimeError("No wallet manager configured -- cannot execute plan.")

        w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        sender = Web3.to_checksum_address(wallet_address)
        contract_addr = Web3.to_checksum_address(contract_address)

        contract = w3.eth.contract(address=contract_addr, abi=APP_INTENT_BASE_ABI)

        # Convert the Python ExecutionPlan to Solidity-compatible tuple
        plan_tuple = _plan_to_solidity(plan)

        # Build the transaction
        nonce = await _retry_rpc(
            lambda: w3.eth.get_transaction_count(sender),
            "get_transaction_count",
        )

        gas_price = await _retry_rpc(
            lambda: w3.eth.gas_price,
            "gas_price",
        )

        # If no explicit value, sum the values from the plan's interactions
        if value_wei == 0:
            value_wei = sum(int(ix.value) for ix in plan.interactions)

        tx = contract.functions.execute(plan_tuple).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
            "gasPrice": gas_price,
            "value": value_wei,
        })

        # Estimate gas
        try:
            gas_estimate = await _retry_rpc(
                lambda: w3.eth.estimate_gas(tx),
                "estimate_gas",
            )
            tx["gas"] = int(gas_estimate * 1.2)
        except Exception as exc:
            logger.warning(
                "Gas estimation failed for execute_plan, using config max_gas: %s",
                exc,
            )
            tx["gas"] = 500_000  # fallback from AppIntentConfig.max_gas default

        # Sign and send
        signed_hex = await self._wallet_manager.sign_transaction(wallet_address, dict(tx))
        tx_hash = await _retry_rpc(
            lambda: w3.eth.send_raw_transaction(bytes.fromhex(
                signed_hex[2:] if signed_hex.startswith("0x") else signed_hex
            )),
            "send_raw_transaction",
        )

        tx_hash_hex = f"0x{tx_hash.hex()}"
        logger.info(
            "Execution tx sent: %s (%s)",
            tx_hash_hex,
            get_tx_url(chain_id, tx_hash_hex),
        )

        return tx_hash_hex

    # -------------------------------------------------------------------
    # Convenience: wait for receipt
    # -------------------------------------------------------------------

    async def wait_for_receipt(
        self,
        tx_hash: str,
        chain_id: int,
        timeout: int = 120,
    ) -> TxReceipt:
        """
        Wait for a transaction receipt and return it.

        Raises ``TimeoutError`` (via web3) if the receipt is not available
        within *timeout* seconds.
        """
        w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        tx_bytes = bytes.fromhex(tx_hash[2:] if tx_hash.startswith("0x") else tx_hash)
        receipt = await _retry_rpc(
            lambda: w3.eth.wait_for_transaction_receipt(tx_bytes, timeout=timeout),
            "wait_for_transaction_receipt",
        )
        return receipt

    # -------------------------------------------------------------------
    # Convenience: read AppIntent state
    # -------------------------------------------------------------------

    async def get_intent_state(
        self,
        contract_address: str,
        chain_id: int,
    ) -> IntentState:
        """
        Read basic state from a deployed AppIntent contract.

        Returns an ``IntentState`` populated with ``nonce``, ``owner``,
        and additional fields in ``raw_params``.
        """
        w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        checksum = Web3.to_checksum_address(contract_address)
        contract = w3.eth.contract(address=checksum, abi=APP_INTENT_BASE_ABI)

        nonce = await _retry_rpc(
            lambda: contract.functions.nonce().call(),
            "nonce()",
        )
        owner = await _retry_rpc(
            lambda: contract.functions.owner().call(),
            "owner()",
        )
        threshold = await _retry_rpc(
            lambda: contract.functions.scoreThreshold().call(),
            "scoreThreshold()",
        )
        intent_type = await _retry_rpc(
            lambda: contract.functions.intentType().call(),
            "intentType()",
        )
        version = await _retry_rpc(
            lambda: contract.functions.version().call(),
            "version()",
        )

        return IntentState(
            contract_address=contract_address,
            chain_id=chain_id,
            nonce=nonce,
            owner=owner,
            raw_params={
                "score_threshold": threshold,
                "intent_type": intent_type,
                "version": version,
            },
        )

    # -------------------------------------------------------------------
    # Convenience: check canExecute
    # -------------------------------------------------------------------

    async def can_execute(
        self,
        contract_address: str,
        plan: ExecutionPlan,
        chain_id: int,
        caller: str | None = None,
    ) -> tuple[bool, str]:
        """
        Check whether a plan can be executed by calling ``canExecute()``.

        Returns ``(True, "")`` or ``(False, reason_string)``.
        """
        w3 = get_web3(chain_id, install_retry=False)  # _retry_rpc owns retries
        checksum = Web3.to_checksum_address(contract_address)
        contract = w3.eth.contract(address=checksum, abi=APP_INTENT_BASE_ABI)

        plan_tuple = _plan_to_solidity(plan)

        call_kwargs: dict[str, Any] = {}
        if caller:
            call_kwargs["from"] = Web3.to_checksum_address(caller)

        result = await _retry_rpc(
            lambda: contract.functions.canExecute(plan_tuple).call(call_kwargs),
            "canExecute()",
        )
        return (result[0], result[1])
