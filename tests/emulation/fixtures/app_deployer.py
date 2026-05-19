"""App deployer fixture for emulation tests.

Deploys sample App Intent contracts for tests. The canonical swap app is
`DexAggregatorApp`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AppDeployer:
    """Deploys sample apps for testing."""

    def __init__(self, anvil_rpc: str) -> None:
        self.rpc_url = anvil_rpc
        self.deployed: dict[str, str] = {}  # app_name -> address

    async def deploy_swap_app(
        self,
        app_intent_base_address: str,
        relayer_address: str,
        validators: list[str],
    ) -> str:
        """Deploy the canonical swap app on the Anvil fork.

        Returns the deployed contract address. Quorum is no longer a deploy
        arg — AppIntentBase reads it from ValidatorRegistry at execution time.
        """
        # In real implementation: forge script with constructor args
        # For testing, return a mock address
        address = f"0x{'aa' * 20}"
        self.deployed["DexAggregatorApp"] = address
        logger.info("Deployed DexAggregatorApp at %s", address)
        return address
