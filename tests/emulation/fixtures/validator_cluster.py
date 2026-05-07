"""Validator cluster management for emulation tests.

Spins up N validator processes with registered hotkeys, OrderBook,
BlockLoop, and ConsensusManager connections.

Uses Anvil deterministic keys for real EIP-712 signing.
Integrates with MetagraphSync's elect_leader for deterministic
leader election and ValidatorPeerNetwork for proposal broadcast.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    sign_plan_approval_eip712,
)
from minotaur_subnet.validator.metagraph_sync import PeerInfo, elect_leader

logger = logging.getLogger(__name__)

# Anvil deterministic private keys (accounts 5-9 reserved for validators)
VALIDATOR_KEYS = [
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",  # 6
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",  # 7
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",  # 8
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",  # 9
]


@dataclass
class ValidatorProcess:
    """A single validator in the test cluster."""
    index: int
    wallet_name: str
    hotkey: str
    stake: float
    evm_address: str
    private_key: str
    is_leader: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False)

    def __str__(self) -> str:
        role = "leader" if self.is_leader else "follower"
        return f"Validator-{self.index} ({role}, stake={self.stake})"


class ValidatorCluster:
    """Manages N validator processes for testing.

    Uses real Anvil deterministic keys for EIP-712 signing.
    """

    def __init__(self) -> None:
        self.validators: list[ValidatorProcess] = []
        self._started = False

    async def start(
        self,
        count: int = 3,
        stakes: list[float] | None = None,
    ) -> list[ValidatorProcess]:
        """Register validators and start processes.

        Args:
            count: Number of validators (max 5).
            stakes: Stake amounts (highest = leader). Defaults to [100, 80, 60].

        Returns:
            List of ValidatorProcess instances with real EVM keys.
        """
        stakes = stakes or [100, 80, 60][:count]
        count = min(count, len(VALIDATOR_KEYS))

        for i in range(count):
            private_key = VALIDATOR_KEYS[i]
            evm_address = address_from_key(private_key)

            vp = ValidatorProcess(
                index=i,
                wallet_name=f"test_validator_{i}",
                hotkey=f"hotkey_{i}",
                stake=stakes[i] if i < len(stakes) else 50,
                evm_address=evm_address,
                private_key=private_key,
            )
            self.validators.append(vp)
            logger.info("Created %s (addr=%s)", vp, evm_address)

        # Use the same leader election as MetagraphSync
        self._elect()
        self._started = True
        return self.validators

    def _elect(self) -> None:
        """Run deterministic leader election matching MetagraphSync."""
        for v in self.validators:
            v.is_leader = False
        peers = [
            PeerInfo(uid=v.index, hotkey=v.hotkey, stake=v.stake, evm_address=v.evm_address)
            for v in self.validators
        ]
        leader_peer = elect_leader(peers)
        if leader_peer is not None:
            for v in self.validators:
                if v.hotkey == leader_peer.hotkey:
                    v.is_leader = True
                    break

    def get_leader(self) -> ValidatorProcess | None:
        """Return the current leader via deterministic election."""
        for v in self.validators:
            if v.is_leader:
                return v
        return None

    async def kill_leader(self) -> None:
        """Kill the leader process to test failover."""
        leader = self.get_leader()
        if leader is None:
            return

        logger.info("Killing leader: %s", leader)
        leader.stake = 0  # Remove from leader consideration
        leader.is_leader = False

        # Re-elect using the same deterministic function as MetagraphSync
        self._elect()
        new_leader = self.get_leader()
        if new_leader:
            logger.info("New leader: %s", new_leader)

    async def stop(self) -> None:
        """Stop all validator processes."""
        for vp in self.validators:
            if vp._task:
                vp._task.cancel()
        self.validators.clear()
        self._started = False

    def get_evm_addresses(self) -> list[str]:
        """Return all validator EVM addresses."""
        return [v.evm_address for v in self.validators]

    def get_sorted_validators(self) -> list[ValidatorProcess]:
        """Return validators sorted by address (ascending) for contract compatibility."""
        return sorted(self.validators, key=lambda v: int(v.evm_address, 16))

    def get_signatures(
        self,
        plan_hash: str | bytes,
        order_id: bytes | None = None,
        score_bps: int = 8000,
        domain_separator: bytes | None = None,
    ) -> list[tuple[str, bytes]]:
        """Collect real EIP-712 signatures from all validators for a plan.

        If domain_separator is provided, produces real ECDSA signatures.
        Otherwise, produces deterministic test signatures.

        Args:
            plan_hash: Plan hash (hex string or bytes).
            order_id: bytes32 order ID for the EIP-712 struct.
            score_bps: Score in basis points.
            domain_separator: EIP-712 domain separator.

        Returns:
            List of (address, signature_bytes) tuples, sorted by address.
        """
        if isinstance(plan_hash, str):
            plan_hash = bytes.fromhex(plan_hash.replace("0x", ""))

        sigs = []
        for v in self.get_sorted_validators():
            if v.stake <= 0:
                continue

            if domain_separator is not None and order_id is not None:
                # Real EIP-712 signature
                sig = sign_plan_approval_eip712(
                    private_key=v.private_key,
                    order_id=order_id,
                    plan_hash=plan_hash,
                    score_bps=score_bps,
                    domain_separator=domain_separator,
                )
            else:
                # Deterministic test signature (64 bytes + v byte)
                sig = bytes.fromhex(f"{v.index:064x}") + b"\x1b"

            sigs.append((v.evm_address, sig))

        return sigs
