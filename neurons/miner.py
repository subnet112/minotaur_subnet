"""Miner implementation for managing a solver and registering it with the aggregator.

Supports both simulation mode (for testing) and bittensor mode (production).
In simulation mode, generates a hotkey from miner_id. In bittensor mode, uses
the configured Bittensor wallet.
"""

import argparse
import atexit
import json
import os
import platform
import random
import requests
import signal
import socket
import sys
import threading
import time
import uuid
from hashlib import blake2b
from typing import Dict, List, Optional, Any

import base58
from nacl.signing import SigningKey

import bittensor as bt

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip .env loading

from .solver import Solver as SolverV3
from .solver_uv2 import SolverV2
from .solver_base import SolverBase

# Solver type registry
SOLVER_TYPES = {
    "v3": SolverV3,
    "uniswap-v3": SolverV3,
    "v2": SolverV2,
    "uniswap-v2": SolverV2,
    "base": SolverBase,
    "base-v3": SolverBase,
    "uniswap-v3-base": SolverBase,
}

SS58_PREFIX = b"SS58PRE"
DEFAULT_ADDRESS_TYPE = 42  # Substrate generic address format
REGISTRATION_PREFIX = "oif-register-solver"
UPDATE_PREFIX = "oif-update-solver"
DELETE_PREFIX = "oif-delete-solver"


def ss58_encode(public_key: bytes, address_type: int = DEFAULT_ADDRESS_TYPE) -> str:
    """Encode a public key as SS58 address."""
    if len(public_key) != 32:
        raise ValueError("public key must be 32 bytes")
    if not (0 <= address_type < 64):
        raise ValueError("address_type must be between 0 and 63")

    data = bytes([address_type]) + public_key
    checksum = blake2b(SS58_PREFIX + data, digest_size=64).digest()
    return base58.b58encode(data + checksum[:2]).decode()


def derive_signing_key(miner_id: str) -> SigningKey:
    """Derive a signing key from miner_id."""
    seed = blake2b(miner_id.encode("utf-8"), digest_size=32).digest()
    return SigningKey(seed)


def generate_hotkey(miner_id: str):
    """Generate hotkey and signing key from miner_id."""
    signing_key = derive_signing_key(miner_id)
    public_key = signing_key.verify_key.encode()
    ss58_address = ss58_encode(public_key)
    return ss58_address, signing_key


