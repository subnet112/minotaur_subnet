"""Miner cluster management for emulation tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MinerProcess:
    """A single miner in the test cluster."""
    index: int
    wallet_name: str
    hotkey: str
    solver_repo: str = ""
    solver_name: str = ""


class MinerCluster:
    """Manages M miner processes for testing."""

    def __init__(self) -> None:
        self.miners: list[MinerProcess] = []

    async def start(self, count: int = 1) -> list[MinerProcess]:
        for i in range(count):
            mp = MinerProcess(
                index=i,
                wallet_name=f"test_miner_{i}",
                hotkey=f"miner_hotkey_{i}",
                solver_name="baseline-swap-solver",
            )
            self.miners.append(mp)
            logger.info("Created Miner-%d", i)
        return self.miners

    async def stop(self) -> None:
        self.miners.clear()
