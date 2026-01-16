"""
Solver Registry - Core registration logic for external solvers.

This module provides a reusable interface for registering, deregistering,
and managing solvers with the aggregator. It can be used by:
- The miner.py for built-in solvers
- The register_solver.py CLI for external solvers
"""

import json
import os
import time
import urllib.request
from hashlib import blake2b
from typing import Dict, List, Optional, Any, Tuple

import base58

# Note: bittensor import is deferred to runtime to avoid argparser conflicts
# It will be imported only when bittensor mode is used

# Try to import nacl for ed25519 signing
try:
    from nacl.signing import SigningKey, VerifyKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


SS58_PREFIX = b"SS58PRE"
DEFAULT_ADDRESS_TYPE = 42
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


def derive_signing_key(seed: str) -> "SigningKey":
    """Derive a signing key from a seed string."""
    if not HAS_NACL:
        raise ImportError("nacl is required for ed25519 signing")
    seed_bytes = blake2b(seed.encode("utf-8"), digest_size=32).digest()
    return SigningKey(seed_bytes)


def generate_hotkey(miner_id: str) -> Tuple[str, "SigningKey"]:
    """Generate hotkey and signing key from miner_id."""
    signing_key = derive_signing_key(miner_id)
    public_key = signing_key.verify_key.encode()
    ss58_address = ss58_encode(public_key)
    return ss58_address, signing_key


