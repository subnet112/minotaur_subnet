#!/usr/bin/env python3
"""
Minotaur Configuration Validator - Validate your .env configuration.

This script checks your configuration files for errors and validates
connectivity to required services before starting.

Usage:
    python scripts/validate_config.py                    # Validate ./validator.env and ./miner.env
    python scripts/validate_config.py --env-file .env   # Validate specific file
    python scripts/validate_config.py --fix             # Attempt to fix common issues
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ANSI color codes
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'


def colored(text: str, color: str) -> str:
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.END}"
    return text


def print_header(text: str):
    print()
    print(colored("═" * 60, Colors.CYAN))
    print(colored(f"  {text}", Colors.BOLD + Colors.CYAN))
    print(colored("═" * 60, Colors.CYAN))
    print()


def print_section(text: str):
    print()
    print(colored(f"▶ {text}", Colors.BOLD))
    print(colored("─" * 40, Colors.DIM))


def print_ok(text: str):
    print(colored(f"  ✅ {text}", Colors.GREEN))


def print_warn(text: str):
    print(colored(f"  ⚠️  {text}", Colors.YELLOW))


def print_error(text: str):
    print(colored(f"  ❌ {text}", Colors.RED))


def print_info(text: str):
    print(colored(f"  ℹ️  {text}", Colors.CYAN))


class ConfigValidator:
    """Validate Minotaur configuration."""
    
    # Required variables for each role
    VALIDATOR_REQUIRED = [
        "AGGREGATOR_URL",
        "VALIDATOR_API_KEY",
    ]
    
    VALIDATOR_RECOMMENDED = [
        "ETHEREUM_RPC_URL",
        "BASE_RPC_URL",
    ]
    
    MINER_REQUIRED = [
        "AGGREGATOR_URL",
        "MINER_API_KEY",
        "MINER_ID",
    ]
    
    MINER_RECOMMENDED = [
        "ETHEREUM_RPC_URL",
        "BASE_RPC_URL",
        "MINER_SOLVER_HOST",
    ]
    
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.config: Dict[str, str] = {}
    
    def load_env_file(self, path: Path) -> bool:
        """Load environment variables from a file."""
        if not path.exists():
            self.errors.append(f"File not found: {path}")
            return False
        
        try:
            with open(path) as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    
                    if '=' not in line:
                        self.warnings.append(f"Line {line_num}: Invalid format (no '=' found)")
                        continue
                    
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    
                    if not key:
                        self.warnings.append(f"Line {line_num}: Empty key")
                        continue
                    
                    self.config[key] = value
            
            return True
        except Exception as e:
            self.errors.append(f"Failed to read {path}: {e}")
            return False
    
    def validate_required(self, required: List[str], role: str):
        """Check that required variables are set."""
        for var in required:
            value = self.config.get(var, "")
            if not value:
                self.errors.append(f"{var} is required for {role} but not set")
            elif value.startswith("your-") or value.endswith("-here") or "<" in value:
                self.errors.append(f"{var} appears to be a placeholder value")
    
    def validate_recommended(self, recommended: List[str], role: str):
        """Check recommended variables."""
        for var in recommended:
            value = self.config.get(var, "")
            if not value:
                self.warnings.append(f"{var} is recommended for {role}")
    
    def validate_rpc_url(self, var_name: str, expected_chain_id: int) -> bool:
        """Validate an RPC URL by testing connectivity."""
        url = self.config.get(var_name, "")
        if not url:
            return False
        
        try:
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_chainId",
                "params": [],
                "id": 1
            }).encode()
            
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
            
            if "error" in result:
                self.errors.append(f"{var_name}: RPC error - {result['error'].get('message', 'Unknown')}")
                return False
            
            chain_id = int(result.get("result", "0x0"), 16)
            if chain_id != expected_chain_id:
                self.errors.append(f"{var_name}: Wrong chain ID (expected {expected_chain_id}, got {chain_id})")
                return False
            
            # Check if using Alchemy
            using_alchemy = "alchemy.com" in url.lower()
            chain_names = {1: "Ethereum", 8453: "Base"}
            chain_name = chain_names.get(chain_id, f"Chain {chain_id}")
            provider = "Alchemy" if using_alchemy else "Public RPC"
            print_ok(f"{var_name}: {chain_name} via {provider}")
            return True
            
        except Exception as e:
            self.errors.append(f"{var_name}: Connection failed - {e}")
            return False
    
    def validate_aggregator(self) -> bool:
        """Validate aggregator connectivity."""
        url = self.config.get("AGGREGATOR_URL", "")
        api_key = self.config.get("VALIDATOR_API_KEY") or self.config.get("MINER_API_KEY")
        
        if not url:
            return False
        
        try:
            health_url = f"{url.rstrip('/')}/health"
            req = urllib.request.Request(health_url)
            if api_key:
                req.add_header("X-API-Key", api_key)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
            
            status = result.get("status", "unknown")
            version = result.get("version", "unknown")
            
            if status == "healthy":
                print_ok(f"AGGREGATOR_URL: Healthy (v{version})")
                return True
            elif status == "degraded":
                print_warn(f"AGGREGATOR_URL: Degraded (v{version})")
                self.warnings.append("Aggregator is in degraded state - some solvers may be offline")
                return True
            else:
                self.errors.append(f"AGGREGATOR_URL: Unhealthy status - {status}")
                return False
                
        except Exception as e:
            self.errors.append(f"AGGREGATOR_URL: Connection failed - {e}")
            return False
    
    def validate_docker(self) -> bool:
        """Check Docker availability."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                print_ok("Docker: Running")
                return True
            else:
                self.errors.append("Docker: Not running or not accessible")
                return False
        except FileNotFoundError:
            self.errors.append("Docker: Not installed")
            return False
        except Exception as e:
            self.errors.append(f"Docker: {e}")
            return False
    
    def validate_docker_socket(self) -> bool:
        """Check Docker socket for validator simulation."""
        socket_path = "/var/run/docker.sock"
        if os.path.exists(socket_path):
            if os.access(socket_path, os.R_OK | os.W_OK):
                print_ok("Docker socket: Accessible")
                return True
            else:
                self.errors.append("Docker socket: No read/write access")
                return False
        else:
            self.warnings.append("Docker socket: Not found at /var/run/docker.sock")
            return False
    
    def validate_simulator_image(self) -> bool:
        """Check if simulator image is available."""
        image = self.config.get(
            "SIMULATOR_DOCKER_IMAGE",
            "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"
        )
        
        try:
            result = subprocess.run(
                ["docker", "images", "-q", image],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.stdout.strip():
                print_ok(f"Simulator image: Available ({image})")
                return True
            else:
                self.warnings.append(f"Simulator image not found locally: {image}")
                print_info("Will be pulled automatically on first run")
                return True
        except:
            return False
    
    def detect_role(self) -> str:
        """Detect whether this is a validator or miner config."""
        has_validator_key = bool(self.config.get("VALIDATOR_API_KEY"))
        has_miner_key = bool(self.config.get("MINER_API_KEY"))
        has_miner_id = bool(self.config.get("MINER_ID"))
        
        if has_validator_key and not has_miner_key:
            return "validator"
        elif has_miner_key or has_miner_id:
            return "miner"
        else:
            return "unknown"
    
    def run(self, env_file: Path) -> Tuple[int, int]:
        """Run all validations and return (error_count, warning_count)."""
        print_header(f"Validating: {env_file}")
        
        # Load config
        if not self.load_env_file(env_file):
            return len(self.errors), len(self.warnings)
        
        # Detect role
        role = self.detect_role()
        print_info(f"Detected role: {role}")
        
        # Validate required variables
        print_section("Required Variables")
        if role == "validator":
            self.validate_required(self.VALIDATOR_REQUIRED, role)
            self.validate_recommended(self.VALIDATOR_RECOMMENDED, role)
        elif role == "miner":
            self.validate_required(self.MINER_REQUIRED, role)
            self.validate_recommended(self.MINER_RECOMMENDED, role)
        else:
            print_warn("Could not detect role - checking all variables")
            self.validate_required(self.VALIDATOR_REQUIRED, "validator")
            self.validate_required(self.MINER_REQUIRED, "miner")
        
        for var in (self.VALIDATOR_REQUIRED if role == "validator" else self.MINER_REQUIRED):
            if self.config.get(var) and var not in [e.split()[0] for e in self.errors]:
                print_ok(f"{var}: Set")
        
        # Validate connectivity
        print_section("Connectivity")
        
        # Aggregator
        self.validate_aggregator()
        
        # RPC endpoints
        if self.config.get("ETHEREUM_RPC_URL"):
            self.validate_rpc_url("ETHEREUM_RPC_URL", expected_chain_id=1)
        
        if self.config.get("BASE_RPC_URL"):
            self.validate_rpc_url("BASE_RPC_URL", expected_chain_id=8453)
        
        # Docker (for validator)
        print_section("System Requirements")
        self.validate_docker()
        
        if role == "validator":
            self.validate_docker_socket()
            self.validate_simulator_image()
        
        # Summary
        print_section("Summary")
        
        if self.errors:
            print()
            print(colored("ERRORS:", Colors.RED + Colors.BOLD))
            for error in self.errors:
                print_error(error)
        
        if self.warnings:
            print()
            print(colored("WARNINGS:", Colors.YELLOW + Colors.BOLD))
            for warning in self.warnings:
                print_warn(warning)
        
        if not self.errors:
            print()
            print(colored("✅ Configuration is valid!", Colors.GREEN + Colors.BOLD))
        else:
            print()
            print(colored(f"❌ Configuration has {len(self.errors)} error(s)", Colors.RED + Colors.BOLD))
        
        return len(self.errors), len(self.warnings)


def find_env_files() -> List[Path]:
    """Find .env files in common locations."""
    locations = [
        Path(".") / "validator.env",
        Path(".") / "miner.env",
        Path(".") / ".env",
        Path("/opt/minotaur") / "validator.env",
        Path("/opt/minotaur") / "miner.env",
    ]
    
    return [p for p in locations if p.exists()]


def main():
    parser = argparse.ArgumentParser(
        description="Validate Minotaur configuration files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/validate_config.py                     # Auto-detect and validate
  python scripts/validate_config.py --env-file .env    # Validate specific file
  python scripts/validate_config.py -e validator.env   # Short form
        """
    )
    
    parser.add_argument(
        "--env-file", "-e",
        type=Path,
        help="Path to .env file to validate"
    )
    
    args = parser.parse_args()
    
    if args.env_file:
        files = [args.env_file]
    else:
        files = find_env_files()
        if not files:
            print_error("No .env files found. Specify one with --env-file")
            print_info("Run 'python scripts/setup_wizard.py' to create configuration")
            sys.exit(1)
    
    total_errors = 0
    total_warnings = 0
    
    for env_file in files:
        validator = ConfigValidator()
        errors, warnings = validator.run(env_file)
        total_errors += errors
        total_warnings += warnings
    
    print()
    if total_errors > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
