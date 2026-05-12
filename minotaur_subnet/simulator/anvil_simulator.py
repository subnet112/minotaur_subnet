"""AnvilSimulator — executes execution plans on a running Anvil fork.

Uses snapshot/revert for isolation: each simulate() call leaves no
lasting state changes on the fork.

Requires a running Anvil instance (local testnet or standalone).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from web3 import Web3

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
    extract_leg_plan,
)
from minotaur_subnet.simulator.revert_decoder import (
    decode_call,
    extract_revert_via_trace,
)

logger = logging.getLogger(__name__)

# ERC-20 Transfer(address,address,uint256) event topic
# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = bytes.fromhex(
    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Default executor address (Anvil account 0, pre-funded with 10,000 ETH)
_DEFAULT_EXECUTOR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


class AnvilSimulator:
    """Simulates execution plans on a running Anvil fork.

    Each simulation:
    1. Takes an EVM snapshot
    2. Impersonates the executor (vault/relayer/user)
    3. Funds the executor if needed
    4. Executes each interaction as a transaction
    5. Captures ERC-20 Transfer events and gas usage
    6. Reverts the snapshot (no lasting state change)

    Args:
        rpc_url: Anvil JSON-RPC endpoint (e.g., http://localhost:8545).
        default_executor: Address to execute from if not specified in plan.
        fund_executor: Whether to auto-fund the executor with ETH.
    """

    def __init__(
        self,
        rpc_url: str,
        default_executor: str = _DEFAULT_EXECUTOR,
        fund_executor: bool = True,
        sim_timeout: float = 30.0,
        upstream_rpc_url: str | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.default_executor = Web3.to_checksum_address(default_executor)
        self.fund_executor = fund_executor
        self.sim_timeout = sim_timeout
        # Upstream RPC the local Anvil is forking from (e.g. Alchemy
        # Base mainnet). Used by _reset_fork to advance the fork to
        # the current upstream head before each simulation. Without
        # this, anvil_reset({}) silently no-ops back to the original
        # fork-block (a foundry quirk) and simulations run against
        # stale state. Optional — local-testnet sims (chain 31337,
        # not forked from anything) leave this unset and skip the
        # head-fetch path entirely.
        self.upstream_rpc_url = (upstream_rpc_url or "").strip() or None
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

        if not self.w3.is_connected():
            logger.warning("Anvil not reachable at %s", rpc_url)
        else:
            block = self.w3.eth.block_number
            logger.info(
                "AnvilSimulator connected: %s (block %d, upstream=%s)",
                rpc_url, block,
                "configured" if self.upstream_rpc_url else "none (fork stays static)",
            )

    async def simulate(
        self,
        plan: ExecutionPlan,
        contract_address: str | None = None,
        intent_order: dict | None = None,
        token_balances: dict[str, int] | None = None,
        fork_block: int | None = None,
    ) -> SimulationResult:
        """Execute a plan against the Anvil fork and return results.

        The plan's interactions are executed sequentially. All ERC-20
        Transfer events are captured from transaction receipts.

        The executor address comes from plan.metadata["executor"],
        falling back to the default executor.

        Args:
            plan: The execution plan to simulate.
            contract_address: Optional app contract for on-chain scoring.
            intent_order: Optional order dict for on-chain scoring.
            token_balances: Optional {token_address: amount_wei} to seed
                the executor with ERC-20 balances before simulation.
                Used by quote to ensure the executor has input tokens.
            fork_block: Optional historical block number. When set, the
                anvil fork rewinds to this block BEFORE simulating — used
                for Stage-2 historical-order replays so pool prices
                match the state at which the original order was filled.
                Default None = reset to upstream latest.
        """
        # SIM-10: Graceful fallback when Anvil is unavailable
        if not self.is_connected():
            logger.warning("Anvil unreachable at %s — returning failed simulation", self.rpc_url)
            return SimulationResult(
                success=False,
                gas_used=0,
                on_chain_score=None,
                error="Anvil unavailable",
            )

        # Re-fork at upstream head (or at fork_block for historical
        # replays) so each simulation sees the right pool state.
        self._reset_fork(block_number=fork_block)

        executor = plan.metadata.get("executor", self.default_executor)
        executor = Web3.to_checksum_address(executor)

        snap_id = self._snapshot()
        try:
            # ── Primary path: scoreIntent via contract ────────────────────
            # When we have the app contract and intent order, call scoreIntent
            # as a real transaction (impersonating the relayer). This mirrors
            # the actual executeIntent flow: the contract deploys a proxy,
            # pulls user tokens (which must be approved), executes plan calls
            # via the proxy, checks invariants, and returns a score.
            # Transfer events come from the receipt.
            if contract_address and intent_order:
                ip = intent_order.get('intent_params', '')
                ip_preview = ip[:40] if isinstance(ip, str) else ip[:20].hex() if isinstance(ip, bytes) else str(ip)[:40]
                print(f"[SIM] scoreIntent path: contract={contract_address[:10]}... user={intent_order.get('submitted_by','?')[:10]}... intent_params_len={len(ip) if isinstance(ip, (str,bytes)) else '?'} preview={ip_preview}", flush=True)
                result = self._simulate_via_score_intent(
                    contract_address, intent_order, plan, token_balances,
                )
                if result is not None:
                    print(f"[SIM] scoreIntent result: success={result.success} gas={result.gas_used} transfers={len(result.token_transfers or [])} on_chain_score={result.on_chain_score}", flush=True)
                    return result
                # Fail closed. The manual-interaction fallback runs plan calls
                # directly from a funded executor, bypassing the contract's
                # proxy deploy / platform fee / invariant checks, which
                # inflates scores vs. real on-chain behavior and caused
                # validator divergence on ord_c88ce65d20764dee. The fallback
                # is only legitimate when no contract is provided (quotes).
                print("[SIM] scoreIntent reverted — fail closed (no fallback when contract provided)", flush=True)
                logger.warning("scoreIntent reverted — refusing to fall back to manual sim")
                return SimulationResult(
                    success=False,
                    gas_used=0,
                    error="scoreIntent simulation reverted",
                )

            # ── Fallback: manual interaction execution ────────────────────
            # Used when no contract is deployed (quotes, dry-runs). Runs plan
            # interactions one by one from the executor address.
            self._impersonate(executor)
            if self.fund_executor:
                self._fund(executor, 100 * 10**18)

            # Deal ERC-20 token balances to executor (for quotes / dry-runs)
            if token_balances:
                for token_addr, amount in token_balances.items():
                    ok = self._deal_erc20(token_addr, executor, amount)
                    if not ok:
                        logger.warning(
                            "Token deal failed: %s → %s (amount=%s). "
                            "Simulation may revert due to insufficient balance.",
                            token_addr, executor, amount,
                        )

            total_gas = 0
            all_transfers: list[TokenTransfer] = []
            state_changes: list[dict[str, Any]] = []

            eth_before = self.w3.eth.get_balance(executor)

            for i, ix in enumerate(plan.interactions):
                try:
                    receipt = self._execute_interaction(ix, executor)
                    total_gas += receipt["gasUsed"]
                    transfers = self._parse_transfer_events(receipt)
                    all_transfers.extend(transfers)
                    logger.debug(
                        "Interaction %d/%d: target=%s gas=%d transfers=%d",
                        i + 1,
                        len(plan.interactions),
                        ix.target,
                        receipt["gasUsed"],
                        len(transfers),
                    )
                except Exception as exc:
                    logger.warning(
                        "Interaction %d/%d failed: %s", i + 1, len(plan.interactions), exc
                    )
                    return SimulationResult(
                        success=False,
                        gas_used=total_gas,
                        error=f"Interaction {i + 1} failed: {exc}",
                        token_transfers=all_transfers,
                    )

            eth_after = self.w3.eth.get_balance(executor)
            eth_delta = eth_before - eth_after
            state_changes.append({
                "type": "balance_change",
                "address": executor,
                "token": "ETH",
                "delta": str(eth_delta),
            })

            price_impact = self._estimate_price_impact(all_transfers)

            return SimulationResult(
                success=True,
                gas_used=total_gas,
                token_transfers=all_transfers,
                state_changes=state_changes,
                price_impact=price_impact,
            )

        except Exception as exc:
            logger.error("Simulation error: %s", exc, exc_info=True)
            return SimulationResult(
                success=False,
                gas_used=0,
                error=str(exc),
            )
        finally:
            self._revert(snap_id)
            self._stop_impersonating(executor)

    def _simulate_via_score_intent(
        self,
        contract_address: str,
        intent_order: dict,
        plan: ExecutionPlan,
        token_balances: dict[str, int] | None = None,
    ) -> SimulationResult | None:
        """Call scoreIntent as a real transaction to simulate the full flow.

        Impersonates the contract's relayer, sends scoreIntent(order, plan)
        as a transaction, and captures Transfer events + gas from the receipt.
        This mirrors executeIntent exactly: proxy deploy, token pull, plan
        execution, invariant check, score return.

        Returns SimulationResult on success, None if setup fails (caller
        should fall back to manual interaction execution).
        """
        try:
            from eth_abi import encode as abi_encode, decode as abi_decode
            from eth_hash.auto import keccak

            target = Web3.to_checksum_address(contract_address)

            # Resolve the contract's relayer address
            relayer_sig = keccak(b"relayer()")[:4]
            relayer_result = self.w3.eth.call({
                "to": target,
                "data": "0x" + relayer_sig.hex(),
            })
            relayer_addr = Web3.to_checksum_address(
                "0x" + relayer_result[-20:].hex()
            )

            # Impersonate relayer and fund with ETH for gas
            print(f"[SIM] impersonating relayer {relayer_addr}", flush=True)
            self._impersonate(relayer_addr)
            self._fund(relayer_addr, 100 * 10**18)
            # Verify impersonation works
            try:
                test_tx = self.w3.eth.send_transaction({"from": relayer_addr, "to": relayer_addr, "value": 0, "gas": 21000})
                print(f"[SIM] impersonation verified: {test_tx.hex()[:16]}...", flush=True)
            except Exception as imp_err:
                print(f"[SIM] impersonation FAILED: {imp_err}", flush=True)

            # Seed user tokens if needed (for scenarios where fork state is stale)
            if token_balances:
                submitted_by = intent_order.get("submitted_by", "")
                if submitted_by:
                    for tok, amt in token_balances.items():
                        self._deal_erc20(tok, submitted_by, amt)
                        self._set_erc20_allowance(tok, submitted_by, target, 2**256 - 1)
                    # Re-impersonate relayer — _set_erc20_allowance may have
                    # stopped impersonating if submitted_by == relayer
                    self._impersonate(relayer_addr)

            # Build scoreIntent calldata
            sig = "scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,uint256,bytes))"
            selector = keccak(sig.encode())[:4]

            order_id = intent_order.get("order_id", b"\x00" * 32)
            if isinstance(order_id, str):
                # Order IDs like "ord_abc123" aren't hex — hash them to bytes32
                try:
                    order_id = bytes.fromhex(order_id.replace("0x", "").ljust(64, "0"))[:32]
                except ValueError:
                    order_id = keccak(order_id.encode())

            app_addr = intent_order.get("app", contract_address)
            intent_sel = intent_order.get("intent_selector", b"\x00" * 4)
            if isinstance(intent_sel, str):
                intent_sel = bytes.fromhex(intent_sel.replace("0x", ""))[:4]

            intent_params = intent_order.get("intent_params", b"")
            if isinstance(intent_params, str):
                if intent_params.startswith("0x"):
                    intent_params = bytes.fromhex(intent_params[2:])
                else:
                    intent_params = bytes.fromhex(intent_params) if all(c in '0123456789abcdefABCDEF' for c in intent_params) else intent_params.encode()

            submitted_by = intent_order.get("submitted_by", "0x" + "00" * 20)
            chain_id = intent_order.get("chain_id", 1)
            deadline = intent_order.get("deadline", 0)
            nonce = intent_order.get("nonce", 0)
            perpetual = intent_order.get("perpetual", False)
            max_executions = intent_order.get("max_executions", 1)
            cooldown = intent_order.get("cooldown", 0)

            # Build ExecutionPlan calls
            calls = []
            for ix in plan.interactions:
                cd = ix.call_data
                if isinstance(cd, str):
                    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if cd else b""
                calls.append((
                    Web3.to_checksum_address(ix.target),
                    int(ix.value) if ix.value else 0,
                    cd,
                ))

            plan_metadata = b""
            if plan.metadata:
                import json as _json
                plan_metadata = _json.dumps(plan.metadata).encode()

            encoded = abi_encode(
                [
                    "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
                    "((address,uint256,bytes)[],uint256,uint256,bytes)",
                ],
                [
                    (
                        order_id,
                        Web3.to_checksum_address(app_addr),
                        intent_sel,
                        intent_params,
                        Web3.to_checksum_address(submitted_by),
                        chain_id,
                        deadline,
                        nonce,
                        perpetual,
                        max_executions,
                        cooldown,
                    ),
                    (calls, plan.deadline, plan.nonce, plan_metadata),
                ],
            )

            calldata = "0x" + (selector + encoded).hex()

            # For native ETH input (user submits with msg.value), the contract
            # expects msg.value > 0 to trigger the wrap path in _fundAndExecute.
            # Without this, the ERC-20 safeTransferFrom path runs and reverts.
            tx_value = 0
            if intent_order and intent_order.get("_input_token_is_native"):
                try:
                    tx_value = int(intent_order.get("_input_amount", 0))
                except (ValueError, TypeError):
                    pass

            # Send as a raw RPC call (bypasses Web3.py's signer middleware)
            tx_params = {
                "from": relayer_addr,
                "to": target,
                "data": calldata,
                "gas": hex(2_000_000),
            }
            if tx_value > 0:
                tx_params["value"] = hex(tx_value)
            raw_result = self.w3.provider.make_request("eth_sendTransaction", [tx_params])
            tx_hash_hex = raw_result.get("result", "")
            if not tx_hash_hex:
                print(f"[SIM] scoreIntent send_tx failed: {raw_result}", flush=True)
                return None
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash_hex, timeout=30)

            if receipt["status"] == 0:
                # Try to get revert reason via eth_call
                revert_reason = "unknown"
                try:
                    self.w3.eth.call({
                        "from": relayer_addr,
                        "to": target,
                        "data": calldata,
                        "gas": 2_000_000,
                    })
                except Exception as revert_exc:
                    revert_reason = str(revert_exc)
                print(f"[SIM] scoreIntent REVERTED: {revert_reason}", flush=True)
                return None

            # Parse transfer events and gas
            all_transfers = self._parse_transfer_events(receipt)
            total_gas = receipt["gasUsed"]

            # Decode return value for on-chain score
            # scoreIntent returns (uint256 score, bool valid)
            on_chain_score = None
            try:
                ret = self.w3.eth.call({
                    "from": relayer_addr,
                    "to": target,
                    "data": calldata,
                    "gas": 2_000_000,
                })
                score_val, valid = abi_decode(["uint256", "bool"], ret)
                on_chain_score = score_val if valid else None
            except Exception:
                pass

            price_impact = self._estimate_price_impact(all_transfers)

            logger.info(
                "scoreIntent simulation: gas=%d transfers=%d on_chain_score=%s",
                total_gas, len(all_transfers), on_chain_score,
            )

            return SimulationResult(
                success=True,
                gas_used=total_gas,
                token_transfers=all_transfers,
                price_impact=price_impact,
                on_chain_score=on_chain_score,
            )

        except Exception as exc:
            import traceback
            print(f"[SIM] scoreIntent exception: {exc}", flush=True)
            traceback.print_exc()
            logger.warning("scoreIntent simulation failed: %s", exc)
            return None

    def _reset_fork(self, block_number: int | None = None) -> None:
        """Reset the Anvil fork to re-fetch all state from upstream RPC.

        Calls ``anvil_reset`` with an explicit ``forking.blockNumber``.
        When ``block_number`` is None we fetch the current upstream
        head (via self.upstream_rpc_url) and reset to that block — this
        is the default path, used so every current-state simulation
        sees fresh pool prices + sees any contracts deployed since the
        anvil container started.

        When a block number is provided, the fork rewinds to THAT block
        instead — used by historical-order replays so the strategy's
        plan is evaluated against pool prices as they were when the
        original order was filled. Requires an archive-capable upstream.

        Subtle: ``anvil_reset`` with empty params ``[{}]`` is a no-op
        in Foundry — the fork stays at its initial block. The explicit
        ``forking.blockNumber`` is what actually advances the fork.
        That's why the upstream-head fetch is required.

        If self.upstream_rpc_url is unset (e.g., local-testnet chain
        31337 which isn't forked from anything), this no-ops gracefully
        — local Anvil already has the state we want.
        """
        if block_number is None:
            if not self.upstream_rpc_url:
                # No upstream configured (local-testnet or test path).
                # Skip the reset; local Anvil state is authoritative.
                return
            try:
                block_number = self._fetch_upstream_head()
            except Exception as exc:
                logger.warning(
                    "Could not fetch upstream head for fork reset (upstream=%s): %s",
                    self.upstream_rpc_url, exc,
                )
                # Best-effort: leave fork at its current block. Better
                # than a half-reset that leaves Anvil in an inconsistent
                # state.
                return

        try:
            params = [{"forking": {"blockNumber": int(block_number)}}]
            self.w3.provider.make_request("anvil_reset", params)
        except Exception as exc:
            logger.warning("anvil_reset failed (block=%s): %s", block_number, exc)

    def _fetch_upstream_head(self) -> int:
        """Query the upstream RPC for the current head block number."""
        if not self.upstream_rpc_url:
            raise RuntimeError("No upstream_rpc_url configured")
        resp = requests.post(
            self.upstream_rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            timeout=5,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        if not result:
            raise RuntimeError(f"Upstream RPC returned no result: {resp.text[:200]}")
        return int(result, 16)

    def _snapshot(self) -> str:
        """Take an EVM state snapshot."""
        result = self.w3.provider.make_request("evm_snapshot", [])
        snap_id = result.get("result", "0x0")
        logger.debug("Snapshot taken: %s", snap_id)
        return snap_id

    def _revert(self, snap_id: str) -> None:
        """Revert to a previous snapshot."""
        self.w3.provider.make_request("evm_revert", [snap_id])
        logger.debug("Reverted to snapshot: %s", snap_id)

    def _impersonate(self, address: str) -> None:
        """Impersonate an account on Anvil."""
        self.w3.provider.make_request("anvil_impersonateAccount", [address])

    def _stop_impersonating(self, address: str) -> None:
        """Stop impersonating an account."""
        try:
            self.w3.provider.make_request(
                "anvil_stopImpersonatingAccount", [address]
            )
        except Exception:
            pass  # Best-effort cleanup

    def _fund(self, address: str, amount_wei: int) -> None:
        """Fund an address with ETH via Anvil cheat code."""
        self.w3.provider.make_request(
            "anvil_setBalance", [address, hex(amount_wei)]
        )

    def _deal_erc20(self, token: str, to: str, amount: int) -> bool:
        """Set an ERC-20 token balance for an address via Anvil cheat code.

        Uses the standard ERC-20 balanceOf storage slot discovery:
        tries common mapping slots (0-10) by computing
        keccak256(abi.encode(address, slot)) and checking if the
        balance changes after writing.

        Returns True if the balance was successfully set, False otherwise.
        """
        token = Web3.to_checksum_address(token)
        to = Web3.to_checksum_address(to)

        # Read current balance via balanceOf(address)
        balance_of_sig = "0x70a08231" + to[2:].lower().zfill(64)
        try:
            current = self.w3.eth.call({"to": token, "data": balance_of_sig})
            current_balance = int.from_bytes(current, "big")
        except Exception:
            logger.warning(
                "Cannot read balanceOf(%s) for token %s — deal skipped", to, token,
            )
            return False

        # Try standard mapping slots 0-10
        amount_hex = hex(amount)[2:].zfill(64)
        to_padded = to[2:].lower().zfill(64)

        for slot in range(11):
            # Storage key = keccak256(abi.encode(address, uint256(slot)))
            slot_hex = hex(slot)[2:].zfill(64)
            key_input = bytes.fromhex(to_padded + slot_hex)
            from eth_hash.auto import keccak
            storage_key = "0x" + keccak(key_input).hex()

            self.w3.provider.make_request(
                "anvil_setStorageAt",
                [token, storage_key, "0x" + amount_hex],
            )

            # Verify it worked
            try:
                result = self.w3.eth.call({"to": token, "data": balance_of_sig})
                new_balance = int.from_bytes(result, "big")
                if new_balance == amount:
                    logger.info(
                        "Dealt %s of %s to %s (slot %d)", amount, token, to, slot
                    )
                    return True
            except Exception:
                pass

            # Revert this slot's write if it didn't work
            self.w3.provider.make_request(
                "anvil_setStorageAt",
                [token, storage_key, "0x" + hex(current_balance)[2:].zfill(64)],
            )

        logger.warning(
            "Could not find balanceOf slot for %s — deal failed "
            "(tried slots 0-10, amount=%s, to=%s)",
            token, amount, to,
        )
        return False

    def _set_erc20_allowance(
        self, token: str, owner: str, spender: str, amount: int,
    ) -> None:
        """Set ERC-20 allowance via Anvil impersonation + approve() call.

        Faster and more reliable than trying to find the allowance storage
        slot (which is a nested mapping: keccak(spender . keccak(owner . slot))).
        """
        token = Web3.to_checksum_address(token)
        owner = Web3.to_checksum_address(owner)
        spender = Web3.to_checksum_address(spender)

        try:
            self._impersonate(owner)
            self._fund(owner, 10**18)  # Need ETH for gas
            # approve(address spender, uint256 amount)
            approve_data = (
                "0x095ea7b3"
                + spender[2:].lower().zfill(64)
                + hex(amount)[2:].zfill(64)
            )
            tx_hash = self.w3.eth.send_transaction({
                "from": owner,
                "to": token,
                "data": approve_data,
                "gas": 100_000,
            })
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10)
            self._stop_impersonating(owner)
        except Exception as exc:
            logger.warning("Set allowance failed: %s → %s for %s: %s", owner, spender, token, exc)
            try:
                self._stop_impersonating(owner)
            except Exception:
                pass

    def _erc20_balance(self, token: str, holder: str) -> int:
        """Read ERC-20 balanceOf via eth_call. Returns 0 on any error."""
        try:
            data = bytes.fromhex("70a08231") + bytes.fromhex(holder.lower().replace("0x", "").rjust(64, "0"))
            out = self.w3.eth.call({
                "to": Web3.to_checksum_address(token), "data": data,
            })
            return int.from_bytes(bytes(out), "big") if out else 0
        except Exception:
            return 0

    def _erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        """Read ERC-20 allowance(owner,spender) via eth_call. Returns 0 on error."""
        try:
            data = (
                bytes.fromhex("dd62ed3e")
                + bytes.fromhex(owner.lower().replace("0x", "").rjust(64, "0"))
                + bytes.fromhex(spender.lower().replace("0x", "").rjust(64, "0"))
            )
            out = self.w3.eth.call({
                "to": Web3.to_checksum_address(token), "data": data,
            })
            return int.from_bytes(bytes(out), "big") if out else 0
        except Exception:
            return 0

    def _snapshot_state(
        self, executor: str, tokens: list[str], allowance_target: str | None,
    ) -> dict[str, Any]:
        """Capture executor balances + allowance to a target."""
        snap: dict[str, Any] = {"executor": executor, "balances": {}}
        for t in tokens:
            if not t:
                continue
            try:
                snap["balances"][t.lower()] = str(self._erc20_balance(t, executor))
            except Exception:
                snap["balances"][t.lower()] = "?"
        if allowance_target:
            snap["allowances"] = {}
            for t in tokens:
                if not t:
                    continue
                try:
                    snap["allowances"][t.lower()] = str(
                        self._erc20_allowance(t, executor, allowance_target),
                    )
                except Exception:
                    snap["allowances"][t.lower()] = "?"
        return snap

    def simulate_with_trace(
        self,
        plan: Any,
        token_balances: dict[str, int] | None = None,
        focus_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run plan via the manual-execution path with rich per-step trace.

        For deep debugging of revert mysteries: returns per-interaction
        snapshots of executor balances + allowances to each target,
        decoded function names, gas, status, and revert reason. Uses the
        simulator's snapshot/revert isolation so it's safe to call.

        Args:
            plan: ExecutionPlan to trace.
            token_balances: optional input-token funding for the executor.
            focus_tokens: which tokens to snapshot (default: all unique
                ERC-20s referenced as targets in the plan plus any in
                ``token_balances``).
        """
        from minotaur_subnet.simulator.revert_decoder import decode_call

        executor = self.default_executor
        focus = list(focus_tokens or [])
        for ix in plan.interactions:
            if ix.target and Web3.to_checksum_address(ix.target) not in (focus + [executor]):
                focus.append(ix.target)
        for t in (token_balances or {}).keys():
            if t not in focus:
                focus.append(t)

        snapshot_id = self._snapshot()
        try:
            self._impersonate(executor)
            if self.fund_executor:
                self._fund(executor, 100 * 10**18)
            if token_balances:
                for token_addr, amount in token_balances.items():
                    self._deal_erc20(token_addr, executor, amount)

            interactions_trace: list[dict[str, Any]] = []
            total_gas = 0
            for i, ix in enumerate(plan.interactions):
                cd = ix.call_data or "0x"
                cd_hex = cd[2:] if isinstance(cd, str) and cd.startswith("0x") else (cd if isinstance(cd, str) else cd.hex())
                cd_bytes = bytes.fromhex(cd_hex) if cd_hex else b""
                fn = decode_call(cd_bytes)
                pre = self._snapshot_state(executor, focus, ix.target)
                try:
                    receipt = self._execute_interaction(ix, executor)
                    post = self._snapshot_state(executor, focus, ix.target)
                    total_gas += receipt["gasUsed"]
                    interactions_trace.append({
                        "index": i,
                        "target": ix.target,
                        "fn": fn,
                        "calldata": cd if isinstance(cd, str) else "0x" + cd_hex,
                        "value": str(ix.value or 0),
                        "status": "ok",
                        "gas_used": receipt["gasUsed"],
                        "pre_state": pre,
                        "post_state": post,
                    })
                except Exception as exc:
                    interactions_trace.append({
                        "index": i,
                        "target": ix.target,
                        "fn": fn,
                        "calldata": cd if isinstance(cd, str) else "0x" + cd_hex,
                        "value": str(ix.value or 0),
                        "status": "reverted",
                        "revert_reason": str(exc)[:400],
                        "gas_used": 0,
                        "pre_state": pre,
                    })
                    return {
                        "interactions": interactions_trace,
                        "total_gas": total_gas,
                        "summary": (
                            f"reverted at step {i + 1}/{len(plan.interactions)}: "
                            f"{str(exc)[:200]}"
                        ),
                    }
            return {
                "interactions": interactions_trace,
                "total_gas": total_gas,
                "summary": (
                    f"all {len(plan.interactions)} interactions succeeded; "
                    f"gas={total_gas}"
                ),
            }
        finally:
            try:
                self._stop_impersonating(executor)
            except Exception:
                pass
            self._revert(snapshot_id)

    def _execute_interaction(
        self, ix: Any, sender: str
    ) -> dict[str, Any]:
        """Execute a single plan interaction as a transaction."""
        value = int(ix.value) if ix.value else 0
        call_data = ix.call_data if ix.call_data and ix.call_data != "0x" else b""

        # Convert hex string calldata to bytes if needed
        if isinstance(call_data, str):
            if call_data.startswith("0x"):
                call_data = bytes.fromhex(call_data[2:])
            elif call_data:
                call_data = bytes.fromhex(call_data)
            else:
                call_data = b""

        target = Web3.to_checksum_address(ix.target)

        tx = {
            "from": sender,
            "to": target,
            "value": value,
            "data": call_data,
            # 1M gas: multi-hop Uniswap V3 swaps use ~150k per hop
            "gas": 1_000_000,
        }

        tx_hash = self.w3.eth.send_transaction(tx)
        # Mine immediately to avoid waiting for block time
        self.w3.provider.make_request("evm_mine", [])
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.sim_timeout)

        if receipt["status"] != 1:
            # Decode the failure: function selector from calldata + revert
            # reason from debug_traceTransaction. Without this, every
            # mystery revert costs ~$2 of LLM time guessing what broke.
            fn = decode_call(call_data)
            reason = extract_revert_via_trace(self.w3, tx_hash) or "no revert data"
            raise RuntimeError(
                f"Transaction reverted: target={ix.target} fn={fn} "
                f"reason={reason} value={ix.value}"
            )

        return dict(receipt)

    def _parse_transfer_events(self, receipt: dict) -> list[TokenTransfer]:
        """Parse ERC-20 Transfer events from a transaction receipt."""
        transfers: list[TokenTransfer] = []

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue

            # Check if this is an ERC-20 Transfer event
            topic0 = topics[0]
            if isinstance(topic0, bytes):
                topic0_bytes = topic0
            elif isinstance(topic0, str):
                topic0_bytes = bytes.fromhex(
                    topic0[2:] if topic0.startswith("0x") else topic0
                )
            else:
                continue

            if topic0_bytes != _TRANSFER_TOPIC or len(topics) < 3:
                continue

            # Decode indexed parameters
            token = log.get("address", "")
            from_addr = _topic_to_address(topics[1])
            to_addr = _topic_to_address(topics[2])

            # Decode non-indexed amount from log data
            data = log.get("data", "0x")
            if isinstance(data, bytes):
                amount = int.from_bytes(data, "big") if data else 0
            elif isinstance(data, str):
                hex_data = data[2:] if data.startswith("0x") else data
                amount = int(hex_data, 16) if hex_data else 0
            else:
                amount = 0

            transfers.append(
                TokenTransfer(
                    token=token,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    amount=str(amount),
                )
            )

        return transfers

    def _call_score_intent(
        self,
        contract_address: str,
        intent_order: dict,
        plan: ExecutionPlan,
        sender: str,
    ) -> int | None:
        """Call scoreIntent(order, plan) on the app contract.

        Returns the BPS score (0-10000) if valid, None if invalid or failed.
        The intent_order dict must contain the Solidity IntentOrder struct fields.
        """
        try:
            from eth_abi import encode as abi_encode, decode as abi_decode
            from eth_hash.auto import keccak

            # scoreIntent((IntentOrder),(ExecutionPlan)) selector
            sig = "scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,uint256,bytes))"
            selector = keccak(sig.encode())[:4]

            # Encode IntentOrder tuple
            order_id = intent_order.get("order_id", b"\x00" * 32)
            if isinstance(order_id, str):
                try:
                    order_id = bytes.fromhex(order_id.replace("0x", "").ljust(64, "0"))[:32]
                except ValueError:
                    order_id = keccak(order_id.encode())

            app_addr = intent_order.get("app", "0x" + "00" * 20)
            intent_sel = intent_order.get("intent_selector", b"\x00" * 4)
            if isinstance(intent_sel, str):
                intent_sel = bytes.fromhex(intent_sel.replace("0x", ""))[:4]
            intent_params = intent_order.get("intent_params", b"")
            if isinstance(intent_params, str):
                intent_params = intent_params.encode()
            submitted_by = intent_order.get("submitted_by", "0x" + "00" * 20)
            chain_id = intent_order.get("chain_id", 1)
            deadline = intent_order.get("deadline", 0)
            nonce = intent_order.get("nonce", 0)
            perpetual = intent_order.get("perpetual", False)
            max_executions = intent_order.get("max_executions", 1)
            cooldown = intent_order.get("cooldown", 0)

            # Build calls tuple list for ExecutionPlan
            calls = []
            for ix in plan.interactions:
                cd = ix.call_data
                if isinstance(cd, str):
                    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if cd else b""
                calls.append((
                    Web3.to_checksum_address(ix.target),
                    int(ix.value) if ix.value else 0,
                    cd,
                ))

            plan_deadline = plan.deadline
            plan_nonce = plan.nonce
            plan_metadata = b""
            if plan.metadata:
                import json
                plan_metadata = json.dumps(plan.metadata).encode()

            # ABI-encode the full calldata
            encoded = abi_encode(
                [
                    "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
                    "((address,uint256,bytes)[],uint256,uint256,bytes)",
                ],
                [
                    (
                        order_id,
                        Web3.to_checksum_address(app_addr),
                        intent_sel,
                        intent_params,
                        Web3.to_checksum_address(submitted_by),
                        chain_id,
                        deadline,
                        nonce,
                        perpetual,
                        max_executions,
                        cooldown,
                    ),
                    (calls, plan_deadline, plan_nonce, plan_metadata),
                ],
            )

            calldata = selector + encoded
            target = Web3.to_checksum_address(contract_address)

            # scoreIntent has onlyRelayer modifier — resolve and impersonate
            # the contract's relayer address instead of using the executor.
            score_sender = sender
            try:
                relayer_result = self.w3.eth.call({
                    "to": target,
                    "data": "0x" + keccak(b"relayer()")[:4].hex(),
                })
                relayer_addr = Web3.to_checksum_address(
                    "0x" + relayer_result[-20:].hex()
                )
                self._impersonate(relayer_addr)
                self._fund(relayer_addr, 10 ** 18)
                score_sender = relayer_addr
            except Exception:
                pass  # Fall back to executor if relayer() call fails

            result = self.w3.eth.call({
                "from": score_sender,
                "to": target,
                "data": "0x" + calldata.hex(),
                "gas": 500_000,
            })

            # Decode (uint256 score, bool valid)
            score, valid = abi_decode(["uint256", "bool"], result)
            return score if valid else None

        except Exception as exc:
            logger.debug("scoreIntent call failed: %s", exc)
            return None

    def _estimate_price_impact(
        self, transfers: list[TokenTransfer]
    ) -> float:
        """Estimate price impact from token transfers (rough heuristic)."""
        if len(transfers) < 2:
            return 0.0
        # For MVP: return a small default impact
        return 0.003

    def is_connected(self) -> bool:
        """Check if the Anvil instance is reachable."""
        try:
            return self.w3.is_connected()
        except Exception:
            return False


class MultiChainSimulator:
    """Routes simulations to the correct AnvilSimulator by chain_id.

    Wraps one AnvilSimulator per chain so that orders targeting different
    chains are simulated against the correct fork state.

    Usage::

        sim = MultiChainSimulator({
            31337: "http://anvil:8545",
            8453:  "http://anvil-base:8546",
        })
        result = await sim.simulate(plan)  # routes by plan chain_id
    """

    def __init__(
        self,
        rpc_urls: dict[int, str],
        default_chain_id: int = 31337,
        upstream_rpc_urls: dict[int, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.simulators: dict[int, AnvilSimulator] = {}
        self.default_chain_id = default_chain_id
        upstream_rpc_urls = upstream_rpc_urls or {}

        for chain_id, url in rpc_urls.items():
            try:
                sim = AnvilSimulator(
                    rpc_url=url,
                    upstream_rpc_url=upstream_rpc_urls.get(chain_id),
                    **kwargs,
                )
                self.simulators[chain_id] = sim
                logger.info(
                    "MultiChainSimulator: chain %d → %s (upstream %s)",
                    chain_id, url,
                    "configured" if upstream_rpc_urls.get(chain_id) else "none",
                )
            except Exception as exc:
                logger.warning(
                    "MultiChainSimulator: failed to init chain %d: %s",
                    chain_id, exc,
                )

    def _get_simulator(self, plan: ExecutionPlan) -> AnvilSimulator | None:
        """Resolve the correct simulator for a plan's chain.

        Resolution order:
        1. plan.metadata["chain_id"] — explicit hint
        2. plan.interactions[0].chain_id — inferred from the plan itself
        3. self.default_chain_id — last-resort (typically local testnet)
        """
        chain_id = plan.metadata.get("chain_id")
        if chain_id is None and plan.interactions:
            # Fallback: infer from the plan's first interaction. Callers
            # (including /v1/apps/{id}/score) don't always stuff chain_id
            # into metadata, but every Interaction carries it.
            chain_id = plan.interactions[0].chain_id
        if chain_id is None:
            chain_id = self.default_chain_id
        if isinstance(chain_id, str):
            try:
                chain_id = int(chain_id)
            except ValueError:
                chain_id = self.default_chain_id

        sim = self.simulators.get(chain_id)
        if sim is None:
            sim = self.simulators.get(self.default_chain_id)
        return sim

    async def simulate(
        self,
        plan: ExecutionPlan,
        **kwargs: Any,
    ) -> SimulationResult:
        """Simulate a plan on the correct chain's Anvil fork."""
        sim = self._get_simulator(plan)
        if sim is None:
            chain_id = plan.metadata.get("chain_id", self.default_chain_id)
            return SimulationResult(
                success=False,
                gas_used=0,
                error=f"No simulator configured for chain {chain_id}",
            )
        return await sim.simulate(plan, **kwargs)

    async def simulate_cross_chain(
        self,
        plan: ExecutionPlan,
        bridge_registry: Any = None,
        **kwargs: Any,
    ) -> SimulationResult:
        """Simulate a cross-chain plan by running each leg independently.

        Source and destination legs are simulated on their respective chain
        forks.  Bridge legs are not simulated — a quote estimate is used
        instead.  Falls back to single-chain ``simulate()`` when the plan
        has no ``metadata["legs"]``.

        Args:
            plan: Execution plan (may contain ``metadata["legs"]``).
            bridge_registry: Optional ``BridgeRegistry`` for bridge quotes.
            **kwargs: Forwarded to per-chain ``AnvilSimulator.simulate()``.

        Returns:
            Combined ``SimulationResult`` with ``leg_results`` and
            ``bridge_estimate`` populated.
        """
        legs = plan.metadata.get("legs")
        if not legs:
            return await self.simulate(plan, **kwargs)

        leg_results: dict[int, Any] = {}
        bridge_estimate: dict[str, Any] | None = None

        for leg in sorted(legs, key=lambda l: l["leg_id"]):
            leg_id = leg["leg_id"]
            leg_chain = leg.get("chain_id", self.default_chain_id)
            leg_plan = extract_leg_plan(plan, leg_id)

            # Skip substrate legs — they execute extrinsics, not EVM txs.
            # Substrate operations are deterministic (valid or not), so
            # simulation isn't needed. The proxy executor validates before exec.
            if leg.get("runtime") == "substrate":
                leg_results[leg_id] = {
                    "success": True,
                    "type": "substrate",
                    "skipped": True,
                    "reason": "Substrate legs are not simulated on Anvil",
                }
                # For bridge legs with substrate runtime, extract bridge estimate
                if leg.get("type") == "bridge":
                    est = leg.get("estimated_output")
                    fee = leg.get("fee")
                    token_out = leg.get("token_out")
                    if est:
                        bridge_estimate = {
                            "protocol": leg.get("bridge_protocol", "tensorplex"),
                            "token_out": token_out or "",
                            "estimated_output": int(est),
                            "fee": int(fee) if fee else 0,
                        }
                continue

            # Skip wait legs (bridge finality placeholder)
            if leg.get("type") == "wait" or leg.get("runtime") == "none":
                leg_results[leg_id] = {
                    "success": True,
                    "type": "wait",
                    "skipped": True,
                }
                continue

            if leg.get("type") == "bridge":
                # Don't simulate bridge — use quote estimate
                if bridge_registry is not None:
                    try:
                        token_in = plan.metadata.get("bridge_token", "")
                        amount = plan.metadata.get("bridge_amount", 0)
                        src = plan.metadata.get("src_chain_id", 1)
                        dst = plan.metadata.get("dst_chain_id", 1)
                        quote = await bridge_registry.best_quote(
                            token_in, int(amount), src, dst,
                        )
                        if quote:
                            bridge_estimate = {
                                "protocol": quote.protocol,
                                "token_in": quote.token_in,
                                "token_out": quote.token_out,
                                "amount_in": quote.amount_in,
                                "estimated_output": quote.estimated_output,
                                "fee": quote.fee,
                                "estimated_duration_s": quote.estimated_duration_s,
                            }
                    except Exception as exc:
                        logger.warning("Bridge quote failed: %s", exc)
                        bridge_estimate = {"error": str(exc)}
                leg_results[leg_id] = {
                    "success": True,
                    "type": "bridge",
                    "bridge_estimate": bridge_estimate,
                }
                continue

            sim = self.simulators.get(leg_chain)
            if sim is None:
                leg_results[leg_id] = {
                    "success": False,
                    "error": f"No simulator for chain {leg_chain}",
                    "gas_used": 0,
                }
                continue

            # Seed destination legs with bridged token balance.
            # Bridge legs are not simulated on-chain, so the dest
            # fork has no bridged tokens — deal them to the executor.
            leg_kwargs = dict(kwargs)
            if leg.get("type") == "destination" and bridge_estimate:
                token_out = bridge_estimate.get("token_out", "")
                est_output = bridge_estimate.get("estimated_output", 0)
                if token_out and est_output:
                    existing = leg_kwargs.get("token_balances") or {}
                    leg_kwargs["token_balances"] = {
                        **existing, token_out: int(est_output),
                    }

            result = await sim.simulate(leg_plan, **leg_kwargs)
            leg_results[leg_id] = {
                "success": result.success,
                "gas_used": result.gas_used,
                "error": result.error,
                "token_transfers": [
                    {"token": t.token, "from": t.from_addr,
                     "to": t.to_addr, "amount": t.amount}
                    for t in (result.token_transfers or [])
                ],
            }

        # Combine into single SimulationResult
        all_transfers: list[TokenTransfer] = []
        total_gas = 0
        all_success = True
        first_error = None

        for lr in leg_results.values():
            total_gas += lr.get("gas_used", 0)
            if not lr.get("success", False):
                all_success = False
                if first_error is None:
                    first_error = lr.get("error")
            for t in lr.get("token_transfers", []):
                all_transfers.append(TokenTransfer(
                    token=t["token"],
                    from_addr=t["from"],
                    to_addr=t["to"],
                    amount=t["amount"],
                ))

        return SimulationResult(
            success=all_success,
            gas_used=total_gas,
            error=first_error,
            token_transfers=all_transfers,
            leg_results=leg_results,
            bridge_estimate=bridge_estimate,
        )

    def is_connected(self) -> bool:
        """True if at least one chain simulator is connected."""
        return any(s.is_connected() for s in self.simulators.values())


def _topic_to_address(topic: Any) -> str:
    """Extract an Ethereum address from a log topic (last 20 bytes)."""
    if isinstance(topic, bytes):
        return Web3.to_checksum_address("0x" + topic[-20:].hex())
    if isinstance(topic, str):
        hex_str = topic[2:] if topic.startswith("0x") else topic
        return Web3.to_checksum_address("0x" + hex_str[-40:])
    return ""
