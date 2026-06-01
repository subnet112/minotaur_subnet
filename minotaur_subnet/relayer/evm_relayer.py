"""EVM Relayer — submits co-signed transactions to target chains.

Implements RelayerBase with real Web3 transaction submission.
Handles contract deployment, executeIntent() calls, and gas management.

Production hardening:
- Local nonce tracking prevents race conditions on concurrent submissions
- Retry with gas price bump on transient failures
- Pre-submission balance check prevents wasted gas on known-insufficient funds
- Dynamic gas estimation with safety margin
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .base import RelayerBase, SubmitResult
from .chain_config import ChainDeployment, EXECUTE_INTENT_ABI, VALIDATOR_REGISTRY_ABI
from .encoder import encode_intent_order, encode_execution_plan

logger = logging.getLogger(__name__)

# Retry configuration (overridable via env)
_MAX_RETRIES = int(os.environ.get("RELAYER_MAX_RETRIES", "2"))
_RETRY_DELAY_BASE = float(os.environ.get("RELAYER_RETRY_DELAY_BASE", "2.0"))
_GAS_BUMP_PERCENT = int(os.environ.get("RELAYER_GAS_BUMP_PERCENT", "20"))
_GAS_ESTIMATE_MULTIPLIER = float(os.environ.get("RELAYER_GAS_ESTIMATE_MULTIPLIER", "1.5"))
_MIN_BALANCE_WEI = int(os.environ.get("RELAYER_MIN_BALANCE_WEI", str(10**16)))  # 0.01 ETH


class NonceManager:
    """Thread-safe local nonce tracking per (chain_id, wallet).

    Prevents the race condition where two concurrent submissions both
    call get_transaction_count() and get the same nonce.
    """

    def __init__(self) -> None:
        self._nonces: dict[str, int] = {}  # key = f"{chain_id}:{wallet}"
        self._lock = threading.Lock()

    def get_and_increment(self, chain_id: int, wallet: str, w3: Any) -> int:
        """Get the next nonce and increment the local counter.

        On first call for a (chain, wallet) pair, syncs from the chain.
        Subsequent calls use the local counter (no RPC call).
        """
        key = f"{chain_id}:{wallet.lower()}"
        with self._lock:
            if key not in self._nonces:
                # Sync from chain on first use
                on_chain = w3.eth.get_transaction_count(wallet, "pending")
                self._nonces[key] = on_chain
                logger.info(
                    "Nonce synced from chain: %s nonce=%d", key, on_chain,
                )
            nonce = self._nonces[key]
            self._nonces[key] = nonce + 1
            return nonce

    def reset(self, chain_id: int, wallet: str, w3: Any) -> None:
        """Re-sync nonce from chain (after a failure or leader change)."""
        key = f"{chain_id}:{wallet.lower()}"
        with self._lock:
            on_chain = w3.eth.get_transaction_count(wallet, "pending")
            self._nonces[key] = on_chain
            logger.info("Nonce reset from chain: %s nonce=%d", key, on_chain)

    def clear(self) -> None:
        """Clear all tracked nonces (e.g., on leader change)."""
        with self._lock:
            self._nonces.clear()


class EvmRelayer(RelayerBase):
    """Real EVM relayer that submits transactions on-chain.

    Features:
    - Local nonce tracking (prevents race conditions)
    - Retry with gas price bump on transient failures
    - Pre-submission balance check
    - Dynamic gas estimation with safety margin

    Args:
        chains: Per-chain deployment configs.
        private_key: Relayer's signing key (used when wallet_manager is None).
        wallet_manager: Optional LitMpcWallet for MPC signing.
    """

    def __init__(
        self,
        chains: dict[int, ChainDeployment],
        private_key: str = "",
        wallet_manager: Any = None,
    ) -> None:
        self.chains = chains
        self.private_key = private_key
        self.wallet_manager = wallet_manager
        self._submissions: list[dict[str, Any]] = []
        self._nonce_manager = NonceManager()

        # Derive wallet address from private key for chains missing relayer_wallet
        if private_key:
            try:
                from eth_account import Account
                derived = Account.from_key(private_key).address
                for cfg in self.chains.values():
                    if not cfg.relayer_wallet:
                        cfg.relayer_wallet = derived
            except Exception:
                pass

    def _check_balance(self, w3: Any, wallet: str, chain_id: int) -> str | None:
        """Check relayer wallet balance. Returns error message if too low."""
        try:
            balance = w3.eth.get_balance(wallet)
            if balance < _MIN_BALANCE_WEI:
                balance_eth = balance / 10**18
                min_eth = _MIN_BALANCE_WEI / 10**18
                msg = (
                    f"Relayer balance too low on chain {chain_id}: "
                    f"{balance_eth:.4f} ETH < {min_eth:.4f} ETH minimum"
                )
                logger.warning(msg)
                return msg
        except Exception as exc:
            logger.warning("Balance check failed on chain %d: %s", chain_id, exc)
        return None

    def _estimate_gas(self, w3: Any, tx: dict) -> int:
        """Estimate gas with safety margin, falling back to default."""
        try:
            estimate = w3.eth.estimate_gas(tx)
            return int(estimate * _GAS_ESTIMATE_MULTIPLIER)
        except Exception:
            return 2_000_000  # Fallback

    def _get_gas_price(self, w3: Any, attempt: int = 0) -> int:
        """Get current gas price with bump for retries."""
        try:
            base_price = w3.eth.gas_price
        except Exception:
            base_price = w3.to_wei(20, "gwei")  # Fallback
        if attempt > 0:
            bump = 1 + (_GAS_BUMP_PERCENT * attempt / 100)
            return int(base_price * bump)
        return base_price

    async def submit_plan(
        self,
        order: Any,
        plan: Any,
        score: float,
        consensus_result: Any = None,
        contract_address: str | None = None,
    ) -> SubmitResult:
        """Submit an approved plan to the target chain.

        Runs the blocking web3 calls in a thread executor so the async
        event loop remains responsive during transaction confirmation.
        """
        import asyncio
        import concurrent.futures
        loop = asyncio.get_running_loop()
        # Use a dedicated executor with enough threads for concurrent submissions
        if not hasattr(self, '_executor'):
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        return await loop.run_in_executor(
            self._executor,
            lambda: self._submit_plan_sync(
                order, plan, score, consensus_result, contract_address,
            ),
        )

    def _submit_plan_sync(
        self,
        order: Any,
        plan: Any,
        score: float,
        consensus_result: Any = None,
        contract_address: str | None = None,
    ) -> SubmitResult:
        """Synchronous implementation of submit_plan (runs in thread executor)."""
        chain_id = getattr(order, "chain_id", 1)
        config = self.chains.get(chain_id)

        if config is None:
            return SubmitResult(
                success=False,
                error=f"Chain {chain_id} not configured",
                chain_id=chain_id,
            )

        app_address = contract_address or config.app_intent_base_address
        if not app_address:
            return SubmitResult(
                success=False,
                error=f"No AppIntentBase deployed on chain {chain_id}",
                chain_id=chain_id,
            )

        # Off-chain mirror of the on-chain AppRegistry gate. Refuse to spend
        # gas on an unregistered app — the contract's _requireRegistered()
        # would revert it anyway, but failing here saves the gas burn and
        # gives a structured error. No-op when APP_REGISTRY_{chain_id} is
        # unset (matches the contract's address(0) escape hatch).
        from minotaur_subnet.consensus.app_registry_cache import is_registered_app
        if not is_registered_app(app_address, chain_id):
            return SubmitResult(
                success=False,
                error=(
                    f"App contract {app_address} is not registered in the "
                    f"AppRegistry on chain {chain_id}"
                ),
                chain_id=chain_id,
            )

        # Warn when submitting to a real (non-Anvil) chain
        rpc_url = (config.rpc_url or "").lower()
        if chain_id in (1, 8453) and not any(
            kw in rpc_url for kw in ("anvil", "localhost", "127.0.0.1")
        ):
            logger.warning(
                "REAL CHAIN TX: chain_id=%d, app=%s, order=%s",
                chain_id,
                app_address,
                getattr(order, "order_id", "?"),
            )

        try:
            from minotaur_subnet.blockchain.chains import get_web3
            w3 = get_web3(chain_id)

            # Pre-submission balance check
            balance_err = self._check_balance(w3, config.relayer_wallet, chain_id)
            if balance_err:
                return SubmitResult(
                    success=False,
                    error=balance_err,
                    chain_id=chain_id,
                )

            contract = w3.eth.contract(
                address=w3.to_checksum_address(app_address),
                abi=EXECUTE_INTENT_ABI,
            )

            # Encode order and plan
            order_tuple = encode_intent_order(order)
            plan_tuple = encode_execution_plan(plan)

            # Collect validator signatures (sorted ascending by address)
            validator_sigs = []
            if consensus_result and hasattr(consensus_result, "approvals"):
                sorted_approvals = sorted(
                    consensus_result.approvals,
                    key=lambda a: int(
                        getattr(a, "validator_id", "0x0").replace("0x", ""), 16
                    ),
                )
                for approval in sorted_approvals:
                    sig = getattr(approval, "signature", "")
                    if sig:
                        validator_sigs.append(
                            bytes.fromhex(sig.replace("0x", ""))
                        )

            # User EIP-712 signature
            # For multi-leg executeLeg: send empty sig (0 bytes) so the
            # contract skips user signature verification (relies on quorum).
            # For single-leg executeIntent: send the user's real signature.
            user_sig_hex = getattr(order, "user_signature", "")
            _meta = plan.metadata or {}
            has_leg_index = "leg_index" in _meta or "cross_chain_leg_index" in _meta
            if has_leg_index:
                user_sig = b""  # empty = skip sig check in executeLeg
            elif user_sig_hex:
                user_sig = bytes.fromhex(user_sig_hex.replace("0x", ""))
            else:
                user_sig = b"\x00" * 65

            # Retry loop
            last_error = None
            for attempt in range(_MAX_RETRIES):
                try:
                    nonce = self._nonce_manager.get_and_increment(
                        chain_id, config.relayer_wallet, w3,
                    )
                    gas_price = self._get_gas_price(w3, attempt)

                    # Build transaction — use executeCrossChainLeg for dest legs
                    # Multi-leg: use executeLeg with leg_index for non-atomic execution
                    # Single-leg: use executeIntent for atomic same-chain intents
                    leg_index = _meta.get("leg_index")
                    if leg_index is None:
                        leg_index = _meta.get("cross_chain_leg_index")
                    is_multi_leg = leg_index is not None or _meta.get("phase") == "destination"

                    if is_multi_leg:
                        if leg_index is None:
                            leg_index = _meta.get("cross_chain_leg_index", 0)
                        call_fn = contract.functions.executeLeg(
                            order_tuple, plan_tuple, leg_index, user_sig, validator_sigs,
                        )
                    else:
                        call_fn = contract.functions.executeIntent(
                            order_tuple, plan_tuple, user_sig, validator_sigs,
                        )

                    # Calculate ETH value for plan calls (e.g., bridge IGP fees)
                    tx_value = sum(
                        int(ix.value) for ix in plan.interactions
                        if ix.value and str(ix.value) != "0"
                    )

                    gas_limit = self._estimate_gas(w3, {
                        "from": config.relayer_wallet,
                        "to": app_address,
                        "value": tx_value,
                        "data": call_fn._encode_transaction_data(),
                    })

                    # Protocol-fee gas-price cap. Bound the bid so the relayer
                    # can never pay more than the locked, validator-certified
                    # fee covers: at gas_limit gas the total spend is <=
                    # locked_fee. A post-certification gas spike above this
                    # makes the tx pend rather than execute at a loss — the
                    # relayer still submits (it cannot refuse a quorum-approved
                    # order), it just doesn't overpay. On floor-dominated chains
                    # (Base/BT EVM) the cap sits far above market so it never
                    # binds; on gas-dominated chains it sits ~the quote-time
                    # price.
                    _locked_fee = int((getattr(order, "params", None) or {}).get("platform_fee_wei", 0) or 0)
                    if _locked_fee > 0 and gas_limit > 0:
                        from minotaur_subnet import fee_policy
                        _cap = fee_policy.max_gas_price_wei(_locked_fee, gas_limit)
                        if 0 < _cap < gas_price:
                            logger.info(
                                "[RELAYER] Capping gas price %d -> %d "
                                "(fee %d / gas_limit %d) for order %s",
                                gas_price, _cap, _locked_fee, gas_limit,
                                getattr(order, "order_id", "?"),
                            )
                            gas_price = _cap

                    tx = call_fn.build_transaction({
                        "from": config.relayer_wallet,
                        "nonce": nonce,
                        "value": tx_value,
                        "gas": gas_limit,
                        "gasPrice": gas_price,
                        "chainId": chain_id,
                    })

                    # Sign and send
                    if self.wallet_manager:
                        import asyncio as _aio
                        signed_raw = _aio.run(self.wallet_manager.sign_transaction(
                            config.relayer_wallet, tx, chain_id,
                        ))
                        tx_hash = w3.eth.send_raw_transaction(
                            bytes.fromhex(signed_raw.replace("0x", ""))
                        )
                    else:
                        signed = w3.eth.account.sign_transaction(tx, self.private_key)
                        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

                    # Wait for receipt — short timeout to avoid blocking the event loop.
                    # Anvil mines every ~2s; mainnet confirmations take ~12s.
                    _receipt_timeout = int(os.environ.get("RELAYER_RECEIPT_TIMEOUT", "15"))
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=_receipt_timeout)

                    tx_success = receipt["status"] == 1
                    revert_reason = None
                    if not tx_success:
                        revert_reason = "Transaction reverted on-chain"
                        try:
                            w3.eth.call(tx, block_identifier=receipt["blockNumber"])
                        except Exception as revert_exc:
                            revert_reason = f"Transaction reverted: {revert_exc}"
                        logger.warning(
                            "On-chain revert (attempt %d/%d): tx=%s reason=%s gas=%d",
                            attempt + 1, _MAX_RETRIES,
                            tx_hash.hex()[:16], revert_reason[:200],
                            receipt["gasUsed"],
                        )
                        # Don't retry on-chain reverts — the plan itself is bad
                        return SubmitResult(
                            success=False,
                            tx_hash=tx_hash.hex(),
                            chain_id=chain_id,
                            block_number=receipt["blockNumber"],
                            gas_used=receipt["gasUsed"],
                            error=revert_reason,
                        )

                    # Success
                    result = SubmitResult(
                        success=True,
                        tx_hash=tx_hash.hex(),
                        chain_id=chain_id,
                        block_number=receipt["blockNumber"],
                        gas_used=receipt["gasUsed"],
                    )
                    self._submissions.append({
                        "order_id": getattr(order, "order_id", ""),
                        "tx_hash": result.tx_hash,
                        "chain_id": chain_id,
                        "gas_used": result.gas_used,
                        "timestamp": time.time(),
                    })
                    if attempt > 0:
                        logger.info(
                            "TX succeeded on attempt %d: %s",
                            attempt + 1, tx_hash.hex()[:16],
                        )
                    return result

                except Exception as exc:
                    last_error = exc
                    err_str = str(exc).lower()

                    # Nonce-related or timeout errors: reset nonce and retry
                    if "nonce" in err_str or "already known" in err_str or "not in the chain" in err_str or "timeout" in err_str:
                        logger.warning(
                            "Nonce/timeout error (attempt %d/%d): %s — resetting nonce",
                            attempt + 1, _MAX_RETRIES, exc,
                        )
                        self._nonce_manager.reset(chain_id, config.relayer_wallet, w3)
                        continue

                    # Underpriced: retry with gas bump
                    if "underpriced" in err_str or "replacement" in err_str:
                        logger.warning(
                            "TX underpriced (attempt %d/%d): %s — bumping gas",
                            attempt + 1, _MAX_RETRIES, exc,
                        )
                        continue

                    # Other transient errors: retry with delay
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_DELAY_BASE * (2 ** attempt)
                        logger.warning(
                            "TX failed (attempt %d/%d): %s — retrying in %.1fs",
                            attempt + 1, _MAX_RETRIES, exc, delay,
                        )
                        time.sleep(delay)
                        continue

                    # Final attempt failed
                    break

            # All retries exhausted
            import traceback
            tb = traceback.format_exc()
            logger.error(
                "All %d attempts failed for order on chain %d: %s\n%s",
                _MAX_RETRIES, chain_id, last_error, tb,
            )
            return SubmitResult(
                success=False,
                error=f"Failed after {_MAX_RETRIES} attempts: {last_error}",
                chain_id=chain_id,
            )

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logger.error(
                "Failed to submit plan on chain %d: %s\n%s",
                chain_id, exc, tb,
            )
            return SubmitResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                chain_id=chain_id,
            )

    def on_leader_changed(self, new_leader_id: str) -> int:
        """Drop in-flight submissions and reset nonces on leader change."""
        dropped = len(self._submissions)
        if dropped > 0:
            logger.info(
                "Leader changed to %s — dropping %d submissions, resetting nonces",
                new_leader_id[:10] if new_leader_id else "unknown", dropped,
            )
            self._submissions.clear()
        self._nonce_manager.clear()
        return dropped

    async def deploy_contract(
        self,
        bytecode: str,
        constructor_args: list[Any],
        chain_id: int,
    ) -> tuple[str, str]:
        """Deploy a contract on the target chain.

        Returns ``(contract_address, tx_hash)`` as hex strings.

        Raises if the deploy transaction reverts on-chain (out-of-gas,
        constructor revert, etc.). Earlier versions returned the would-be
        contractAddress without checking ``receipt.status``, which silently
        produced "deployed" addresses with no bytecode.
        """
        config = self.chains.get(chain_id)
        if config is None:
            raise ValueError(f"Chain {chain_id} not configured")

        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)

        nonce = self._nonce_manager.get_and_increment(
            chain_id, config.relayer_wallet, w3,
        )

        # Estimate gas with safety margin so we don't OOG-revert on chains
        # where contract creation costs grow over time. Falls back to a
        # generous default if estimate_gas itself fails (e.g. RPC issue).
        gas_limit = self._estimate_gas(w3, {
            "from": config.relayer_wallet,
            "data": bytecode,
        })
        # Floor at 6M for contract creation — the multiplier on a small
        # estimate underestimates real cost when the constructor runs heavy
        # initialisation (mappings, immutables, EIP-712 domain hashing).
        gas_limit = max(gas_limit, 6_000_000)

        tx = {
            "from": config.relayer_wallet,
            "nonce": nonce,
            "data": bytecode,
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
            "chainId": chain_id,
        }

        signed = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

        tx_hash_hex = tx_hash.hex()
        if receipt.get("status", 1) != 1:
            raise RuntimeError(
                f"Contract deploy reverted on chain {chain_id} "
                f"(tx={tx_hash_hex} gasUsed={receipt.get('gasUsed')} "
                f"of {gas_limit})"
            )

        address = receipt["contractAddress"]
        logger.info(
            "Contract deployed on chain %d at %s (tx: %s, gasUsed: %d/%d)",
            chain_id, address, tx_hash_hex,
            receipt.get("gasUsed", 0), gas_limit,
        )
        return address, tx_hash_hex

    async def register_intent(
        self,
        contract_address: str,
        selector: bytes,
        name: str,
        chain_id: int,
    ) -> str:
        """Call registerIntent(selector, name) on a deployed AppIntentBase."""
        config = self.chains.get(chain_id)
        if config is None:
            raise ValueError(f"Chain {chain_id} not configured")

        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)

        register_abi = [
            {
                "inputs": [
                    {"name": "selector", "type": "bytes4"},
                    {"name": "name", "type": "string"},
                ],
                "name": "registerIntent",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
        ]

        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_address),
            abi=register_abi,
        )

        nonce = self._nonce_manager.get_and_increment(
            chain_id, config.relayer_wallet, w3,
        )

        tx = contract.functions.registerIntent(
            selector, name,
        ).build_transaction({
            "from": config.relayer_wallet,
            "nonce": nonce,
            "gas": 100_000,
            "chainId": chain_id,
        })

        signed = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        return tx_hash.hex()

    async def sync_validators(
        self,
        chain_id: int,
        validators: list[str],
    ) -> str:
        """Update the validator set on a chain's ValidatorRegistry."""
        config = self.chains.get(chain_id)
        if config is None:
            raise ValueError(f"Chain {chain_id} not configured")

        if not config.validator_registry_address:
            raise ValueError(f"No ValidatorRegistry deployed on chain {chain_id}")

        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)

        contract = w3.eth.contract(
            address=w3.to_checksum_address(config.validator_registry_address),
            abi=VALIDATOR_REGISTRY_ABI,
        )

        nonce = self._nonce_manager.get_and_increment(
            chain_id, config.relayer_wallet, w3,
        )

        tx = contract.functions.updateValidators(
            validators,
        ).build_transaction({
            "from": config.relayer_wallet,
            "nonce": nonce,
            "gas": 200_000,
            "chainId": chain_id,
        })

        signed = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        return tx_hash.hex()

    async def get_tx_status(self, tx_hash: str, chain_id: int) -> dict:
        """Check status of a submitted transaction."""
        from minotaur_subnet.blockchain.chains import get_web3
        w3 = get_web3(chain_id)

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            return {
                "found": True,
                "status": "success" if receipt["status"] == 1 else "reverted",
                "block_number": receipt["blockNumber"],
                "gas_used": receipt["gasUsed"],
            }
        except Exception:
            return {"found": False, "status": "pending"}

    # ── Escrow management ─────────────────────────────────────────────────

    async def call_escrow_deposit(
        self,
        contract_address: str,
        chain_id: int,
        order_id: str,
        leg_index: int,
        token: str,
        amount: int,
        user: str,
        deadline: int,
    ) -> str:
        """Call escrowDeposit() on the destination chain contract.

        Returns TX hash on success, raises on failure.
        """
        from minotaur_subnet.blockchain.chains import get_web3
        from eth_abi import encode as abi_encode
        from eth_hash.auto import keccak

        w3 = get_web3(chain_id)
        wallet = self._resolve_wallet(chain_id)

        # escrowDeposit(bytes32 orderId, uint256 legIndex, address token,
        #               uint256 amount, address user, uint256 deadline)
        selector = keccak(
            b"escrowDeposit(bytes32,uint256,address,uint256,address,uint256)"
        )[:4]
        order_id_bytes = bytes.fromhex(order_id.replace("0x", "").zfill(64))
        params = abi_encode(
            ["bytes32", "uint256", "address", "uint256", "address", "uint256"],
            [order_id_bytes, leg_index,
             w3.to_checksum_address(token), amount,
             w3.to_checksum_address(user), deadline],
        )
        call_data = "0x" + selector.hex() + params.hex()

        nonce = self._nonce_manager.get_and_increment(chain_id, wallet, w3)
        tx = {
            "from": wallet,
            "to": w3.to_checksum_address(contract_address),
            "data": call_data,
            "nonce": nonce,
            "gas": 200_000,
            "gasPrice": self._get_gas_price(w3),
        }

        signed = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise RuntimeError(f"escrowDeposit reverted: tx={tx_hash.hex()}")

        logger.info("escrowDeposit OK: order=%s leg=%d amount=%d tx=%s",
                     order_id[:16], leg_index, amount, tx_hash.hex()[:16])
        return tx_hash.hex()

    async def call_escrow_release(
        self,
        contract_address: str,
        chain_id: int,
        order_id: str,
        leg_index: int,
        validator_signatures: list[str],
        release_hash: str,
    ) -> str:
        """Call escrowRelease() on the destination chain contract.

        Returns TX hash on success, raises on failure.
        """
        from minotaur_subnet.blockchain.chains import get_web3
        from eth_abi import encode as abi_encode
        from eth_hash.auto import keccak

        w3 = get_web3(chain_id)
        wallet = self._resolve_wallet(chain_id)

        # escrowRelease(bytes32 orderId, uint256 legIndex,
        #               bytes[] validatorSignatures, bytes32 releaseHash)
        selector = keccak(
            b"escrowRelease(bytes32,uint256,bytes[],bytes32)"
        )[:4]
        order_id_bytes = bytes.fromhex(order_id.replace("0x", "").zfill(64))
        release_hash_bytes = bytes.fromhex(release_hash.replace("0x", "").zfill(64))

        # Encode validator signatures as bytes[]
        sig_bytes_list = [bytes.fromhex(s.replace("0x", "")) for s in validator_signatures]
        params = abi_encode(
            ["bytes32", "uint256", "bytes[]", "bytes32"],
            [order_id_bytes, leg_index, sig_bytes_list, release_hash_bytes],
        )
        call_data = "0x" + selector.hex() + params.hex()

        nonce = self._nonce_manager.get_and_increment(chain_id, wallet, w3)
        tx = {
            "from": wallet,
            "to": w3.to_checksum_address(contract_address),
            "data": call_data,
            "nonce": nonce,
            "gas": 300_000,
            "gasPrice": self._get_gas_price(w3),
        }

        signed = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise RuntimeError(f"escrowRelease reverted: tx={tx_hash.hex()}")

        logger.info("escrowRelease OK: order=%s leg=%d tx=%s",
                     order_id[:16], leg_index, tx_hash.hex()[:16])
        return tx_hash.hex()

    def _resolve_wallet(self, chain_id: int) -> str:
        """Get the relayer wallet address for a chain."""
        config = self.chains.get(chain_id)
        if config and config.relayer_wallet:
            return config.relayer_wallet
        # Derive from private key
        if self.private_key:
            from eth_account import Account
            return Account.from_key(self.private_key).address
        raise ValueError(f"No relayer wallet for chain {chain_id}")