class SolverRegistry:
    """
    Registry for managing solver registration with the aggregator.
    
    Supports two modes:
    - Simulation: Uses ed25519 signing with derived keys
    - Bittensor: Uses sr25519 signing with wallet hotkey
    """
    
    def __init__(
        self,
        aggregator_url: str,
        api_key: str,
        miner_id: Optional[str] = None,
        mode: str = "simulation",
        wallet: Optional[Any] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize the solver registry.
        
        Args:
            aggregator_url: Aggregator API base URL
            api_key: Miner API key for authentication
            miner_id: Miner identifier (required for simulation mode)
            mode: "simulation" or "bittensor"
            wallet: Bittensor wallet (required for bittensor mode)
            logger: Optional logger instance
        """
        self.aggregator_url = aggregator_url.rstrip('/')
        self.api_key = api_key
        self.mode = mode
        self.logger = logger
        
        if mode == "bittensor":
            if wallet is None:
                raise ValueError("Wallet is required in bittensor mode")
            # Check if bittensor is available (import deferred to avoid argparser conflict)
            try:
                import bittensor as bt
            except ImportError:
                raise ImportError("bittensor is required for bittensor mode")
            self.wallet = wallet
            self.miner_id = wallet.hotkey.ss58_address
            self.signing_keypair = wallet.hotkey
            self.signing_key = None
            self.signature_type = "sr25519"
        else:
            if not miner_id:
                raise ValueError("miner_id is required in simulation mode")
            if not HAS_NACL:
                raise ImportError("nacl is required for simulation mode signing")
            self.wallet = None
            hotkey, signing_key = generate_hotkey(miner_id)
            self.miner_id = hotkey
            self.signing_key = signing_key
            self.signing_keypair = None
            self.signature_type = "ed25519"
    
    def _log(self, level: str, msg: str):
        """Log a message if logger is available."""
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)
    
    def _sign_message(self, message: bytes) -> str:
        """Sign a message and return hex signature."""
        if self.signing_keypair is not None:
            try:
                signature_bytes = self.signing_keypair.sign(message)
            except Exception:
                signature_bytes = self.signing_keypair.sign(message).data
            if isinstance(signature_bytes, str):
                return signature_bytes if signature_bytes.startswith("0x") else f"0x{signature_bytes}"
            return "0x" + bytes(signature_bytes).hex()
        
        if self.signing_key is None:
            raise ValueError("Signing key not available")
        signature = self.signing_key.sign(message).signature
        return "0x" + signature.hex()
    
    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        timeout: int = 10
    ) -> Tuple[Optional[dict], int]:
        """Make an authenticated API request."""
        url = f"{self.aggregator_url}{endpoint}"
        
        try:
            if data:
                body = json.dumps(data).encode()
                req = urllib.request.Request(url, data=body, method=method)
                req.add_header("Content-Type", "application/json")
            else:
                req = urllib.request.Request(url, method=method)
            
            req.add_header("X-API-Key", self.api_key)
            
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode())
                return result, response.status
        except urllib.request.HTTPError as e:
            try:
                error_body = json.loads(e.read().decode())
            except:
                error_body = {"error": str(e)}
            return error_body, e.code
        except Exception as e:
            return {"error": str(e)}, 0
    
    def check_aggregator_health(self) -> Dict[str, Any]:
        """Check if aggregator is healthy."""
        result, status = self._request("GET", "/health")
        if status == 200 and result:
            return {
                "healthy": True,
                "status": result.get("status", "unknown"),
                "version": result.get("version", "unknown"),
            }
        return {"healthy": False, "error": result.get("error") if result else "No response"}
    
    def check_solver_health(self, endpoint: str) -> Dict[str, Any]:
        """Check if a solver endpoint is healthy."""
        try:
            health_url = f"{endpoint.rstrip('/')}/health"
            req = urllib.request.Request(health_url)
            
            start = time.time()
            with urllib.request.urlopen(req, timeout=5) as response:
                latency = (time.time() - start) * 1000
                result = json.loads(response.read().decode())
            
            return {
                "healthy": result.get("status") in ["healthy", "ok"],
                "latency_ms": round(latency, 1),
                "response": result
            }
        except Exception as e:
            return {"healthy": False, "error": str(e)}
    
    def get_solver_tokens(self, endpoint: str, max_attempts: int = 30, delay: float = 1.0) -> List[dict]:
        """Get supported tokens from solver."""
        tokens_url = f"{endpoint.rstrip('/')}/tokens"
        
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(tokens_url)
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                
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
            except Exception as e:
                if attempt < max_attempts:
                    time.sleep(delay)
                else:
                    self._log("warning", f"Failed to fetch tokens: {e}")
        
        return []
    
    def get_solver(self, solver_id: str) -> Optional[Dict[str, Any]]:
        """Get solver information from aggregator."""
        result, status = self._request("GET", f"/v1/solvers/{solver_id}")
        if status == 200:
            return result
        return None
    
    def list_solvers(self, miner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List registered solvers."""
        endpoint = "/v1/solvers"
        if miner_id:
            endpoint += f"?miner_id={miner_id}"
        
        result, status = self._request("GET", endpoint)
        if status == 200:
            if isinstance(result, dict) and "solvers" in result:
                return result["solvers"]
            elif isinstance(result, list):
                return result
        return []
    
    def register(
        self,
        solver_id: str,
        endpoint: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        adapter_id: str = "oif-v1",
        wait_for_tokens: bool = True,
    ) -> Dict[str, Any]:
        """
        Register a solver with the aggregator.
        
        Args:
            solver_id: Unique solver identifier
            endpoint: Solver HTTP endpoint URL
            name: Human-readable solver name
            description: Solver description
            adapter_id: Protocol adapter ID
            wait_for_tokens: Whether to wait for token discovery
        
        Returns:
            Dict with 'success', 'message', and optionally 'solver_id'
        """
        # Check if solver already exists
        existing = self.get_solver(solver_id)
        if existing:
            status = existing.get("status", "unknown")
            if status == "active":
                current_endpoint = existing.get("endpoint")
                if current_endpoint == endpoint:
                    return {"success": True, "message": "Solver already registered with same endpoint"}
                # Update endpoint
                return self.update_endpoint(solver_id, endpoint)
            else:
                self._log("info", f"Solver exists but status is '{status}', re-registering")
        
        # Check solver health
        health = self.check_solver_health(endpoint)
        if not health["healthy"]:
            return {"success": False, "message": f"Solver not healthy: {health.get('error', 'Unknown error')}"}
        
        # Get tokens
        tokens = []
        if wait_for_tokens:
            tokens = self.get_solver_tokens(endpoint)
            if not tokens:
                return {"success": False, "message": "Solver did not report any tokens"}
        
        # Build supported assets payload
        max_assets = int(os.environ.get("MOCK_MINER_REGISTRATION_LIMIT", "4096"))
        assets = []
        for token in tokens[:max_assets]:
            address = token.get("address")
            symbol = token.get("symbol") or (address[-4:].upper() if address else "TOK")
            assets.append({
                "chain_id": token.get("chain_id", 1),
                "address": address,
                "symbol": symbol,
                "name": token.get("name", symbol),
                "decimals": token.get("decimals", 18),
            })
        
        # Sign registration
        reg_message = f"{REGISTRATION_PREFIX}:{self.miner_id}:{solver_id}:{endpoint}"
        signature = self._sign_message(reg_message.encode("utf-8"))
        
        registration_data = {
            "solverId": solver_id,
            "adapterId": adapter_id,
            "endpoint": endpoint,
            "minerId": self.miner_id,
            "signature": signature,
            "signatureType": self.signature_type,
            "name": name or f"Solver {solver_id}",
            "description": description or f"Solver registered via CLI",
            "supportedAssets": {"type": "assets", "assets": assets},
        }
        
        result, status = self._request("POST", "/v1/solvers/register", registration_data)
        
        if status == 201:
            return {"success": True, "message": f"Registered solver: {solver_id}", "solver_id": solver_id}
        elif status == 400 and result and "already exists" in str(result):
            return {"success": True, "message": f"Solver already registered: {solver_id}"}
        else:
            error_msg = result.get("message", result.get("error", "Unknown error")) if result else "No response"
            return {"success": False, "message": f"Registration failed: {error_msg}", "status_code": status}
    
    def update_endpoint(self, solver_id: str, new_endpoint: str) -> Dict[str, Any]:
        """Update solver endpoint."""
        update_message = f"{UPDATE_PREFIX}:{self.miner_id}:{solver_id}:{new_endpoint}"
        signature = self._sign_message(update_message.encode("utf-8"))
        
        update_data = {
            "solverId": solver_id,
            "minerId": self.miner_id,
            "signature": signature,
            "endpoint": new_endpoint,
        }
        
        result, status = self._request("PUT", f"/v1/solvers/{solver_id}", update_data)
        
        if status == 200:
            return {"success": True, "message": f"Updated endpoint for: {solver_id}"}
        else:
            error_msg = result.get("message", "Unknown error") if result else "No response"
            return {"success": False, "message": f"Update failed: {error_msg}"}
    
    def deregister(self, solver_id: str) -> Dict[str, Any]:
        """
        Deregister a solver from the aggregator.
        
        Args:
            solver_id: Solver ID to deregister
        
        Returns:
            Dict with 'success' and 'message'
        """
        # Check ownership
        solver = self.get_solver(solver_id)
        if solver:
            registered_miner = solver.get("minerId")
            if registered_miner and registered_miner != self.miner_id:
                return {
                    "success": False,
                    "message": f"Cannot deregister: solver owned by {registered_miner}, not {self.miner_id}"
                }
        
        # Sign deletion
        delete_message = f"{DELETE_PREFIX}:{self.miner_id}:{solver_id}"
        signature = self._sign_message(delete_message.encode("utf-8"))
        
        delete_data = {
            "solverId": solver_id,
            "minerId": self.miner_id,
            "signature": signature,
            "signatureType": self.signature_type,
        }
        
        result, status = self._request("DELETE", f"/v1/solvers/{solver_id}", delete_data)
        
        if status == 200:
            return {"success": True, "message": f"Deregistered solver: {solver_id}"}
        elif status == 404:
            return {"success": True, "message": f"Solver not found (already deregistered): {solver_id}"}
        else:
            error_msg = result.get("message", "Unknown error") if result else "No response"
            return {"success": False, "message": f"Deregistration failed: {error_msg}"}