class Miner:
    """Represents a subnet miner running one or more solvers."""

    def __init__(
        self,
        miner_id: str,
        aggregator_url: str,
        aggregator_api_key: Optional[str] = None,
        miner_api_key: Optional[str] = None,
        base_port: int = 8000,
        solver_host: Optional[str] = None,
        mode: str = "simulation",
        wallet: Optional[bt.Wallet] = None,
        num_solvers: int = 1,
        solver_type: str = "v3",
        logger=None,
    ):
        """Initialize miner.
        
        Args:
            miner_id: Miner identifier (used as-is in simulation mode, or as wallet name in bittensor mode)
            aggregator_url: Aggregator API base URL
            aggregator_api_key: Optional general aggregator API key
            miner_api_key: API key for miner-specific endpoints (required for /v1/solvers/* endpoints). Falls back to aggregator_api_key if not set.
            base_port: Base port for solver servers
            solver_host: Host address for solver endpoint (default: localhost, use host.docker.internal or host IP if aggregator is in Docker)
            mode: "simulation" or "bittensor"
            wallet: Bittensor wallet (required in bittensor mode)
            num_solvers: Number of solvers to run (default: 1)
            solver_type: Solver type to use ("v2", "v3", "uniswap-v2", "uniswap-v3")
            logger: Logger instance
        """
        self.mode = mode
        self.logger = logger or bt.logging
        self.aggregator_url = aggregator_url
        self.solver_type = solver_type
        self.solver_class = SOLVER_TYPES.get(solver_type, SolverV3)
        # Use MINER_API_KEY for miner-specific endpoints (required)
        if not miner_api_key or not miner_api_key.strip():
            error_msg = (
                "MINER_API_KEY is required for miner endpoints. "
                "Set it via --miner.api_key or MINER_API_KEY environment variable. "
                f"Current value: {repr(miner_api_key)}"
            )
            if self.logger:
                self.logger.error(error_msg)
            raise ValueError(error_msg)
        self.miner_api_key = miner_api_key.strip()
        self.base_port = base_port
        self.solver_host = solver_host or "localhost"
        self.num_solvers = max(1, num_solvers)  # Ensure at least 1 solver
        self.solvers = []
        self.solver_threads = []
        self.registered_solvers = []  # Track registered solvers for cleanup

        if mode == "bittensor":
            if wallet is None:
                raise ValueError("Wallet is required in bittensor mode")
            self.wallet = wallet
            self.miner_id = wallet.hotkey.ss58_address
            # Derive signing key from hotkey
            # Note: In production, you'd use the actual hotkey private key
            # For now, we derive from the hotkey address
            self.signing_key = derive_signing_key(self.miner_id)
        else:  # simulation mode
            self.wallet = None
            hotkey, signing_key = generate_hotkey(miner_id)
            self.miner_id = hotkey
            self.signing_key = signing_key
            if self.logger:
                self.logger.info(f"Simulation mode: Generated hotkey {hotkey} from miner_id {miner_id}")

    def create_solver(self, solver_index: int, latency_ms: int = None, quality: float = None):
        """Create a solver configuration."""
        if latency_ms is None:
            latency_ms = random.randint(80, 300)
        if quality is None:
            quality = random.uniform(0.8, 1.2)

        solver_id = f"{self.miner_id}-solver-{solver_index:02d}"
        port = self.base_port + solver_index

        solver_config = {
            "solver_id": solver_id,
            "port": port,
            "latency_ms": latency_ms,
            "quality": quality,
            # Endpoint advertised to the aggregator (may need to be reachable from Docker/remote).
            "endpoint": f"http://{self.solver_host}:{port}",
            # Local endpoint used by the miner for readiness checks and token polling.
            # The solver runs in-process, so localhost is the most reliable target even when
            # solver_host is an external/bridge IP.
            "local_endpoint": f"http://127.0.0.1:{port}",
        }

        self.solvers.append(solver_config)
        return solver_config

    def get_solver_info(self, solver_id: str) -> Optional[Dict[str, Any]]:
        """Get solver information from aggregator."""
        try:
            headers = {}
            if self.miner_api_key:
                headers["X-API-Key"] = self.miner_api_key
            
            response = requests.get(
                f"{self.aggregator_url}/v1/solvers/{solver_id}",
                headers=headers,
                timeout=3
            )
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to get solver info: {e}")
            return None

    def check_solver_exists(self, solver_id: str) -> bool:
        """Check if solver is already registered."""
        return self.get_solver_info(solver_id) is not None

    def _registration_message(self, solver_config: dict) -> str:
        return f"{REGISTRATION_PREFIX}:{self.miner_id}:{solver_config['solver_id']}:{solver_config['endpoint']}"

    def _sign_registration(self, solver_config: dict) -> str:
        message = self._registration_message(solver_config).encode("utf-8")
        signature = self.signing_key.sign(message).signature
        return "0x" + signature.hex()

    def _update_message(self, solver_id: str, new_endpoint: str) -> str:
        """Generate update message for endpoint update."""
        return f"{UPDATE_PREFIX}:{self.miner_id}:{solver_id}:{new_endpoint}"

    def _sign_update(self, solver_id: str, new_endpoint: str) -> str:
        """Sign an endpoint update message."""
        message = self._update_message(solver_id, new_endpoint).encode("utf-8")
        signature = self.signing_key.sign(message).signature
        return "0x" + signature.hex()

    def _delete_message(self, solver_id: str) -> str:
        """Generate delete message for solver deregistration."""
        return f"{DELETE_PREFIX}:{self.miner_id}:{solver_id}"

    def _sign_delete(self, solver_id: str) -> str:
        """Sign a solver deletion message."""
        message = self._delete_message(solver_id).encode("utf-8")
        signature = self.signing_key.sign(message).signature
        return "0x" + signature.hex()

    def deregister_solver(self, solver_id: str) -> bool:
        """Deregister a solver from the aggregator."""
        # First, check if solver exists and verify miner_id matches
        solver_info = self.get_solver_info(solver_id)
        registered_miner_id = None
        if solver_info:
            registered_miner_id = solver_info.get('minerId')
            if registered_miner_id and registered_miner_id != self.miner_id:
                if self.logger:
                    self.logger.error(
                        f"❌ Cannot deregister solver {solver_id}: "
                        f"Registered miner_id ({registered_miner_id}) doesn't match "
                        f"current miner_id ({self.miner_id})"
                    )
                return False
        
        delete_message = self._delete_message(solver_id)
        signature = self._sign_delete(solver_id)
        
        # Verify signature locally
        try:
            from nacl.signing import VerifyKey
            message_bytes = delete_message.encode("utf-8")
            verify_key = self.signing_key.verify_key
            signature_bytes = bytes.fromhex(signature[2:])  # Remove 0x prefix
            verify_key.verify(message_bytes, signature_bytes)
            sig_verified = True
        except Exception as e:
            sig_verified = False
            sig_error = str(e)
        
        delete_data = {
            "solverId": solver_id,
            "minerId": self.miner_id,
            "signature": signature,
            "signatureType": "ed25519"
        }

        try:
            headers = {"Content-Type": "application/json"}
            if self.miner_api_key:
                headers["X-API-Key"] = self.miner_api_key
            else:
                if self.logger:
                    self.logger.warning(f"⚠️  No MINER_API_KEY set - deregistration may fail")
            
            if self.logger:
                verify_key_ss58 = ss58_encode(self.signing_key.verify_key.encode())
                self.logger.debug(
                    f"🔍 Deregistering solver:\n"
                    f"   solver_id: {solver_id}\n"
                    f"   miner_id: {self.miner_id}\n"
                    f"   registered_miner_id: {registered_miner_id if solver_info else 'N/A'}\n"
                    f"   verify_key (SS58): {verify_key_ss58}\n"
                    f"   delete_message: {delete_message}\n"
                    f"   signature: {signature}\n"
                    f"   local_sig_verification: {'✅ PASS' if sig_verified else f'❌ FAIL: {sig_error}'}\n"
                    f"   api_key_set: {bool(self.miner_api_key)}"
                )
            
            response = requests.delete(
                f"{self.aggregator_url}/v1/solvers/{solver_id}",
                json=delete_data,
                headers=headers,
                timeout=5
            )

            if response.status_code == 200:
                if self.logger:
                    self.logger.success(f"Deregistered solver: {solver_id}")
                return True
            elif response.status_code == 404:
                if self.logger:
                    self.logger.info(f"Solver not found (may already be deregistered): {solver_id}")
                return True  # Already gone, consider success
            else:
                try:
                    error = response.json()
                    msg = error.get('message', 'Unknown error')
                    error_type = error.get('error', '')
                except:
                    msg = response.text[:200]
                    error_type = ''

                if self.logger:
                    self.logger.error(
                        f"❌ Failed to deregister solver: Status {response.status_code}\n"
                        f"   Error type: {error_type}\n"
                        f"   Error message: {msg}\n"
                        f"   Full response: {response.text[:500]}\n"
                        f"   Request payload: {json.dumps(delete_data, indent=2)}"
                    )
                return False

        except requests.exceptions.RequestException as e:
            if self.logger:
                self.logger.warning(f"Connection error deregistering solver: {e}")
            return False

    def cleanup(self):
        """Deregister all registered solvers."""
        if not self.registered_solvers:
            return
        
        if self.logger:
            self.logger.info(f"Deregistering {len(self.registered_solvers)} solver(s)...")
        
        for solver_config in self.registered_solvers:
            solver_id = solver_config["solver_id"]
            self.deregister_solver(solver_id)
        
        self.registered_solvers.clear()

    def _poll_solver_tokens(self, solver_config: dict, max_attempts: int = 30, delay: float = 1.0) -> list:
        """Wait for the solver to expose supported tokens."""
        endpoint = solver_config.get("local_endpoint") or solver_config["endpoint"]
        tokens_url = f"{endpoint}/tokens"
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(tokens_url, timeout=5)
                response.raise_for_status()
                data = response.json()
                networks = data.get("networks", {})

                for chain_key, network in networks.items():
                    tokens = network.get("tokens", [])
                    if tokens:
                        chain_id = network.get("chain_id")
                        if chain_id is None:
                            try:
                                chain_id = int(chain_key)
                            except (TypeError, ValueError):
                                chain_id = 1
                        for token in tokens:
                            token.setdefault("chain_id", chain_id)
                        return tokens
            except Exception as exc:
                last_error = exc

            time.sleep(delay)

        if last_error:
            if self.logger:
                self.logger.warning(f"Failed to fetch tokens from {tokens_url}: {last_error}")
        else:
            if self.logger:
                self.logger.warning(f"Solver at {tokens_url} did not report any tokens in time")
        return []

    def _build_supported_assets_payload(self, tokens: list) -> dict:
        """Convert solver token list into registration payload."""
        if not tokens:
            return {"type": "assets", "assets": []}

        max_assets = int(os.environ.get("MOCK_MINER_REGISTRATION_LIMIT", "4096"))
        assets_payload = []
        for token in tokens[:max_assets]:
            address = token.get("address")
            symbol = token.get("symbol") or (address[-4:].upper() if address else "TOK")
            assets_payload.append(
                {
                    "chain_id": token.get("chain_id", 1),
                    "address": address,
                    "symbol": symbol,
                    "name": token.get("name", symbol),
                    "decimals": token.get("decimals", 18),
                }
            )

        return {
            "type": "assets",
            "assets": assets_payload,
        }

    def update_solver_endpoint(self, solver_config: dict) -> bool:
        """Update solver endpoint if it has changed."""
        solver_id = solver_config["solver_id"]
        new_endpoint = solver_config["endpoint"]

        # Get current solver info
        solver_info = self.get_solver_info(solver_id)
        if solver_info is None:
            return False  # Solver doesn't exist, need to register instead

        current_endpoint = solver_info.get("endpoint")
        if current_endpoint == new_endpoint:
            if self.logger:
                self.logger.info(f"Solver endpoint unchanged: {solver_id} -> {new_endpoint}")
            return True

        # Endpoint has changed, update it
        update_data = {
            "solverId": solver_id,
            "minerId": self.miner_id,
            "signature": self._sign_update(solver_id, new_endpoint),
            "endpoint": new_endpoint
        }

        try:
            headers = {"Content-Type": "application/json"}
            if self.miner_api_key:
                headers["X-API-Key"] = self.miner_api_key
            
            response = requests.put(
                f"{self.aggregator_url}/v1/solvers/{solver_id}",
                json=update_data,
                headers=headers,
                timeout=5
            )

            if response.status_code == 200:
                if self.logger:
                    self.logger.success(f"Updated solver endpoint: {solver_id} -> {new_endpoint}")
                return True
            else:
                try:
                    error = response.json()
                    msg = error.get('message', 'Unknown error')
                except:
                    msg = response.text[:200]

                if self.logger:
                    self.logger.error(f"Failed to update solver endpoint: Status {response.status_code}, Error: {msg}")
                return False

        except requests.exceptions.RequestException as e:
            if self.logger:
                self.logger.error(f"Connection error updating solver: {e}")
            return False

    def register_solver(self, solver_config: dict) -> bool:
        """
        Register a solver with the aggregator.
        
        Behavior:
        - If solver doesn't exist: register it (POST /v1/solvers/register)
        - If solver exists and is active with same endpoint: skip (already registered)
        - If solver exists and is active but endpoint changed: update endpoint (PUT /v1/solvers/{id})
        - If solver exists but is NOT active (inactive/error/unknown): re-register to activate (POST /v1/solvers/register)
        
        Note: When a miner stops, it deregisters the solver (sets status to inactive).
        When relaunching, this method detects the inactive status and re-registers to activate it.
        """
        solver_id = solver_config["solver_id"]

        # Check if solver exists
        solver_info = self.get_solver_info(solver_id)
        if solver_info is not None:
            # Solver exists, check status
            status = solver_info.get("status", "unknown")
            
            # If solver is active, check if endpoint needs updating
            if status == "active":
                current_endpoint = solver_info.get("endpoint")
                new_endpoint = solver_config["endpoint"]
                
                if current_endpoint == new_endpoint:
                    if self.logger:
                        self.logger.info(f"Solver already registered and active with same endpoint: {solver_id}")
                    return True
                else:
                    # Endpoint has changed, update it
                    if self.logger:
                        self.logger.info(f"Solver exists but endpoint changed: {current_endpoint} -> {new_endpoint}")
                    return self.update_solver_endpoint(solver_config)
            else:
                # Solver exists but is not active (inactive, error, or unknown status)
                # Re-register to reactivate it
                if self.logger:
                    self.logger.info(f"Solver exists but status is '{status}', re-registering to activate: {solver_id}")
                # Continue to registration flow below (don't return early)

        tokens = self._poll_solver_tokens(solver_config)
        if not tokens:
            if self.logger:
                self.logger.error(f"Aborting registration for {solver_id}: solver tokens unavailable")
            return False

        supported_assets = self._build_supported_assets_payload(tokens)

        registration_data = {
            "solverId": solver_id,
            "adapterId": "oif-v1",
            "endpoint": solver_config["endpoint"],
            "minerId": self.miner_id,
            "signature": self._sign_registration(solver_config),
            "signatureType": "ed25519",
            "name": f"{self.miner_id}'s Solver {solver_config['solver_id'].split('-')[-1]}",
            "description": f"Solver with {solver_config['latency_ms']}ms latency",
            "supportedAssets": supported_assets
        }

        try:
            headers = {"Content-Type": "application/json"}
            if self.miner_api_key:
                headers["X-API-Key"] = self.miner_api_key
            else:
                if self.logger:
                    self.logger.error("MINER_API_KEY is not set - cannot authenticate with aggregator")
                return False
            
            if self.logger:
                self.logger.debug(f"Registering solver with API key: {self.miner_api_key[:10]}... (truncated)")
            
            response = requests.post(
                f"{self.aggregator_url}/v1/solvers/register",
                json=registration_data,
                headers=headers,
                timeout=5
            )

            if response.status_code == 201:
                if self.logger:
                    self.logger.success(f"Registered solver: {solver_id}")
                return True
            elif response.status_code == 400 and "already exists" in response.text:
                if self.logger:
                    self.logger.info(f"Solver already registered: {solver_id}")
                return True
            else:
                try:
                    error = response.json()
                    msg = error.get('message', 'Unknown error')
                except:
                    msg = response.text[:200]

                if self.logger:
                    if response.status_code == 401:
                        self.logger.error(
                            f"Registration failed for {solver_id}: Status {response.status_code} (Unauthorized)\n"
                            f"  Error: {msg}\n"
                            f"  Check that MINER_API_KEY is set correctly and has the 'Miner' role.\n"
                            f"  Current API key: {self.miner_api_key[:10] if self.miner_api_key else 'NOT SET'}... (truncated)"
                        )
                    else:
                        self.logger.error(f"Registration failed for {solver_id}: Status {response.status_code}, Error: {msg}")
                return False

        except requests.exceptions.RequestException as e:
            if self.logger:
                self.logger.error(f"Connection error: {e}")
            return False

    def start_solver_thread(self, solver_config: dict):
        """Start a solver server in a separate thread."""
        solver_class = self.solver_class
        def run_solver():
            try:
                solver = solver_class(
                    solver_id=solver_config["solver_id"],
                    port=solver_config["port"],
                    latency_ms=solver_config["latency_ms"],
                    quality=solver_config["quality"],
                    logger=self.logger,
                )
                solver.run(debug=False)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error starting solver {solver_config['solver_id']}: {e}")

        thread = threading.Thread(
            target=run_solver,
            name=f"solver-{solver_config['solver_id']}",
            daemon=True
        )
        thread.start()
        return thread
    
    def _wait_for_solver_ready(self, solver_config: dict, max_attempts: int = 10, delay: float = 1.0) -> bool:
        """Wait for solver to be ready by checking /health endpoint."""
        endpoint = solver_config.get("local_endpoint") or solver_config["endpoint"]
        health_url = f"{endpoint}/health"
        
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(health_url, timeout=2)
                if response.status_code == 200:
                    if self.logger:
                        self.logger.info(f"Solver is ready at {endpoint}")
                    return True
            except Exception as e:
                if attempt < max_attempts:
                    if self.logger:
                        self.logger.debug(f"Waiting for solver to start (attempt {attempt}/{max_attempts})...")
                    time.sleep(delay)
                else:
                    if self.logger:
                        self.logger.warning(f"Solver not ready after {max_attempts} attempts: {e}")
        
        return False

    def run(self):
        """Main miner lifecycle: create solvers, register them, start servers."""
        if self.logger:
            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"🔥 Starting Miner: {self.miner_id}")
            self.logger.info(f"   Mode: {self.mode}")
            self.logger.info(f"   Hotkey: {self.miner_id}")
            self.logger.info(f"   Aggregator: {self.aggregator_url}")
            self.logger.info(f"   Number of Solvers: {self.num_solvers}")
            self.logger.info(f"   Solver Type: {self.solver_type} ({self.solver_class.__name__})")
            self.logger.info(f"{'='*60}\n")

        # Step 1: Create and start all solver configurations
        for solver_index in range(self.num_solvers):
            latency = random.randint(80, 300)
            quality = random.uniform(0.85, 1.15)
            solver_config = self.create_solver(solver_index, latency, quality)

            # Step 2: Start solver server
            if self.logger:
                self.logger.info(f"Starting solver {solver_index + 1}/{self.num_solvers} (port {solver_config['port']})...")
            thread = self.start_solver_thread(solver_config)
            self.solver_threads.append(thread)

            # Wait for solver to be ready
            if self.logger:
                self.logger.info(f"Waiting for solver {solver_index + 1} to initialize...")
            # Solvers may perform token discovery and RPC init before binding the HTTP server.
            # Use a longer wait window to avoid false negatives during startup.
            if not self._wait_for_solver_ready(solver_config, max_attempts=60, delay=0.5):
                if self.logger:
                    self.logger.error(f"Solver {solver_index + 1} failed to start. Registration will likely fail.")
                continue

            # Step 3: Register solver with aggregator
            if self.logger:
                self.logger.info(f"Registering solver {solver_index + 1} with aggregator...")
            if self.register_solver(solver_config):
                if self.logger:
                    self.logger.success(f"Successfully registered solver: {solver_config['solver_id']}")
                # Track registered solver for cleanup
                self.registered_solvers.append(solver_config)
            else:
                if self.logger:
                    self.logger.error(f"Failed to register solver: {solver_config['solver_id']}")

        # Step 4: Keep running with health check loop
        if self.logger:
            self.logger.info(f"All {len(self.registered_solvers)} solver(s) running. Press Ctrl+C to stop.\n")

        # Register cleanup handlers
        def signal_handler(signum, frame):
            if self.logger:
                self.logger.info(f"\nReceived signal {signum}, shutting down...")
            self.cleanup()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        atexit.register(self.cleanup)

        # Health check configuration
        health_check_interval = int(os.environ.get("MINER_HEALTH_CHECK_INTERVAL", "30"))  # seconds
        last_health_check = time.time()
        aggregator_was_down = False

        try:
            # Keep main thread alive while solver thread runs
            while any(t.is_alive() for t in self.solver_threads):
                current_time = time.time()
                
                # Periodic health check and re-registration
                if current_time - last_health_check >= health_check_interval:
                    last_health_check = current_time
                    self._check_and_recover_solvers(aggregator_was_down)
                    
                    # Update aggregator status for next iteration
                    aggregator_was_down = not self._is_aggregator_healthy()
                
                time.sleep(1)
        except KeyboardInterrupt:
            if self.logger:
                self.logger.info("\nShutting down...")
            self.cleanup()
            # Threads are daemon, they'll stop when main exits

    def _is_aggregator_healthy(self) -> bool:
        """Check if aggregator is reachable."""
        try:
            response = requests.get(
                f"{self.aggregator_url}/health",
                timeout=5
            )
            return response.status_code == 200
        except Exception:
            return False

    def _check_and_recover_solvers(self, aggregator_was_down: bool):
        """Check solver health and re-register if needed."""
        if not self.registered_solvers:
            return
        
        # First check if aggregator is healthy
        if not self._is_aggregator_healthy():
            if self.logger:
                self.logger.warning("⚠️  Aggregator unreachable - will retry re-registration when it's back")
            return
        
        # If aggregator just came back online, log it
        if aggregator_was_down:
            if self.logger:
                self.logger.info("✅ Aggregator is back online - checking solver registrations...")
        
        for solver_config in self.registered_solvers:
            solver_id = solver_config["solver_id"]
            
            # Check if solver is still registered and active
            solver_info = self.get_solver_info(solver_id)
            
            if solver_info is None:
                # Solver not found - need to re-register
                if self.logger:
                    self.logger.warning(f"🔄 Solver {solver_id} not found on aggregator - re-registering...")
                if self.register_solver(solver_config):
                    if self.logger:
                        self.logger.success(f"✅ Re-registered solver: {solver_id}")
                else:
                    if self.logger:
                        self.logger.error(f"❌ Failed to re-register solver: {solver_id}")
            else:
                status = solver_info.get("status", "unknown")
                if status != "active":
                    # Solver exists but not active - re-register to reactivate
                    if self.logger:
                        self.logger.warning(f"🔄 Solver {solver_id} status is '{status}' - re-registering to reactivate...")
                    if self.register_solver(solver_config):
                        if self.logger:
                            self.logger.success(f"✅ Reactivated solver: {solver_id}")
                    else:
                        if self.logger:
                            self.logger.error(f"❌ Failed to reactivate solver: {solver_id}")
                else:
                    # Solver is active - check endpoint
                    current_endpoint = solver_info.get("endpoint")
                    expected_endpoint = solver_config["endpoint"]
                    if current_endpoint != expected_endpoint:
                        if self.logger:
                            self.logger.info(f"🔄 Updating solver endpoint: {solver_id}")
                        self.update_solver_endpoint(solver_config)


