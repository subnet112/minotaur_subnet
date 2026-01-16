"""Order simulator using Docker containers for validation.

Provides functionality to simulate orders using Docker-based simulators,
similar to the mock validator implementation.

Includes MockSimulator for testing without Docker/RPC dependencies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone


class SimulationError(Exception):
    """Exception raised when simulation fails."""
    pass


# Supported chain IDs and their names
SUPPORTED_CHAINS = {
    1: "Ethereum Mainnet",
    8453: "Base",
}


class OrderSimulator:
    """Handles order simulation using Docker containers with container reuse."""

    DEFAULT_SIMULATOR_IMAGE = "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        simulator_image: str = DEFAULT_SIMULATOR_IMAGE,
        logger: Optional[logging.Logger] = None,
        failed_simulations_dir: Optional[Path] = None,
        timeout_seconds: int = 300,
        container_pool_size: int = 1,
        simulation_script_path: str = "/app/bin/run_simulation.sh",
        chain_rpc_urls: Optional[Dict[int, str]] = None,
    ):
        """
        Initialize the order simulator with container reuse.

        Args:
            rpc_url: Default Ethereum RPC URL for simulation (chain ID 1)
            simulator_image: Docker image name for the simulator
            logger: Logger instance
            failed_simulations_dir: Directory to save failed simulation JSONs
            timeout_seconds: Timeout for simulator execution in seconds (default: 300 = 5 minutes)
            container_pool_size: Number of persistent containers to maintain (default: 1, no parallelism)
            simulation_script_path: Path to simulation script inside container (default: /app/bin/run_simulation.sh)
            chain_rpc_urls: Optional dict mapping chain_id -> rpc_url for multi-chain support
        """
        # Initialize logger first so we can use it for debugging
        self.logger = logger or logging.getLogger(__name__)
        
        # Get timeout from environment variable or use provided/default value
        timeout_env = os.getenv("SIMULATOR_TIMEOUT_SECONDS")
        if timeout_env:
            try:
                timeout_seconds = int(timeout_env)
            except ValueError:
                self.logger.warning(f"⚠️  Invalid SIMULATOR_TIMEOUT_SECONDS value: {timeout_env}, using default {timeout_seconds}")
        
        self.timeout_seconds = timeout_seconds
        self.logger.debug(f"🔍 Simulator timeout: {self.timeout_seconds} seconds ({self.timeout_seconds / 60:.1f} minutes)")
        
        # Build chain RPC URL mapping
        # Priority: explicit chain_rpc_urls > environment variables > rpc_url parameter
        eth_rpc = os.getenv("ETHEREUM_RPC_URL")
        infura_rpc = os.getenv("INFURA_RPC_URL")
        sim_rpc = os.getenv("SIMULATOR_RPC_URL")
        base_rpc = os.getenv("BASE_RPC_URL")
        
        self.logger.debug(
            f"🔍 RPC URL sources: rpc_url param={rpc_url is not None}, "
            f"ETHEREUM_RPC_URL={'SET' if eth_rpc else 'NOT SET'}, "
            f"INFURA_RPC_URL={'SET' if infura_rpc else 'NOT SET'}, "
            f"SIMULATOR_RPC_URL={'SET' if sim_rpc else 'NOT SET'}, "
            f"BASE_RPC_URL={'SET' if base_rpc else 'NOT SET'}"
        )
        
        # Default RPC URL (for chain ID 1 / Ethereum mainnet)
        self.rpc_url = rpc_url or eth_rpc or infura_rpc or sim_rpc or "https://mainnet.infura.io/v3/<KEY>"
        
        # Build chain-specific RPC URL mapping
        self.chain_rpc_urls: Dict[int, str] = {}
        
        # Start with provided chain_rpc_urls if any
        if chain_rpc_urls:
            self.chain_rpc_urls.update(chain_rpc_urls)
        
        # Add/override with environment variables
        if self.rpc_url and self.rpc_url != "https://mainnet.infura.io/v3/<KEY>":
            self.chain_rpc_urls[1] = self.rpc_url  # Ethereum mainnet
        
        if base_rpc:
            self.chain_rpc_urls[8453] = base_rpc  # Base
        
        # Log configured chains
        if self.chain_rpc_urls:
            chains_str = ", ".join([
                f"{SUPPORTED_CHAINS.get(cid, f'Chain {cid}')} ({cid})"
                for cid in sorted(self.chain_rpc_urls.keys())
            ])
            self.logger.info(f"🔗 Simulator configured for chains: {chains_str}")
        else:
            self.logger.warning("⚠️  No valid RPC URLs provided. Simulation may fail. Set ETHEREUM_RPC_URL, BASE_RPC_URL, or SIMULATOR_RPC_URL environment variable.")
        
        self.simulator_image = simulator_image
        self.logger.debug(f"🔍 Simulator image: {self.simulator_image}")
        
        # Container pool configuration
        self.container_pool_size = max(1, container_pool_size)
        self._container_names: list = []  # List[str] but using list for Python 3.8 compatibility
        self._container_lock = asyncio.Lock()
        self.simulation_script_path = simulation_script_path
        
        # Register cleanup on exit
        import atexit
        atexit.register(self.cleanup_containers)
        
        # Initialize failed_simulations_dir
        if failed_simulations_dir is None:
            default_dir = Path(__file__).parent / "failed_simulations"
            self.logger.debug(f"🔍 Using default failed_simulations_dir: {default_dir}")
            self.failed_simulations_dir = default_dir
        else:
            self.logger.debug(f"🔍 Using provided failed_simulations_dir: {failed_simulations_dir}")
            self.failed_simulations_dir = failed_simulations_dir
        
        try:
            self.failed_simulations_dir.mkdir(exist_ok=True)
            self.logger.debug(f"🔍 Initialized OrderSimulator: rpc_url={self.rpc_url is not None}, image={self.simulator_image}, failed_dir={self.failed_simulations_dir}, pool_size={self.container_pool_size}")
        except Exception as e:
            self.logger.error(f"🔍 Failed to create failed_simulations_dir: {e}")
            raise

        # Optionally auto-pull the simulator image so operators always run the latest version.
        # This is especially useful for validators where the simulator contract logic can evolve.
        #
        # Opt-out by setting SIMULATOR_AUTO_PULL=0/false/no.
        auto_pull = os.getenv("SIMULATOR_AUTO_PULL", "true").lower() in ("1", "true", "yes")
        if auto_pull:
            self._pull_simulator_image()
        else:
            self.logger.info("⏭️  SIMULATOR_AUTO_PULL disabled - skipping docker pull of simulator image")
        
        # Start container pool
        self._start_container_pool()
        
        # Track current container index for round-robin selection
        self._current_container_index = 0

    def _start_container_pool(self):
        """Start persistent Docker containers for reuse."""
        import uuid
        
        self.logger.info(f"🚀 Starting container pool with {self.container_pool_size} container(s)...")
        
        for i in range(self.container_pool_size):
            container_name = f"mino-simulation-{uuid.uuid4().hex[:8]}"
            
            try:
                # Start a container that stays running
                # Format: docker run -d --name <name> --entrypoint /bin/bash <image> -c "tail -f /dev/null"
                cmd = [
                    "docker", "run", "-d",
                    "--name", container_name,
                    "--entrypoint", "/bin/bash",
                    self.simulator_image,
                    "-c", "tail -f /dev/null"
                ]
                
                self.logger.debug(f"🔍 Starting container {i+1}/{self.container_pool_size}: {container_name}")
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,  # 30 second timeout for container startup
                    check=False
                )
                
                if result.returncode == 0:
                    container_id = result.stdout.strip()
                    self._container_names.append(container_name)
                    self.logger.info(f"✅ Started container {i+1}/{self.container_pool_size}: {container_name} ({container_id[:12]})")
                else:
                    # Check if container already exists
                    if "already in use" in result.stderr or "Conflict" in result.stderr:
                        # Try to remove and recreate
                        self.logger.warning(f"⚠️  Container {container_name} already exists, removing...")
                        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
                        # Retry creation
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
                        if result.returncode == 0:
                            self._container_names.append(container_name)
                            self.logger.info(f"✅ Recreated container {i+1}/{self.container_pool_size}: {container_name}")
                        else:
                            self.logger.error(f"❌ Failed to create container {container_name}: {result.stderr}")
                    else:
                        self.logger.error(f"❌ Failed to start container {i+1}/{self.container_pool_size}: {result.stderr}")
                        
            except Exception as e:
                self.logger.error(f"❌ Error starting container {i+1}/{self.container_pool_size}: {e}")
        
        if not self._container_names:
            raise RuntimeError("Failed to start any simulation containers. Check Docker is running and image exists.")
        
        self.logger.info(f"✅ Container pool ready: {len(self._container_names)} container(s) available")

    def _pull_simulator_image(self) -> None:
        """Pull the configured simulator image (best-effort).

        If the pull fails, we continue and let container startup surface the error.
        """
        if not self.simulator_image:
            return
        try:
            self.logger.info(f"⬇️  Pulling simulator image: {self.simulator_image}")
            result = subprocess.run(
                ["docker", "pull", self.simulator_image],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if result.returncode == 0:
                self.logger.info(f"✅ Pulled simulator image: {self.simulator_image}")
            else:
                stderr = (result.stderr or "").strip()
                self.logger.warning(
                    f"⚠️  Failed to pull simulator image {self.simulator_image} (exit {result.returncode}). "
                    f"Will try to run with local image if present. Error: {stderr[:300]}"
                )
        except FileNotFoundError:
            self.logger.warning("⚠️  docker CLI not found; cannot auto-pull simulator image")
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️  Timed out pulling simulator image: {self.simulator_image}")
        except Exception as exc:
            self.logger.warning(f"⚠️  Unexpected error pulling simulator image: {exc}")

    def _get_container_name(self) -> Optional[str]:
        """Get next container name using round-robin selection."""
        if not self._container_names:
            return None
        
        container_name = self._container_names[self._current_container_index]
        self._current_container_index = (self._current_container_index + 1) % len(self._container_names)
        return container_name

    def _check_container_health(self, container_name: str) -> bool:
        """Check if a container is running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
            return container_name in result.stdout
        except Exception:
            return False

    def _restart_container(self, container_name: str) -> bool:
        """Restart a failed container."""
        try:
            self.logger.warning(f"⚠️  Restarting container {container_name}...")
            # Remove old container
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
            # Start new container
            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--entrypoint", "/bin/bash",
                self.simulator_image,
                "-c", "tail -f /dev/null"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode == 0:
                self.logger.info(f"✅ Restarted container {container_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to restart container {container_name}: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"❌ Error restarting container {container_name}: {e}")
            return False

    def cleanup_containers(self):
        """Stop and remove all containers in the pool."""
        if not self._container_names:
            return
        
        self.logger.info(f"🧹 Cleaning up {len(self._container_names)} container(s)...")
        for container_name in self._container_names:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=10,
                    check=False
                )
                self.logger.debug(f"✅ Removed container {container_name}")
            except Exception as e:
                self.logger.warning(f"⚠️  Failed to remove container {container_name}: {e}")
        
        self._container_names.clear()
        self.logger.info("✅ Container cleanup complete")

    def _extract_chain_id(self, simulator_data: dict) -> int:
        """Extract chain ID from simulator data.
        
        Looks for chain ID in multiple places:
        1. quoteDetails.settlement.chainId
        2. quoteDetails.originChainId
        3. Parse from interop address in quoteDetails.details.availableInputs[0].asset
        4. Parse from interop address in quoteDetails.availableInputs[0].asset (fallback)
        
        Returns chain ID or 1 (Ethereum mainnet) as default.
        """
        try:
            quote_details = simulator_data.get("quoteDetails", {})
            
            # Try settlement chainId first
            settlement = quote_details.get("settlement", {})
            if isinstance(settlement, dict):
                chain_id = settlement.get("chainId")
                if chain_id is not None:
                    self.logger.debug(f"🔍 Extracted chain ID {chain_id} from settlement.chainId")
                    return int(chain_id)
            
            # Try originChainId
            origin_chain_id = quote_details.get("originChainId")
            if origin_chain_id is not None:
                self.logger.debug(f"🔍 Extracted chain ID {origin_chain_id} from originChainId")
                return int(origin_chain_id)
            
            # Try to extract from interop address - check nested "details" first (OIF format)
            details = quote_details.get("details", {})
            
            # Try availableInputs from details (OIF format: quoteDetails.details.availableInputs)
            available_inputs = details.get("availableInputs", [])
            if not available_inputs:
                # Fallback: try direct availableInputs (alternative format)
                available_inputs = quote_details.get("availableInputs", [])
            
            if available_inputs and len(available_inputs) > 0:
                asset = available_inputs[0].get("asset", "")
                chain_id = self._parse_chain_id_from_interop(asset)
                if chain_id:
                    self.logger.debug(f"🔍 Extracted chain ID {chain_id} from availableInputs interop address")
                    return chain_id
            
            # Try requestedOutputs from details
            requested_outputs = details.get("requestedOutputs", [])
            if not requested_outputs:
                # Fallback: try direct requestedOutputs
                requested_outputs = quote_details.get("requestedOutputs", [])
            
            if requested_outputs and len(requested_outputs) > 0:
                asset = requested_outputs[0].get("asset", "")
                chain_id = self._parse_chain_id_from_interop(asset)
                if chain_id:
                    self.logger.debug(f"🔍 Extracted chain ID {chain_id} from requestedOutputs interop address")
                    return chain_id
            
        except Exception as e:
            self.logger.debug(f"🔍 Error extracting chain ID: {e}")
        
        # Default to Ethereum mainnet
        self.logger.debug("🔍 Could not extract chain ID, defaulting to 1 (Ethereum mainnet)")
        return 1

    def _parse_chain_id_from_interop(self, interop_address: str) -> Optional[int]:
        """Parse chain ID from ERC-7930 interop address.
        
        Format: 0x01 + 00 00 (chain type) + chain_ref_len + addr_len + chain_ref + address
        """
        try:
            if not interop_address or not interop_address.startswith("0x"):
                return None
            
            hex_part = interop_address[2:]
            if len(hex_part) < 10:  # Minimum: version(2) + chain_type(4) + lens(4)
                return None
            
            # Parse ERC-7930 format
            # Bytes: [0]=version, [1-2]=chain_type, [3]=chain_ref_len, [4]=addr_len
            chain_ref_len = int(hex_part[6:8], 16)
            
            if chain_ref_len == 0:
                return 1  # Default chain
            
            # Extract chain reference bytes
            chain_ref_start = 10  # After version(2) + chain_type(4) + lens(4)
            chain_ref_end = chain_ref_start + (chain_ref_len * 2)
            chain_ref_hex = hex_part[chain_ref_start:chain_ref_end]
            
            if chain_ref_hex:
                return int(chain_ref_hex, 16)
            
        except Exception:
            pass
        
        return None

    def _get_rpc_url_for_chain(self, chain_id: int) -> str:
        """Get the RPC URL for a specific chain ID."""
        rpc_url = self.chain_rpc_urls.get(chain_id)
        
        if rpc_url:
            return rpc_url
        
        # Fall back to default RPC URL with warning
        chain_name = SUPPORTED_CHAINS.get(chain_id, f"Chain {chain_id}")
        self.logger.warning(
            f"⚠️  No RPC URL configured for {chain_name} (chain ID {chain_id}). "
            f"Using default RPC (may fail). Set {'BASE_RPC_URL' if chain_id == 8453 else 'ETHEREUM_RPC_URL'} environment variable."
        )
        return self.rpc_url

    def _sanitize_decimal_string(self, value: Any) -> str:
        """Ensure a value is a valid decimal string for uint256 fields."""
        if value is None:
            return "0"
        value_str = str(value).strip()
        if not value_str or value_str == "":
            return "0"
        # If it's a hex string, convert to decimal
        if value_str.startswith("0x"):
            hex_part = value_str[2:].strip()
            if not hex_part:
                return "0"
            try:
                num = int(hex_part, 16)
                return str(num)
            except ValueError:
                return "0"
        # If it's already a decimal string, validate it's a number
        try:
            num = int(value_str, 10)
            return str(num)
        except ValueError:
            return "0"

    def _sanitize_hex_string(self, value: Any) -> str:
        """Ensure a value is a valid hex string for callData fields."""
        if value is None:
            return "0x"
        value_str = str(value).strip()
        if not value_str or value_str == "":
            return "0x"
        # If it already starts with 0x, validate it
        if value_str.startswith("0x"):
            hex_part = value_str[2:].strip()
            if not hex_part:
                return "0x"  # Empty hex string
            # Validate hex digits
            try:
                int(hex_part, 16)
                return value_str  # Valid hex string
            except ValueError:
                return "0x"  # Invalid hex, return empty
        # If it doesn't start with 0x, try to interpret as hex number
        try:
            num = int(value_str, 16)
            return hex(num) if num > 0 else "0x"
        except ValueError:
            # Not a valid hex number, return empty
            return "0x"

    def _sanitize_interaction(self, interaction: dict) -> dict:
        """Sanitize an interaction to ensure all fields are valid."""
        target = interaction.get("target")
        if target is None:
            target = ""

        return {
            "target": target,
            "value": self._sanitize_decimal_string(interaction.get("value", "0")),
            "callData": self._sanitize_hex_string(interaction.get("callData", "")),
        }

    def _compute_interactions_hash(self, execution_plan: dict) -> str:
        """Compute canonical keccak256 hash of the execution plan."""
        try:
            from eth_utils import keccak
            from eth_utils.address import to_canonical_address
        except ImportError:
            self.logger.error("eth_utils not installed, cannot compute interactions hash")
            return "0x0"

        encoded = bytearray()
        for interaction in execution_plan.get("preInteractions", []) + \
                          execution_plan.get("interactions", []) + \
                          execution_plan.get("postInteractions", []):
            # Encode target address (20 bytes)
            target = interaction.get("target", "")
            if target:
                target_bytes = to_canonical_address(target)
                encoded.extend(target_bytes)
            else:
                encoded.extend(bytes(20))  # Zero address

            # Encode value as 32-byte big-endian uint256
            value_str = interaction.get("value", "0")
            try:
                value_int = int(value_str, 10)
            except (ValueError, TypeError):
                value_int = 0
            value_bytes = value_int.to_bytes(32, byteorder="big")
            encoded.extend(value_bytes)

            # Encode keccak256 hash of callData
            call_data_hex = interaction.get("callData", "0x")
            if call_data_hex.startswith("0x"):
                call_data_hex = call_data_hex[2:]
            call_data_bytes = bytes.fromhex(call_data_hex) if call_data_hex else b""
            call_hash = keccak(call_data_bytes)
            encoded.extend(call_hash)

        # Final hash of all encoded interactions
        final_hash = keccak(bytes(encoded))
        return "0x" + final_hash.hex()

    def _sanitize_execution_plan(self, execution_plan: dict) -> dict:
        """Sanitize execution plan to ensure all hex fields are valid."""
        # Sanitize blockNumber - ensure it's a string, not null
        block_number = execution_plan.get("blockNumber")
        if block_number is None:
            block_number = None  # Keep as None if missing (optional field)
        else:
            block_number = str(block_number)

        sanitized = {
            "blockNumber": block_number,
            "preInteractions": [
                self._sanitize_interaction(interaction)
                for interaction in execution_plan.get("preInteractions", [])
            ],
            "interactions": [
                self._sanitize_interaction(interaction)
                for interaction in execution_plan.get("interactions", [])
            ],
            "postInteractions": [
                self._sanitize_interaction(interaction)
                for interaction in execution_plan.get("postInteractions", [])
            ],
        }

        # Add interactions hash for debugging
        sanitized["_interactionsHash"] = self._compute_interactions_hash(sanitized)

        return sanitized

    def _prepare_simulator_data(self, order: dict) -> Optional[dict]:
        """Extract quoteDetails and signature from order for simulator.
        
        The simulator expects quoteDetails and signature fields.
        Format: {"quoteDetails": {...}, "signature": "0x..."}
        """
        quote_details = order.get("quoteDetails")
        if not quote_details:
            self.logger.error(f"🔍 Order missing quoteDetails: {list(order.keys())}")
            return None
        
        # Extract signature from order (required by simulator)
        signature = order.get("signature")
        if not signature:
            order_id = order.get("orderId", "unknown")
            self.logger.error(f"🔍 Order {order_id} missing signature field - simulator will fail. Order keys: {list(order.keys())}")
            # Return payload anyway - simulator will provide clear error message
            return {"quoteDetails": quote_details}
        
        # Return quoteDetails and signature as the simulator expects
        payload = {
            "quoteDetails": quote_details,
            "signature": signature
        }
        
        self.logger.debug(f"🔍 Prepared simulator payload: quoteDetails present, signature present (length: {len(signature)})")
        return payload

    def _save_failed_simulation(self, order_id: str, simulator_data: dict, error_message: Optional[str] = None, full_order: Optional[dict] = None):
        """Save failed simulation JSON to a file for manual debugging.
        
        Args:
            order_id: The order ID
            simulator_data: The prepared simulator payload (quoteDetails + signature)
            error_message: Error message if available
            full_order: The complete original order JSON (optional, for debugging)
        """
        try:
            # Debug: Check failed_simulations_dir
            if self.failed_simulations_dir is None:
                self.logger.error(f"🔍 Cannot save failed simulation: failed_simulations_dir is None")
                return
            
            # Create filename with order ID and timestamp
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            # Sanitize order_id for filename (remove invalid chars)
            safe_order_id = "".join(c for c in order_id if c.isalnum() or c in "-_")[:50]
            filename = f"failed_{safe_order_id}_{timestamp}.json"
            
            self.logger.debug(f"🔍 Saving failed simulation: dir={self.failed_simulations_dir}, filename={filename}")
            filepath = self.failed_simulations_dir / filename

            # Create a complete record with metadata
            record = {
                "orderId": order_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "errorMessage": error_message,
                "simulatorData": simulator_data,
                "rpcUrl": self.rpc_url,
                "simulatorImage": self.simulator_image,
            }
            
            # Include full order if provided (for debugging)
            if full_order is not None:
                record["fullOrder"] = full_order

            # Write to file with pretty formatting
            with open(filepath, "w") as f:
                json.dump(record, f, indent=2)

            self.logger.info(f"💾 Saved failed simulation JSON to: {filepath}")
            if self._container_names:
                container_name = self._container_names[0] if self._container_names else "mino-simulation-container"
                self.logger.info(f"   To simulate manually: cat {filepath} | jq -c .simulatorData | docker exec -i {container_name} env -u SIM_INPUT_PATH {self.simulation_script_path} '' '{self.rpc_url}'")
            else:
                self.logger.info(f"   To simulate manually: cat {filepath} | jq -c .simulatorData | docker exec -i <container-name> env -u SIM_INPUT_PATH {self.simulation_script_path} '' '{self.rpc_url}'")
        except Exception as e:
            self.logger.error(f"⚠️  Failed to save simulation JSON: {e}")

    def _call_simulator(self, simulator_data: dict) -> Tuple[Optional[dict], Optional[str]]:
        """Call the Docker-based simulator with the order data."""
        try:
            # Extract chain ID and get appropriate RPC URL
            chain_id = self._extract_chain_id(simulator_data)
            rpc_url = self._get_rpc_url_for_chain(chain_id)
            chain_name = SUPPORTED_CHAINS.get(chain_id, f"Chain {chain_id}")
            
            # Debug: Log simulator configuration
            self.logger.debug(f"🔍 Simulator config: chain={chain_name} ({chain_id}), rpc_url={rpc_url[:50]}..., image={self.simulator_image}")
            
            # Validate RPC URL before calling simulator
            if not rpc_url or rpc_url == "https://mainnet.infura.io/v3/<KEY>":
                error_msg = f"Invalid or missing RPC URL for {chain_name} (chain ID {chain_id}). Set {'BASE_RPC_URL' if chain_id == 8453 else 'ETHEREUM_RPC_URL'} environment variable."
                self.logger.error(f"⚠️  {error_msg}")
                raise ValueError(error_msg)
            
            # Validate simulator image
            if not self.simulator_image:
                error_msg = f"Invalid or missing simulator image. Current value: {repr(self.simulator_image)}"
                self.logger.error(f"⚠️  {error_msg}")
                raise ValueError(error_msg)
            
            # Validate failed_simulations_dir
            if self.failed_simulations_dir is None:
                error_msg = f"Invalid or missing failed_simulations_dir. Current value: {repr(self.failed_simulations_dir)}"
                self.logger.error(f"⚠️  {error_msg}")
                raise ValueError(error_msg)
            
            # Convert simulator data to JSON string
            try:
                json_input = json.dumps(simulator_data, ensure_ascii=False)
                # Validate JSON can be parsed back (ensures it's valid JSON)
                json.loads(json_input)
                self.logger.debug(f"🔍 Prepared simulator data (length: {len(json_input)} chars)")
                # Log first 500 chars of JSON for debugging
                self.logger.debug(f"🔍 JSON preview: {json_input[:500]}...")
            except (TypeError, ValueError) as json_err:
                error_msg = f"Failed to serialize simulator data to JSON: {json_err}"
                self.logger.error(f"⚠️  {error_msg}")
                self.logger.error(f"🔍 Simulator data type: {type(simulator_data)}")
                self.logger.error(f"🔍 Simulator data keys: {simulator_data.keys() if isinstance(simulator_data, dict) else 'N/A'}")
                raise ValueError(error_msg) from json_err
            
            # Validate all command components before building command
            self.logger.debug(f"🔍 Validating command components: simulator_image={repr(self.simulator_image)}, rpc_url={repr(rpc_url)}")
            if self.simulator_image is None:
                raise ValueError(f"simulator_image is None")
            if rpc_url is None:
                raise ValueError(f"rpc_url is None for chain {chain_id}")
            
            # Get container name from pool (round-robin)
            container_name = self._get_container_name()
            if not container_name:
                error_msg = "No containers available in pool"
                self.logger.error(f"⚠️  {error_msg}")
                raise RuntimeError(error_msg)
            
            # Check container health
            if not self._check_container_health(container_name):
                self.logger.warning(f"⚠️  Container {container_name} is not running, attempting restart...")
                if not self._restart_container(container_name):
                    error_msg = f"Container {container_name} is not available and restart failed"
                    self.logger.error(f"⚠️  {error_msg}")
                    raise RuntimeError(error_msg)
            
            # Run simulation using docker exec on persistent container
            # Format: docker exec -i <container> env -u SIM_INPUT_PATH <script_path> '' '<rpc_url>'
            # JSON is piped via stdin to avoid command-line length limits
            # We use env -u to unset SIM_INPUT_PATH so the script reads from stdin instead of trying to read from a file
            # Using env -u instead of sh -c ensures stdin is properly passed to the script
            cmd = [
                "docker", "exec", "-i",
                container_name,
                "env", "-u", "SIM_INPUT_PATH",
                self.simulation_script_path,
                "",  # Empty first argument (JSON comes from stdin)
                str(rpc_url)  # Use chain-specific RPC URL
            ]
            
            self.logger.debug(f"🔍 Executing Docker exec command on container {container_name} for {chain_name}")
            self.logger.debug(f"🔍 Command: docker exec -i {container_name} env -u SIM_INPUT_PATH {self.simulation_script_path} '' [RPC_URL for chain {chain_id}]")
            self.logger.debug(f"🔍 JSON will be piped via stdin (length: {len(json_input)} chars)")

            result = subprocess.run(
                cmd,
                input=json_input,  # Pipe JSON via stdin
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,  # Configurable timeout (default: 300 seconds = 5 minutes)
                check=False  # Don't raise on non-zero exit
            )

            if result.returncode != 0:
                # Collect error message from stderr and stdout
                error_parts = []
                error_parts.append(f"Simulator returned exit code {result.returncode}")

                if result.stderr:
                    # Extract meaningful error from stderr (often contains the actual error)
                    stderr_lines = result.stderr.strip().split('\n')
                    # Look for error messages (skip warnings and verbose output)
                    for line in stderr_lines:
                        if any(keyword in line.lower() for keyword in ['error', 'failed', 'revert', 'invalid', 'mismatch']):
                            error_parts.append(line.strip())
                    # If no specific error found, include last few lines
                    if len(error_parts) == 1 and stderr_lines:
                        error_parts.append(stderr_lines[-1].strip())

                if result.stdout:
                    # Sometimes errors are in stdout
                    stdout_lines = result.stdout.strip().split('\n')
                    for line in stdout_lines:
                        if any(keyword in line.lower() for keyword in ['error', 'failed', 'revert']):
                            error_parts.append(line.strip())

                error_message = " | ".join(error_parts[:3])  # Limit to first 3 error parts

                self.logger.warning(f"⚠️  Simulator returned non-zero exit code {result.returncode}")
                self.logger.error(f"🔍 Full stderr: {result.stderr}")
                self.logger.error(f"🔍 Full stdout: {result.stdout}")
                self.logger.error(f"🔍 JSON payload that was sent (first 2000 chars): {json_input[:2000]}")
                # Also log the structure of simulator_data
                if isinstance(simulator_data, dict):
                    self.logger.error(f"🔍 Simulator data keys: {list(simulator_data.keys())}")
                    if "quoteDetails" in simulator_data:
                        quote_details = simulator_data["quoteDetails"]
                        if isinstance(quote_details, dict) and "settlement" in quote_details:
                            settlement = quote_details["settlement"]
                            if isinstance(settlement, dict):
                                self.logger.error(f"🔍 Settlement keys: {list(settlement.keys())}")
                                if "executionPlan" in settlement:
                                    exec_plan = settlement["executionPlan"]
                                    self.logger.error(f"🔍 Execution plan keys: {list(exec_plan.keys()) if isinstance(exec_plan, dict) else 'N/A'}")

                return None, error_message

            # Parse simulator output
            try:
                output = result.stdout.strip()
                # Handle case where Docker might add extra output
                if output.startswith("{"):
                    # Find the JSON object in the output
                    json_start = output.find("{")
                    json_end = output.rfind("}") + 1
                    if json_start >= 0 and json_end > json_start:
                        output = output[json_start:json_end]

                simulator_result = json.loads(output)
                return simulator_result, None
            except json.JSONDecodeError as e:
                error_message = f"Failed to parse simulator output: {str(e)}"
                self.logger.warning(f"⚠️  {error_message}")
                self.logger.debug(f"   Output: {result.stdout[:500]}")
                return None, error_message

        except subprocess.TimeoutExpired:
            error_message = f"Simulator call timed out after {self.timeout_seconds} seconds ({self.timeout_seconds / 60:.1f} minutes)"
            self.logger.warning(f"⚠️  {error_message}")
            return None, error_message
        except FileNotFoundError:
            error_message = "Docker not found. Please ensure Docker is installed and in PATH"
            self.logger.warning(f"⚠️  {error_message}")
            return None, error_message
        except Exception as exc:
            error_message = f"Error calling simulator: {str(exc)}"
            self.logger.error(f"⚠️  {error_message}")
            self.logger.error(f"🔍 Exception type: {type(exc).__name__}")
            self.logger.error(f"🔍 Exception args: {exc.args}")
            import traceback
            self.logger.debug(f"🔍 Full traceback:\n{traceback.format_exc()}")
            return None, error_message

    async def simulate_order(self, order: dict) -> Tuple[bool, Optional[str]]:
        """Simulate order using the Docker-based simulator.

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        simulator_data = self._prepare_simulator_data(order)
        if not simulator_data:
            return False, "Failed to prepare simulator data"

        # Run simulation in thread pool since subprocess is blocking
        loop = asyncio.get_event_loop()
        simulator_result, error_message = await loop.run_in_executor(
            None, self._call_simulator, simulator_data
        )

        order_id = order.get("orderId", "unknown")

        if simulator_result is None:
            # Save failed simulation JSON for debugging (include full order)
            self._save_failed_simulation(order_id, simulator_data, error_message, full_order=order)
            return False, error_message or "Simulator call failed"

        success = simulator_result.get("success", False)

        # If simulation failed, save the JSON for debugging (include full order)
        if not success:
            error_msg = simulator_result.get("errorMessage") or error_message or "Simulation failed"
            self._save_failed_simulation(order_id, simulator_data, error_msg, full_order=order)
            return False, error_msg

        return True, None


class MockSimulator(OrderSimulator):
    """Mock simulator for testing without Docker/RPC dependencies.

    Simulates order validation without actually running Docker containers
    or making RPC calls. Useful for testing the validation pipeline.
    """

    def __init__(
        self,
        success_rate: float = 0.8,
        min_delay_ms: int = 100,
        max_delay_ms: int = 500,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the mock simulator.

        Args:
            success_rate: Probability of simulation success (0.0-1.0)
            min_delay_ms: Minimum simulation delay in milliseconds
            max_delay_ms: Maximum simulation delay in milliseconds
            logger: Logger instance
        """
        # Don't call super().__init__ since we don't need Docker/RPC setup
        self.success_rate = success_rate
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.logger = logger or logging.getLogger(__name__)

        # Mock statistics
        self.total_simulations = 0
        self.successful_simulations = 0

    async def simulate_order(self, order: dict) -> Tuple[bool, Optional[str]]:
        """Mock order simulation with configurable success rate and delay."""
        import random
        import time

        order_id = order.get("orderId", "unknown")
        self.total_simulations += 1

        # Simulate processing delay
        delay_ms = random.randint(self.min_delay_ms, self.max_delay_ms)
        await asyncio.sleep(delay_ms / 1000.0)

        # Determine success based on configured rate
        success = random.random() < self.success_rate

        if success:
            self.successful_simulations += 1
            result_msg = f"Mock simulation succeeded (delay: {delay_ms}ms)"
            self.logger.debug(f"✅ {order_id}: {result_msg}")
            return True, None
        else:
            # Generate a realistic-looking error
            errors = [
                "Transaction reverted: insufficient balance",
                "Transaction reverted: execution timeout",
                "Invalid opcode at address",
                "Stack overflow in contract execution",
                "Gas estimation failed",
            ]
            error_msg = random.choice(errors)
            result_msg = f"Mock simulation failed: {error_msg} (delay: {delay_ms}ms)"
            self.logger.debug(f"❌ {order_id}: {result_msg}")
            return False, error_msg

    def get_stats(self) -> Dict[str, Any]:
        """Get mock simulation statistics."""
        return {
            "total_simulations": self.total_simulations,
            "successful_simulations": self.successful_simulations,
            "success_rate": self.successful_simulations / self.total_simulations if self.total_simulations > 0 else 0.0,
            "success_rate_config": self.success_rate,
        }

    def reset_stats(self):
        """Reset simulation statistics."""
        self.total_simulations = 0
        self.successful_simulations = 0
