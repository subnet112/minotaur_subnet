"""BridgeTracker — monitors in-flight bridge transfers and submits destination legs.

After the source leg of a cross-chain order is relayed, the BridgeTracker
polls the bridge adapter for completion.  Once the bridge reports COMPLETED,
the tracker extracts the destination-leg plan and submits it via the relayer.

Lifecycle: BRIDGING → (bridge completes) → dest leg submitted → FILLED
           BRIDGING → (bridge fails)     → BRIDGE_FAILED
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.bridge.base import BridgeStatus, BridgeStatusEnum
from minotaur_subnet.orderbook.orderbook import OrderStatus
from minotaur_subnet.shared.types import ExecutionPlan, extract_leg_plan

logger = logging.getLogger(__name__)


@dataclass
class TrackedBridge:
    """A single in-flight bridge transfer being monitored."""
    order_id: str
    src_tx_hash: str
    plan: ExecutionPlan
    src_chain_id: int
    dst_chain_id: int
    bridge_protocol: str
    tracked_at: float = field(default_factory=time.time)
    poll_count: int = 0
    max_polls: int = 120  # ~2 hours at 60s interval


class BridgeTracker:
    """Polls bridge adapters and completes cross-chain orders.

    Args:
        bridge_registry: Registry of bridge adapters.
        orderbook: Intent OrderBook for status updates.
        relayer: Relayer for submitting destination legs.
        poll_interval: Seconds between polling cycles.
    """

    def __init__(
        self,
        bridge_registry: Any,
        orderbook: Any,
        relayer: Any,
        poll_interval: float = 60.0,
        consensus: Any = None,
        simulator: Any = None,
    ) -> None:
        self.bridge_registry = bridge_registry
        self.orderbook = orderbook
        self.relayer = relayer
        self.poll_interval = poll_interval
        self.consensus = consensus
        self.simulator = simulator

        self._tracked: dict[str, TrackedBridge] = {}
        self._running = False

    def track(
        self,
        order_id: str,
        src_tx_hash: str,
        plan: ExecutionPlan,
    ) -> None:
        """Register a bridge transfer for monitoring.

        Called by BlockLoop after the source leg is successfully relayed.
        """
        meta = plan.metadata
        self._tracked[order_id] = TrackedBridge(
            order_id=order_id,
            src_tx_hash=src_tx_hash,
            plan=plan,
            src_chain_id=meta.get("src_chain_id", 1),
            dst_chain_id=meta.get("dst_chain_id", 1),
            bridge_protocol=meta.get("bridge_protocol", "mock"),
        )
        logger.info(
            "Tracking bridge for order %s: %s %d→%d (tx=%s)",
            order_id,
            meta.get("bridge_protocol", "?"),
            meta.get("src_chain_id", 0),
            meta.get("dst_chain_id", 0),
            src_tx_hash[:16],
        )

    @property
    def tracked_count(self) -> int:
        """Number of bridges currently being tracked."""
        return len(self._tracked)

    def get_tracking_info(self, order_id: str) -> dict | None:
        """Get tracking info for an order. Returns None if not tracked."""
        tracked = self._tracked.get(order_id)
        if tracked is None:
            return None
        return {
            "poll_count": tracked.poll_count,
            "max_polls": tracked.max_polls,
            "tracked_since": tracked.tracked_at,
            "bridge_protocol": tracked.bridge_protocol,
            "src_chain_id": tracked.src_chain_id,
            "dst_chain_id": tracked.dst_chain_id,
        }

    async def poll_once(self) -> int:
        """Check all tracked bridges once. Returns count of completed bridges."""
        if not self._tracked:
            return 0

        completed = 0
        to_remove: list[str] = []

        for order_id, tracked in list(self._tracked.items()):
            tracked.poll_count += 1

            # Max polls exceeded → mark as failed
            if tracked.poll_count > tracked.max_polls:
                logger.warning(
                    "Bridge timeout for order %s after %d polls",
                    order_id, tracked.poll_count,
                )
                self._mark_bridge_failed(order_id, "Bridge polling timeout")
                to_remove.append(order_id)
                continue

            adapter = self.bridge_registry.get(tracked.bridge_protocol)
            if adapter is None:
                logger.warning(
                    "No adapter for protocol %s (order %s)",
                    tracked.bridge_protocol, order_id,
                )
                continue

            try:
                status = await adapter.check_status(
                    tracked.src_tx_hash, tracked.src_chain_id,
                    dst_chain_id=tracked.dst_chain_id,
                )

                if status.status == BridgeStatusEnum.COMPLETED:
                    await self._on_bridge_complete(tracked, status)
                    to_remove.append(order_id)
                    completed += 1
                elif status.status == BridgeStatusEnum.FAILED:
                    error = status.error or "Bridge transfer failed"
                    self._mark_bridge_failed(order_id, error)
                    to_remove.append(order_id)
                # PENDING / IN_TRANSIT → keep polling
            except NotImplementedError:
                # Stub adapter (e.g. Tensorplex) — skip until real integration
                logger.debug(
                    "Bridge status check not implemented for %s",
                    tracked.bridge_protocol,
                )
            except Exception as exc:
                logger.warning(
                    "Bridge status check failed for order %s: %s",
                    order_id, exc,
                )

        for oid in to_remove:
            self._tracked.pop(oid, None)

        return completed

    async def _on_bridge_complete(
        self,
        tracked: TrackedBridge,
        bridge_status: BridgeStatus,
    ) -> None:
        """Bridge finished — execute remaining destination legs."""
        logger.info(
            "Bridge complete for order %s (dst_tx=%s)",
            tracked.order_id,
            getattr(bridge_status, "dst_tx_hash", "?"),
        )
        print(f"[BRIDGE] Bridge complete for {tracked.order_id}", flush=True)

        order = self.orderbook.get(tracked.order_id)
        if order is None:
            logger.warning("Order %s disappeared from orderbook", tracked.order_id)
            return

        meta = tracked.plan.metadata
        contract_address = meta.get("contract_address") or order.params.get("app_address")

        # Verify bridged tokens actually arrived on destination chain
        dst_chain_id = tracked.dst_chain_id
        if dst_chain_id and contract_address:
            try:
                from minotaur_subnet.blockchain.chains import get_web3
                from web3 import Web3
                dst_w3 = get_web3(dst_chain_id)
                # Check bridge token balance at contract address
                multi_leg = meta.get("multi_leg_plan", {})
                forward_legs = multi_leg.get("forward_legs", [])
                bridge_leg = next((l for l in forward_legs if l.get("metadata", {}).get("type") == "bridge_source"), None)
                if bridge_leg:
                    bridge_token = bridge_leg.get("metadata", {}).get("bridge_token_out", "")
                    if bridge_token:
                        bal_data = "0x70a08231" + contract_address[2:].lower().zfill(64)
                        result = dst_w3.eth.call({
                            "to": Web3.to_checksum_address(bridge_token),
                            "data": bal_data,
                        })
                        balance = int.from_bytes(result, "big")
                        print(f"[BRIDGE] Token {bridge_token[:10]} balance at {contract_address[:10]} on chain {dst_chain_id}: {balance}", flush=True)
                        if balance == 0:
                            logger.warning("Bridge delivery verification: zero token balance at contract")
            except Exception as exc:
                logger.debug("Bridge delivery verification failed: %s", exc)

        # Multi-leg plan stored by blockloop when registering bridge
        remaining_legs_data = meta.get("remaining_legs", [])
        if remaining_legs_data:
            await self._execute_remaining_legs(
                tracked, order, remaining_legs_data, contract_address,
            )
            return

        # Legacy: substrate-origin bridge (no multi-leg plan)
        await self._execute_legacy_dest_leg(tracked, order, contract_address)

    async def _execute_remaining_legs(
        self,
        tracked: TrackedBridge,
        order: Any,
        remaining_legs_data: list[dict],
        contract_address: str | None,
    ) -> None:
        """Execute remaining forward legs after bridge delivery.

        Called by the bridge tracker when a multi-leg order's bridge completes.
        Picks up where the blockloop left off — simulates, gets consensus,
        and submits each remaining leg.
        """
        from minotaur_subnet.shared.types import LegPlan, ExecutionPlan

        remaining_legs = [LegPlan.from_dict(d) for d in remaining_legs_data]
        print(f"[BRIDGE] Executing {len(remaining_legs)} remaining legs for {tracked.order_id}", flush=True)

        self.orderbook.update_order(
            tracked.order_id, status="executing_leg",
        )

        for leg in remaining_legs:
            leg_label = f"leg {leg.leg_index} (chain {leg.chain_id})"
            print(f"[BRIDGE] {tracked.order_id}: processing {leg_label}", flush=True)

            # Build ExecutionPlan for this leg
            leg_plan = ExecutionPlan(
                intent_id=order.app_id,
                interactions=leg.interactions,
                deadline=int(order.deadline),
                nonce=0,
                metadata={
                    **leg.metadata,
                    "leg_index": leg.leg_index,
                    "chain_id": leg.chain_id,
                },
            )

            # Simulate destination leg before consensus
            if self.simulator is not None and contract_address:
                try:
                    intent_order_dict = {
                        "order_id": order.order_id,
                        "app": contract_address,
                        "intent_selector": leg.intent_selector,
                        "intent_params": leg.intent_params_hex,
                        "submitted_by": order.submitted_by,
                        "chain_id": leg.chain_id,
                        "deadline": int(order.deadline),
                        "nonce": 0,
                        "perpetual": False,
                        "max_executions": 1,
                        "cooldown": 0,
                    }
                    sim = await self.simulator.simulate(
                        leg_plan,
                        contract_address=contract_address,
                        intent_order=intent_order_dict,
                    )
                    if sim and not sim.success:
                        logger.warning("Dest leg simulation failed for %s %s", tracked.order_id, leg_label)
                        self._mark_bridge_failed(tracked.order_id, f"Dest leg {leg.leg_index} simulation failed")
                        return
                    print(f"[BRIDGE] {tracked.order_id}: {leg_label} simulation passed", flush=True)
                except Exception as exc:
                    logger.warning("Dest leg simulation error: %s", exc)

            # Escrow: deposit bridged tokens and get validator release
            escrow_params_list = tracked.plan.metadata.get("escrow_params", [])
            escrow_for_leg = next(
                (ep for ep in escrow_params_list if ep.get("leg_index") == leg.leg_index),
                None,
            )
            if escrow_for_leg and contract_address and hasattr(self.relayer, "call_escrow_deposit"):
                try:
                    # Step 1: Deposit bridged tokens into escrow
                    await self.relayer.call_escrow_deposit(
                        contract_address=contract_address,
                        chain_id=leg.chain_id,
                        order_id=escrow_for_leg["order_id"],
                        leg_index=escrow_for_leg["leg_index"],
                        token=escrow_for_leg["token"],
                        amount=escrow_for_leg["amount"],
                        user=escrow_for_leg["user"],
                        deadline=escrow_for_leg["deadline"],
                    )
                    print(f"[BRIDGE] {tracked.order_id}: {leg_label} escrow deposited", flush=True)

                    # Step 2: Get validator consensus for escrow release
                    # The release hash = keccak(orderId, legIndex, token, amount)
                    from eth_hash.auto import keccak
                    from eth_abi import encode as abi_encode
                    order_id_bytes = bytes.fromhex(escrow_for_leg["order_id"].replace("0x", "").zfill(64))
                    release_hash = keccak(abi_encode(
                        ["bytes32", "uint256", "address", "uint256"],
                        [order_id_bytes, escrow_for_leg["leg_index"],
                         escrow_for_leg["token"], escrow_for_leg["amount"]],
                    ))

                    # Get validator signatures for release via consensus
                    release_sigs = []
                    if self.consensus is not None:
                        try:
                            release_result = await self.consensus.propose(
                                tracked.order_id, leg_plan, 0.6,
                                "0x" + release_hash.hex(),
                                chain_id=leg.chain_id,
                                contract_address=contract_address,
                            )
                            if release_result and release_result.reached:
                                release_sigs = [a.signature for a in release_result.approvals]
                        except Exception as exc:
                            logger.warning("Escrow release consensus failed: %s", exc)

                    if release_sigs:
                        await self.relayer.call_escrow_release(
                            contract_address=contract_address,
                            chain_id=leg.chain_id,
                            order_id=escrow_for_leg["order_id"],
                            leg_index=escrow_for_leg["leg_index"],
                            validator_signatures=release_sigs,
                            release_hash="0x" + release_hash.hex(),
                        )
                        print(f"[BRIDGE] {tracked.order_id}: {leg_label} escrow released", flush=True)
                    else:
                        logger.warning("No release signatures for escrow, proceeding without")

                except Exception as exc:
                    logger.warning("Escrow flow failed for %s: %s", tracked.order_id, exc)
                    print(f"[BRIDGE] {tracked.order_id}: escrow error: {exc}", flush=True)

            # Get consensus for destination leg execution
            consensus_result = None
            if self.consensus is not None and contract_address:
                try:
                    from minotaur_subnet.consensus.signatures import hash_plan
                    from copy import copy
                    plan_hash = hash_plan(leg_plan)

                    # Create per-leg order with correct intent selector + params
                    leg_order = copy(order)
                    leg_params = dict(order.params)
                    leg_params["intent_selector"] = leg.intent_selector
                    if leg.intent_params_hex:
                        leg_params["intent_params_hex"] = leg.intent_params_hex
                    leg_order.params = leg_params

                    consensus_result = await self.consensus.propose(
                        tracked.order_id, leg_plan, 0.6, plan_hash,
                        chain_id=leg.chain_id,
                        contract_address=contract_address,
                    )
                    reached = consensus_result.reached if consensus_result else False
                    print(f"[BRIDGE] {tracked.order_id}: {leg_label} consensus reached={reached}", flush=True)
                except Exception as exc:
                    logger.warning("Bridge dest leg consensus failed: %s", exc)
                    print(f"[BRIDGE] {tracked.order_id}: {leg_label} consensus error: {exc}", flush=True)

            # Submit via relayer with per-leg params
            from copy import copy as _copy
            submit_order = _copy(order)
            submit_params = dict(order.params)
            submit_params["intent_selector"] = leg.intent_selector
            if leg.intent_params_hex:
                submit_params["intent_params_hex"] = leg.intent_params_hex
            submit_order.params = submit_params

            try:
                submit_result = await self.relayer.submit_plan(
                    submit_order, leg_plan, 0.6, consensus_result,
                    contract_address=contract_address,
                )
                print(f"[BRIDGE] {tracked.order_id}: {leg_label} submit success={submit_result.success} tx={submit_result.tx_hash} err={submit_result.error}", flush=True)
            except Exception as exc:
                logger.error("Bridge dest leg submit failed: %s", exc)
                self._mark_bridge_failed(tracked.order_id, f"Dest leg failed: {exc}")
                return

            if not submit_result.success:
                self._mark_bridge_failed(
                    tracked.order_id,
                    f"Dest leg {leg.leg_index} failed: {submit_result.error}",
                )
                return

        self._mark_filled(tracked.order_id, submit_result.tx_hash)

    async def _execute_legacy_dest_leg(
        self,
        tracked: TrackedBridge,
        order: Any,
        contract_address: str | None,
    ) -> None:
        """Legacy path for substrate-origin bridges without multi-leg plans."""
        legs = tracked.plan.metadata.get("legs", [])
        dest_leg = next(
            (l for l in legs if l.get("type") == "destination"), None,
        )
        if dest_leg is None:
            self._mark_filled(tracked.order_id, tracked.src_tx_hash)
            return

        dest_plan = extract_leg_plan(tracked.plan, dest_leg["leg_id"])
        dest_plan.metadata["phase"] = "destination"
        dest_plan.metadata["cross_chain_leg_index"] = dest_leg["leg_id"]

        try:
            if tracked.plan.metadata.get("substrate_origin"):
                print(f"[BRIDGE] Seeding bridged tokens for {tracked.order_id}", flush=True)
                await self._seed_bridged_tokens(tracked, order, contract_address)

            consensus_result = None
            if self.consensus is not None and contract_address:
                try:
                    from minotaur_subnet.consensus.signatures import hash_plan
                    plan_hash = hash_plan(dest_plan)
                    consensus_result = await self.consensus.propose(
                        tracked.order_id, dest_plan, order.score or 0.0, plan_hash,
                        chain_id=order.chain_id, contract_address=contract_address,
                    )
                except Exception as exc:
                    logger.warning("Dest leg consensus failed: %s", exc)

            submit_result = await self.relayer.submit_plan(
                order, dest_plan, order.score or 0.0, consensus_result,
                contract_address=contract_address,
            )
            if submit_result.success:
                self._mark_filled(tracked.order_id, submit_result.tx_hash)
            else:
                self._mark_bridge_failed(
                    tracked.order_id,
                    f"Dest leg submission failed: {submit_result.error}",
                )
        except Exception as exc:
            logger.error(
                "Failed to submit dest leg for order %s: %s",
                tracked.order_id, exc,
            )
            self._mark_bridge_failed(tracked.order_id, str(exc))

    async def _seed_bridged_tokens(
        self,
        tracked: TrackedBridge,
        order: Any,
        contract_address: str | None,
    ) -> None:
        """Seed the user and contract with bridged wTAO for the dest swap.

        On testnet, the mock bridge doesn't actually deliver tokens.
        This simulates what a real bridge would do: deliver wTAO to the
        user's address on Ethereum so the DexAggregator can pull it.
        """
        try:
            legs = tracked.plan.metadata.get("legs", [])
            bridge_leg = next((l for l in legs if l.get("type") == "bridge"), None)
            if not bridge_leg:
                return

            amount = int(bridge_leg.get("estimated_output", 0))
            wTAO = bridge_leg.get("token_out", "0x77E06c9eCCf2E797fd462A92B6D7642EF85b0A44")
            if not amount:
                return

            from minotaur_subnet.blockchain.chains import get_web3
            from web3 import Web3

            chain_id = order.chain_id or 31337
            w3 = get_web3(chain_id)
            user = Web3.to_checksum_address(order.submitted_by)
            token = Web3.to_checksum_address(wTAO)

            # Deal wTAO to the user via Anvil cheat code (impersonate + mint)
            # First, impersonate a whale or use deal
            w3.provider.make_request("anvil_impersonateAccount", [user])
            w3.provider.make_request("anvil_setBalance", [user, hex(10**18)])

            # Use deal to set wTAO balance
            from eth_hash.auto import keccak
            balance_of_sig = "0x70a08231" + user[2:].lower().zfill(64)
            amount_hex = hex(amount)[2:].zfill(64)
            user_padded = user[2:].lower().zfill(64)

            for slot in range(11):
                slot_hex = hex(slot)[2:].zfill(64)
                key_input = bytes.fromhex(user_padded + slot_hex)
                storage_key = "0x" + keccak(key_input).hex()
                w3.provider.make_request("anvil_setStorageAt", [token, storage_key, "0x" + amount_hex])
                result = w3.eth.call({"to": token, "data": balance_of_sig})
                if int.from_bytes(result, "big") == amount:
                    break

            # Approve contract to spend wTAO
            if contract_address:
                spender = Web3.to_checksum_address(contract_address)
                approve_data = "0x095ea7b3" + spender[2:].lower().zfill(64) + "ff" * 32
                tx_hash = w3.eth.send_transaction({"from": user, "to": token, "data": approve_data, "gas": 100_000})
                w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10)

            w3.provider.make_request("anvil_stopImpersonatingAccount", [user])

            print(f"[BRIDGE] Seeded {amount} wTAO to {user[:16]} + approved {contract_address[:16] if contract_address else 'none'}", flush=True)
        except Exception as exc:
            print(f"[BRIDGE] Seed failed: {exc}", flush=True)
            import traceback; traceback.print_exc()

    def _mark_filled(self, order_id: str, tx_hash: str | None) -> None:
        """Mark order as FILLED after successful cross-chain execution."""
        self.orderbook.update_order(
            order_id,
            status=OrderStatus.FILLED,
            tx_hash=tx_hash,
        )
        logger.info("Cross-chain order %s → FILLED", order_id)

    def _mark_bridge_failed(self, order_id: str, error: str) -> None:
        """Mark order as BRIDGE_FAILED."""
        self.orderbook.update_order(
            order_id,
            status=OrderStatus.BRIDGE_FAILED,
            error=error,
        )
        logger.warning("Cross-chain order %s → BRIDGE_FAILED: %s", order_id, error)

    async def run_loop(self) -> None:
        """Background polling loop. Runs until stop() is called."""
        self._running = True
        logger.info("BridgeTracker started (poll_interval=%.0fs)", self.poll_interval)
        while self._running:
            try:
                completed = await self.poll_once()
                if completed > 0:
                    logger.info("BridgeTracker: %d bridges completed this cycle", completed)
            except Exception as exc:
                logger.error("BridgeTracker poll error: %s", exc, exc_info=True)
            await asyncio.sleep(self.poll_interval)
        logger.info("BridgeTracker stopped")

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False
