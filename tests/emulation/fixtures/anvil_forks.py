"""Anvil fork management for emulation tests.

Manages Anvil instances forking real chains for deterministic testing.
Each fork runs as a Docker container or local process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AnvilFork:
    """Manages an Anvil instance forking a real chain."""
    chain_id: int
    fork_url: str
    port: int
    block_time: int = 2
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """Start Anvil with fork."""
        cmd = [
            "anvil",
            "--fork-url", self.fork_url,
            "--port", str(self.port),
            "--block-time", str(self.block_time),
            "--chain-id", str(self.chain_id),
            "--accounts", "10",
            "--balance", "10000",
        ]
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Wait for Anvil to be ready
        await asyncio.sleep(2)
        logger.info(
            "Anvil fork started: chain=%d port=%d pid=%d",
            self.chain_id, self.port, self._process.pid,
        )

    async def stop(self) -> None:
        """Stop the Anvil process."""
        if self._process:
            self._process.terminate()
            await self._process.wait()
            logger.info("Anvil fork stopped: chain=%d", self.chain_id)

    async def deploy_app_intent_base(
        self,
        relayer_address: str,
        validators: list[str],
        quorum_bps: int,
    ) -> str:
        """Deploy AppIntentBase.sol via forge script on this fork."""
        cmd = [
            "forge", "script",
            "script/Deploy.s.sol",
            "--rpc-url", self.rpc_url,
            "--broadcast",
            "--private-key", "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # Anvil default key 0
        ]
        env = {
            **os.environ,
            "RELAYER_ADDRESS": relayer_address,
            "VALIDATORS": ",".join(validators),
            "QUORUM_BPS": str(quorum_bps),
        }
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=os.path.join(os.path.dirname(__file__), "../../../contracts"),
        )
        stdout, stderr = await proc.communicate()
        # Parse deployed address from forge output
        for line in stdout.decode().split("\n"):
            if "deployed at:" in line.lower():
                parts = line.split()
                for part in parts:
                    if part.startswith("0x") and len(part) == 42:
                        return part
        logger.warning("Could not parse deployment address from forge output")
        return ""

    async def fund_account(self, address: str, amount_eth: float) -> None:
        """Fund an address with ETH using Anvil cheat code."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        # anvil_setBalance
        amount_wei = int(amount_eth * 1e18)
        w3.provider.make_request(
            "anvil_setBalance",
            [address, hex(amount_wei)],
        )

    async def impersonate(self, address: str) -> None:
        """Impersonate an account for testing."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        w3.provider.make_request("anvil_impersonateAccount", [address])

    async def mine_block(self) -> None:
        """Force mine a block."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        w3.provider.make_request("evm_mine", [])

    async def snapshot(self) -> int:
        """Take chain snapshot for test isolation."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        result = w3.provider.make_request("evm_snapshot", [])
        return int(result["result"], 16)

    async def revert(self, snapshot_id: int) -> None:
        """Revert to a previous snapshot."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        w3.provider.make_request("evm_revert", [hex(snapshot_id)])
