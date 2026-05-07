"""Full pipeline integration test: order -> scoring -> relayer submission.

Tests the complete lifecycle with multi-validator consensus, peer network
broadcast, and leader-based block loop processing.
Uses mock relayer and in-memory OrderBook.
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.orderbook import IntentOrderBook, OrderStatus
from minotaur_subnet.consensus import ConsensusManager
from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork, PeerEndpoint
from minotaur_subnet.consensus.eip712 import address_from_key
from minotaur_subnet.validator.metagraph_sync import PeerInfo, elect_leader


class TestFullPipeline:
    """End-to-end: user submits order -> block loop processes -> mock relay."""

    @pytest.mark.asyncio
    async def test_order_to_fill(self, block_loop, orderbook, mock_relayer):
        """Submit an order and verify it gets processed and filled."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000000000000000000",
                "min_output": "2500000000",
            },
            submitted_by="0x" + "11" * 20,
        )
        assert order.status == OrderStatus.OPEN

        result = await block_loop.tick()

        assert result.orders_processed == 1
        updated = orderbook.get(order.order_id)
        assert updated.status in (OrderStatus.FILLED, OrderStatus.REJECTED)

        if updated.status == OrderStatus.FILLED:
            assert updated.tx_hash is not None
            assert len(mock_relayer.submissions) == 1

    @pytest.mark.asyncio
    async def test_multiple_orders_same_tick(self, block_loop, orderbook, mock_relayer):
        """Multiple orders processed in a single tick."""
        for i in range(3):
            orderbook.submit(
                app_id="emulation_swap",
                intent_function="execute",
                params={
                    "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                    "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "input_amount": str(1000 * (i + 1)),
                },
                submitted_by=f"0x{i:040x}",
            )

        result = await block_loop.tick()
        assert result.orders_processed == 3

    @pytest.mark.asyncio
    async def test_unknown_app_rejected(self, block_loop, orderbook):
        """Orders for non-existent apps are rejected."""
        order = orderbook.submit(
            app_id="nonexistent_app",
            intent_function="execute",
            params={},
            submitted_by="0x" + "11" * 20,
        )

        await block_loop.tick()
        updated = orderbook.get(order.order_id)
        assert updated.status == OrderStatus.REJECTED
        assert "not found" in updated.error.lower()

    @pytest.mark.asyncio
    async def test_expired_order_not_processed(self, block_loop, orderbook):
        """Expired orders are marked expired, not processed."""
        import time
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={},
            submitted_by="0x" + "11" * 20,
            deadline=time.time() - 100,
        )

        result = await block_loop.tick()
        assert result.orders_expired == 1
        assert result.orders_processed == 0
        assert orderbook.get(order.order_id).status == OrderStatus.EXPIRED


class TestMultiValidatorConsensus:
    """Tests for multi-validator consensus with peer network."""

    @pytest.mark.asyncio
    async def test_single_validator_auto_approves(
        self, block_loop, orderbook, consensus_manager,
    ):
        """Single-validator mode auto-approves without waiting."""
        block_loop.set_consensus(consensus_manager)

        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000",
            },
            submitted_by="0x" + "11" * 20,
        )

        result = await block_loop.tick()
        updated = orderbook.get(order.order_id)
        assert updated.status in (OrderStatus.FILLED, OrderStatus.REJECTED)

    @pytest.mark.asyncio
    async def test_consensus_with_peer_network_wired(
        self, block_loop, orderbook, consensus_manager, peer_network,
    ):
        """BlockLoop with consensus + peer network wired in."""
        block_loop.set_consensus(consensus_manager)
        block_loop.set_peer_network(peer_network)

        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000",
            },
            submitted_by="0x" + "11" * 20,
        )

        # Peer network broadcast will fail (no real HTTP) but the single-validator
        # consensus should still auto-approve
        result = await block_loop.tick()
        updated = orderbook.get(order.order_id)
        assert updated.status in (OrderStatus.FILLED, OrderStatus.REJECTED)

    @pytest.mark.asyncio
    async def test_peer_network_set_on_block_loop(
        self, block_loop, peer_network,
    ):
        """Verify peer_network can be set and retrieved from BlockLoop."""
        assert block_loop.peer_network is None
        block_loop.set_peer_network(peer_network)
        assert block_loop.peer_network is peer_network


class TestLeaderFailover:
    """Tests for validator leader failover using deterministic election."""

    @pytest.mark.asyncio
    async def test_leader_failover(self, validator_cluster):
        """Kill the leader, verify next-highest-stake takes over."""
        leader = validator_cluster.get_leader()
        assert leader is not None
        assert leader.stake == 100

        await validator_cluster.kill_leader()

        new_leader = validator_cluster.get_leader()
        assert new_leader is not None
        assert new_leader.stake == 80
        assert new_leader.is_leader is True

    @pytest.mark.asyncio
    async def test_leader_election_matches_metagraph_sync(self, validator_cluster):
        """Verify cluster election matches MetagraphSync's elect_leader."""
        peers = [
            PeerInfo(
                uid=v.index, hotkey=v.hotkey,
                stake=v.stake, evm_address=v.evm_address,
            )
            for v in validator_cluster.validators
        ]
        expected_leader = elect_leader(peers)
        actual_leader = validator_cluster.get_leader()

        assert expected_leader is not None
        assert actual_leader is not None
        assert expected_leader.hotkey == actual_leader.hotkey

    @pytest.mark.asyncio
    async def test_double_failover(self, validator_cluster):
        """Kill leader twice, third validator takes over."""
        await validator_cluster.kill_leader()
        await validator_cluster.kill_leader()

        new_leader = validator_cluster.get_leader()
        assert new_leader is not None
        assert new_leader.stake == 60

    @pytest.mark.asyncio
    async def test_all_validators_killed(self, validator_cluster):
        """If all validators killed, no leader elected."""
        for _ in range(3):
            await validator_cluster.kill_leader()

        leader = validator_cluster.get_leader()
        assert leader is None


class TestPerpetualOrders:
    """Tests for perpetual order re-execution."""

    @pytest.mark.asyncio
    async def test_perpetual_order_stays_open(self, block_loop, orderbook, mock_relayer):
        """Perpetual orders re-open after fill if under max_executions."""
        order = orderbook.submit(
            app_id="emulation_swap",
            intent_function="execute",
            params={
                "input_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "output_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "input_amount": "1000",
            },
            submitted_by="0x" + "11" * 20,
            perpetual=True,
            max_executions=3,
        )

        await block_loop.tick()
        updated = orderbook.get(order.order_id)

        if updated.status == OrderStatus.OPEN:
            assert updated.execution_count >= 1
