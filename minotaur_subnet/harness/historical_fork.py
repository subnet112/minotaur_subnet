"""Per-block Anvil forks for Stage 3 regression testing.

Spawns a fresh Anvil Docker container pinned to a specific historical
block number. Used by the regression stage to replay failed orders
against the exact market state they originally executed in.

Each fork is short-lived — one per scenario. This is expensive (Docker
startup + RPC fetch + RAM) so Stage 3 only runs on adoption-candidate
challengers that have a small number of regression candidates (typically
0-5 scenarios per round).

Usage:
    async with historical_anvil(chain_id=8453, block_number=28000123) as rpc_url:
        # rpc_url is the ephemeral URL of the forked Anvil
        # Simulate plans against this block's state
        result = await simulator.simulate(plan, rpc_url=rpc_url)
    # Container is automatically cleaned up on exit
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import subprocess
from typing import AsyncIterator

logger = logging.getLogger(__name__)


# Archive RPC env vars per chain. Must support eth_getBlockByNumber at
# historical blocks and eth_getProof for state reads. Nodies free tier
# may NOT support this — check with provider.
_ARCHIVE_RPC_ENV = {
    1: "ARCHIVE_RPC_ETH",
    8453: "ARCHIVE_RPC_BASE",
    964: "ARCHIVE_RPC_BTEVM",
    31337: "ARCHIVE_RPC_ETH",  # Local anvil reuses ETH archive
}

# Default anvil image (same as production anvil containers)
_ANVIL_IMAGE = "ghcr.io/foundry-rs/foundry:latest"

# How long to wait for Anvil to become ready (seconds)
_ANVIL_READY_TIMEOUT = 30.0


class HistoricalForkError(Exception):
    """Archive RPC not configured for the requested chain."""


@contextlib.asynccontextmanager
async def historical_anvil(
    chain_id: int,
    block_number: int,
) -> AsyncIterator[str]:
    """Spawn an ephemeral Anvil Docker container forked at a specific block.

    Yields the RPC URL (e.g. "http://172.17.0.X:8545") that solvers
    and simulators can use. Cleans up the container on exit.

    Raises:
        HistoricalForkError: If no archive RPC is configured for the chain.
    """
    env_var = _ARCHIVE_RPC_ENV.get(chain_id)
    if env_var is None:
        raise HistoricalForkError(f"No archive RPC env var defined for chain {chain_id}")

    archive_rpc = os.environ.get(env_var, "").strip()
    if not archive_rpc:
        raise HistoricalForkError(
            f"{env_var} not set — Stage 3 regression replay requires archive RPC for chain {chain_id}"
        )

    # Unique container name to avoid conflicts
    container_name = f"anvil-hist-{chain_id}-{block_number}-{secrets.token_hex(4)}"
    port = 8545  # Internal port; we'll connect via container IP

    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", container_name,
        "--entrypoint", "anvil",
        _ANVIL_IMAGE,
        "--host", "0.0.0.0",
        "--port", str(port),
        "--fork-url", archive_rpc,
        "--fork-block-number", str(block_number),
        "--chain-id", str(chain_id),
        "--accounts", "1",
        "--balance", "10000",
        "--no-storage-caching",
        "--silent",
    ]

    logger.info(
        "Spawning historical Anvil: chain=%d block=%d container=%s",
        chain_id, block_number, container_name,
    )

    # Start container
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HistoricalForkError(
                f"Failed to start Anvil: {stderr.decode()[:300]}"
            )
    except FileNotFoundError:
        raise HistoricalForkError("docker command not available on host")

    container_id = stdout.decode().strip()[:12]

    try:
        # Get container IP
        inspect = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container_name,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ip_out, _ = await inspect.communicate()
        container_ip = ip_out.decode().strip()
        if not container_ip:
            raise HistoricalForkError("Could not determine container IP")

        rpc_url = f"http://{container_ip}:{port}"

        # Wait for Anvil to be ready
        await _wait_for_anvil_ready(rpc_url, _ANVIL_READY_TIMEOUT)

        logger.info(
            "Historical Anvil ready: %s (container=%s)",
            rpc_url, container_id,
        )
        yield rpc_url

    finally:
        # Clean up container
        try:
            kill = await asyncio.create_subprocess_exec(
                "docker", "kill", container_name,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await kill.communicate()
            logger.debug("Stopped historical Anvil %s", container_name)
        except Exception as exc:
            logger.warning("Failed to clean up historical Anvil %s: %s", container_name, exc)


async def _wait_for_anvil_ready(rpc_url: str, timeout: float) -> None:
    """Poll Anvil until eth_blockNumber returns successfully."""
    import urllib.request
    import json

    deadline = asyncio.get_event_loop().time() + timeout
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }).encode("utf-8")

    while asyncio.get_event_loop().time() < deadline:
        try:
            def _poll() -> bool:
                req = urllib.request.Request(
                    rpc_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                    return "result" in data

            ready = await asyncio.get_event_loop().run_in_executor(None, _poll)
            if ready:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)

    raise HistoricalForkError(f"Anvil did not become ready within {timeout}s")


def archive_rpc_available(chain_id: int) -> bool:
    """Check if archive RPC is configured for a chain."""
    env_var = _ARCHIVE_RPC_ENV.get(chain_id)
    if env_var is None:
        return False
    return bool(os.environ.get(env_var, "").strip())