def main():
    """Main entry point for miner."""
    parser = argparse.ArgumentParser(description="Subnet Miner")
    
    # Wallet arguments (bittensor v10+ no longer provides add_args)
    parser.add_argument("--wallet.name", type=str, default="default", help="Wallet name")
    parser.add_argument("--wallet.hotkey", type=str, default="default", help="Hotkey name")
    parser.add_argument("--wallet.path", type=str, default="~/.bittensor/wallets", help="Wallet path")
    
    # Subtensor arguments
    parser.add_argument("--subtensor.network", type=str, default="finney", help="Bittensor network (finney, test, local)")
    parser.add_argument("--subtensor.chain_endpoint", type=str, default=None, help="Chain endpoint URL (optional)")
    
    # Logging arguments
    parser.add_argument("--logging.debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--logging.trace", action="store_true", help="Enable trace logging")
    parser.add_argument("--logging.logging_dir", type=str, default="~/.bittensor/miners", help="Logging directory")
    
    # Miner-specific arguments
    parser.add_argument("--miner.mode", choices=["simulation", "bittensor"], default="simulation",
                       help="Miner mode: simulation (testing) or bittensor (production)")
    parser.add_argument("--miner.id", type=str, default=None,
                       help="Miner identifier (required in simulation mode, defaults to wallet name in bittensor mode)")
    parser.add_argument("--aggregator.url", type=str, default="http://localhost:4000",
                       help="Aggregator API base URL")
    parser.add_argument("--aggregator.api_key", type=str, default=None,
                       help="General aggregator API key (optional)")
    parser.add_argument("--miner.api_key", type=str, default=None,
                       help="Miner API key for miner-specific endpoints (required for /v1/solvers/* endpoints)")
    parser.add_argument("--miner.base_port", type=int, default=8000,
                       help="Base port for solver server")
    parser.add_argument("--miner.solver_host", type=str, default=None,
                       help="Host address for solver endpoint (default: localhost, use host.docker.internal or host IP if aggregator is in Docker)")
    parser.add_argument("--miner.num_solvers", type=int, default=1,
                       help="Number of solvers to run (default: 1)")
    parser.add_argument("--miner.solver_type", type=str, default="v3",
                       choices=["v2", "v3", "uniswap-v2", "uniswap-v3", "base", "base-v3", "uniswap-v3-base"],
                       help="Solver type: v2/uniswap-v2 (Uniswap V2 mainnet), v3/uniswap-v3 (Uniswap V3 mainnet, default), base/base-v3/uniswap-v3-base (Uniswap V3 on Base)")
    config = bt.Config(parser)
    
    # Override from environment (command-line args take precedence over env vars)
    # Get command-line args first
    cmd_line_miner_api_key = getattr(config.miner, "api_key", None)
    
    # Environment variables (but command-line overrides them)
    miner_mode = os.getenv("MINER_MODE", config.miner.mode)
    miner_id = os.getenv("MINER_ID", config.miner.id)
    aggregator_url = os.getenv("AGGREGATOR_URL", config.aggregator.url)
    aggregator_api_key = os.getenv("AGGREGATOR_API_KEY", config.aggregator.api_key)
    # Command-line argument takes precedence over environment variable
    miner_api_key = cmd_line_miner_api_key if cmd_line_miner_api_key else os.getenv("MINER_API_KEY")
    base_port = int(os.getenv("MINER_BASE_PORT", config.miner.base_port))
    solver_host = os.getenv("MINER_SOLVER_HOST", getattr(config.miner, "solver_host", None))
    num_solvers = int(os.getenv("MINER_NUM_SOLVERS", getattr(config.miner, "num_solvers", 1)))
    solver_type = os.getenv("MINER_SOLVER_TYPE", getattr(config.miner, "solver_type", "v3"))
    
    # Auto-detect solver host if aggregator is likely in Docker
    if solver_host is None:
        # Check if aggregator URL suggests Docker (localhost or docker hostname)
        if "localhost" in aggregator_url or "127.0.0.1" in aggregator_url:
            # Try to detect if we're on Linux (use host IP) or Mac/Windows (use host.docker.internal)
            try:
                if platform.system() == "Linux":
                    # On Linux, try to get host IP
                    try:
                        host_ip = socket.gethostbyname(socket.gethostname())
                        # Only use if it's not localhost
                        if host_ip and host_ip != "127.0.0.1":
                            solver_host = host_ip
                            bt.logging.info(f"Auto-detected solver host: {solver_host} (for Docker connectivity)")
                    except:
                        pass
                else:
                    # Mac/Windows - use host.docker.internal
                    solver_host = "host.docker.internal"
                    bt.logging.info(f"Auto-detected solver host: {solver_host} (for Docker connectivity)")
            except:
                pass
    
    # Default to localhost if nothing detected
    if solver_host is None:
        solver_host = "localhost"
    
    # Initialize wallet if in bittensor mode
    wallet = None
    if miner_mode == "bittensor":
        wallet = bt.Wallet(
            name=getattr(config.wallet, "name", "default"),
            hotkey=getattr(config.wallet, "hotkey", "default"),
            path=getattr(config.wallet, "path", "~/.bittensor/wallets"),
        )
        if miner_id is None:
            miner_id = wallet.name
    
    # In simulation mode, generate miner_id if not provided
    if miner_mode == "simulation" and miner_id is None:
        hostname = socket.gethostname()
        short_uuid = str(uuid.uuid4())[:8]
        miner_id = f"simulation-miner-{hostname}-{short_uuid}"
        bt.logging.info(f"Generated miner_id: {miner_id}")
    
    # Debug: Check if MINER_API_KEY is loaded
    if not miner_api_key or not miner_api_key.strip():
        env_key = os.getenv('MINER_API_KEY')
        bt.logging.error(
            f"MINER_API_KEY is not set or is empty!\n"
            f"  Command-line arg: {repr(cmd_line_miner_api_key)}\n"
            f"  Environment variable: {repr(env_key)}\n"
            f"  Final value: {repr(miner_api_key)}\n"
            f"  Check your .env file or pass --miner.api_key on command line"
        )
    else:
        source = "command-line" if cmd_line_miner_api_key else "environment"
        bt.logging.debug(f"MINER_API_KEY loaded from {source}: {miner_api_key[:10]}... (truncated)")
    
    # Create and run miner
    miner = Miner(
        miner_id=miner_id,
        aggregator_url=aggregator_url,
        aggregator_api_key=aggregator_api_key,
        miner_api_key=miner_api_key,
        base_port=base_port,
        solver_host=solver_host,
        mode=miner_mode,
        wallet=wallet,
        num_solvers=num_solvers,
        solver_type=solver_type,
        logger=bt.logging,
    )
    
    miner.run()


if __name__ == "__main__":
    main()

