"""Local subtensor management for emulation tests.

Starts a local Bittensor chain (via Docker) for testing validator/miner
registration, metagraph queries, and weight setting.

Uses bittensor 10.x API: bt.Subtensor, bt.Wallet, bt.Balance.
Image: ghcr.io/opentensor/subtensor-localnet:devnet-ready
  - Runs two internal validator nodes (One/Two) with fast blocks (~250ms)
  - Alice (5Grw...QY) is pre-funded with 1M TAO as sudo account
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

# Docker image for local subtensor — the dedicated localnet image
SUBTENSOR_IMAGE = "ghcr.io/opentensor/subtensor-localnet:devnet-ready"
CONTAINER_NAME = "test-subtensor"

# Alice's raw seed (//Alice dev account, pre-funded on localnet)
ALICE_RAW_SEED = "0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a"
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


class LocalSubtensor:
    """Manages a local subtensor for testing.

    Runs the subtensor-localnet Docker image which internally starts two
    validator nodes (One and Two) with fast block production (~250ms).
    Alice is the sudo account with 1M TAO for subnet/neuron registration.
    """

    def __init__(self, port: int = 9944) -> None:
        self.port = port
        self.url = f"ws://127.0.0.1:{port}"
        self.netuid: int = 1  # Will be set on subnet registration
        self._container_id: str | None = None
        self._alice_wallet = None  # Lazily initialized
        self._wallets: dict[tuple[str, str], Any] = {}
        self._temp_home = tempfile.mkdtemp(prefix="minotaur-bittensor-home-")
        self._original_home = os.environ.get("HOME")

    async def start(self) -> None:
        """Start local subtensor via Docker."""
        if not _docker_available():
            raise RuntimeError("Docker is required but not installed")

        # Bittensor writes under ~/.bittensor on import. Use an isolated,
        # writable HOME during emulation tests instead of the machine-global one.
        os.environ["HOME"] = self._temp_home
        os.makedirs(os.path.join(self._temp_home, ".bittensor", "miners"), exist_ok=True)
        os.makedirs(os.path.join(self._temp_home, ".bittensor", "wallets"), exist_ok=True)

        # Remove any stale container
        await self._run_cmd("docker", "rm", "-f", CONTAINER_NAME)

        # subtensor-localnet runs two internal nodes on ports 9944 and 9945
        cmd = [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "-p", f"{self.port}:9944",
            "-p", f"{self.port + 1}:9945",
            SUBTENSOR_IMAGE,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to start subtensor: {stderr.decode()}"
            )
        self._container_id = stdout.decode().strip()

        # Wait for chain to start accepting connections and producing blocks
        await self._wait_for_ready(timeout=90)
        logger.info("Local subtensor started: %s", self._container_id[:12])

    async def _wait_for_ready(self, timeout: int = 90) -> None:
        """Wait until the subtensor WebSocket is responding and blocks are being produced."""
        import time
        last_error: str | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                import bittensor as bt
                sub = bt.Subtensor(network=self.url)
                block = sub.block
                if block > 0:
                    logger.info("Local subtensor ready at block %d", block)
                    return
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(2)
        logs = ""
        try:
            logs = await self._run_cmd("docker", "logs", "--tail", "40", CONTAINER_NAME)
        except Exception:
            logs = ""
        raise RuntimeError(
            f"Local subtensor did not become ready in {timeout}s"
            + (f" (last_error={last_error})" if last_error else "")
            + (f"\nRecent container logs:\n{logs}" if logs else "")
        )

    async def stop(self) -> None:
        """Stop and remove the subtensor container."""
        if self._container_id:
            await self._run_cmd("docker", "rm", "-f", self._container_id)
            logger.info("Local subtensor stopped")
            self._container_id = None
        if self._original_home is not None:
            os.environ["HOME"] = self._original_home

    def _get_subtensor(self):
        """Create a bittensor 10.x Subtensor client."""
        import bittensor as bt
        return bt.Subtensor(network=self.url)

    def _get_alice_wallet(self):
        """Get or create Alice's pre-funded wallet."""
        if self._alice_wallet is None:
            import bittensor as bt
            self._alice_wallet = bt.Wallet(name="alice_local_test")
            self._alice_wallet.regenerate_coldkey(
                seed=ALICE_RAW_SEED,
                use_password=False,
                overwrite=True,
            )
            self._alice_wallet.regenerate_hotkey(
                seed=ALICE_RAW_SEED,
                use_password=False,
                overwrite=True,
            )
            assert self._alice_wallet.coldkeypub.ss58_address == ALICE_SS58, (
                f"Alice wallet mismatch: {self._alice_wallet.coldkeypub.ss58_address}"
            )
            self._wallets[("alice_local_test", "default")] = self._alice_wallet
        return self._alice_wallet

    async def register_subnet(self, netuid: int = 1) -> None:
        """Register a subnet on the local chain.

        Uses Alice's wallet (pre-funded on local chain with 1M TAO).
        """
        logger.info("Registering subnet on local chain")
        sub = self._get_subtensor()
        alice_wallet = self._get_alice_wallet()

        result = sub.register_subnet(wallet=alice_wallet)
        if not result.success:
            raise RuntimeError(f"Failed to register subnet: {result}")

        resolved_netuid = self._resolve_owned_subnet_netuid(
            sub,
            owner_ss58=alice_wallet.hotkey.ss58_address,
        )
        start_call = getattr(sub, "start_call", None)
        if callable(start_call):
            started = start_call(
                wallet=alice_wallet,
                netuid=resolved_netuid,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
            if isinstance(started, tuple):
                ok, message = started
                if not ok:
                    raise RuntimeError(f"Failed to start subnet emissions: {message}")
            elif hasattr(started, "success") and not started.success:
                raise RuntimeError(f"Failed to start subnet emissions: {started}")
        else:
            from bittensor.core.extrinsics.start_call import start_call_extrinsic

            ok, message = start_call_extrinsic(
                subtensor=sub,
                wallet=alice_wallet,
                netuid=resolved_netuid,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
            if not ok:
                raise RuntimeError(f"Failed to start subnet emissions: {message}")

        self.netuid = resolved_netuid
        logger.info("Subnet registered (requested=%d, resolved=%d)", netuid, resolved_netuid)

    async def register_neuron(
        self, wallet_name: str, hotkey: str,
    ) -> None:
        """Register a neuron via burned_register (faster than PoW).

        Creates wallet if needed, transfers TAO from Alice, registers on subnet.
        """
        import bittensor as bt

        logger.info("Registering neuron %s/%s", wallet_name, hotkey)
        sub = self._get_subtensor()

        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey)
        wallet.create_if_non_existent(
            coldkey_use_password=False,
            hotkey_use_password=False,
        )
        self._wallets[(wallet_name, hotkey)] = wallet

        # Transfer TAO from Alice (pre-funded) to the new wallet
        alice_wallet = self._get_alice_wallet()
        result = sub.transfer(
            wallet=alice_wallet,
            destination_ss58=wallet.coldkeypub.ss58_address,
            amount=bt.Balance.from_tao(10000),
        )
        if hasattr(result, 'success') and not result.success:
            raise RuntimeError(f"Failed to transfer TAO: {result}")

        # burned_register = pay TAO, no PoW needed
        result = None
        for attempt in range(5):
            result = sub.burned_register(
                wallet=wallet,
                netuid=self.netuid,
            )
            if result.success:
                break
            message = str(result)
            retryable = (
                "Custom error: 6" in message
                or "block limits" in message.lower()
                or "too many registrations" in message.lower()
            )
            if not retryable or attempt == 4:
                raise RuntimeError(f"Failed to register neuron: {result}")
            await asyncio.sleep(1)

        logger.info("Neuron %s/%s registered on netuid=%d", wallet_name, hotkey, self.netuid)

    async def register_validator(
        self, wallet_name: str, hotkey: str, stake_amount: float,
    ) -> None:
        """Register a validator with specified stake.

        Creates wallet, registers, and stakes TAO.
        """
        import bittensor as bt

        await self.register_neuron(wallet_name, hotkey)

        if stake_amount > 0:
            sub = self._get_subtensor()
            wallet = bt.Wallet(name=wallet_name, hotkey=hotkey)

            result = sub.add_stake(
                wallet=wallet,
                netuid=self.netuid,
                hotkey_ss58=wallet.hotkey.ss58_address,
                amount=bt.Balance.from_tao(stake_amount),
            )
            if hasattr(result, 'success') and not result.success:
                logger.warning("Failed to add stake: %s", result)

        logger.info(
            "Validator %s/%s registered with stake %.2f",
            wallet_name, hotkey, stake_amount,
        )

    async def register_miner(self, wallet_name: str, hotkey: str) -> None:
        """Register a miner (no stake)."""
        await self.register_neuron(wallet_name, hotkey)
        logger.info("Miner %s/%s registered", wallet_name, hotkey)

    def _resolve_owned_subnet_netuid(
        self,
        subtensor: Any,
        *,
        owner_ss58: str,
        timeout: int = 15,
    ) -> int:
        """Resolve the newest non-root subnet owned by the supplied hotkey."""
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            infos = []
            get_all = getattr(subtensor, "get_all_subnets_info", None)
            all_subnets = getattr(subtensor, "all_subnets", None)
            try:
                if callable(get_all):
                    infos = get_all() or []
                elif callable(all_subnets):
                    infos = all_subnets() or []
            except Exception:
                infos = []

            owned = []
            for info in infos:
                try:
                    netuid = int(getattr(info, "netuid", -1))
                except Exception:
                    continue
                owner = getattr(info, "owner_ss58", "") or getattr(info, "owner", "")
                if owner == owner_ss58 and netuid > 0:
                    owned.append(netuid)
            if owned:
                return max(owned)
            time.sleep(1)

        raise RuntimeError(f"Failed to resolve subnet netuid for owner {owner_ss58}")

    async def get_metagraph(self, netuid: int | None = None) -> Any:
        """Query the local metagraph.

        Returns a bittensor Metagraph object with neuron info.
        """
        netuid = netuid or self.netuid
        sub = self._get_subtensor()
        metagraph = sub.metagraph(netuid=netuid)
        return metagraph

    async def set_weights(
        self,
        wallet_name: str,
        hotkey: str,
        uids: list[int],
        weights: list[float],
    ) -> bool:
        """Set weights from a validator for the given UIDs.

        Returns True on success.
        """
        import bittensor as bt
        import numpy as np

        sub = self._get_subtensor()
        wallet = self._wallets.get((wallet_name, hotkey))
        if wallet is None:
            wallet = bt.Wallet(name=wallet_name, hotkey=hotkey)

        uid = sub.get_uid_for_hotkey_on_subnet(wallet.hotkey.ss58_address, self.netuid)
        if uid is None:
            raise RuntimeError(
                f"Hotkey {wallet.hotkey.ss58_address} not registered on netuid={self.netuid}"
            )

        # Fresh local subnets can enforce a long weight-update cooldown before
        # the first commit/reveal cycle is accepted. Wait it out explicitly so
        # tests don't depend on subtle SDK no-op behavior.
        for _ in range(180):
            blocks_since_last_update = sub.blocks_since_last_update(self.netuid, uid)
            weights_rate_limit = sub.weights_rate_limit(self.netuid)
            if blocks_since_last_update > weights_rate_limit:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError(
                f"Timed out waiting for weight rate limit on netuid={self.netuid}"
            )

        result = sub.set_weights(
            wallet=wallet,
            netuid=self.netuid,
            uids=np.array(uids, dtype=np.int64),
            weights=np.array(weights, dtype=np.float32),
            max_attempts=10,
            block_time=0.25,  # Fast localnet uses ~250ms blocks
        )
        return result.success if hasattr(result, 'success') else bool(result)

    async def _run_cmd(self, *cmd: str) -> tuple[str, str]:
        """Run a command asynchronously."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode()
