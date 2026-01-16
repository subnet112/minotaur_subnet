#!/usr/bin/env python3
"""
Minotaur Setup Wizard - Interactive configuration for validators and miners.

This wizard guides users through the setup process, validates configurations,
and generates the necessary .env files for running Minotaur services.

Usage:
    python scripts/setup_wizard.py
    python scripts/setup_wizard.py --output-dir /opt/minotaur
    python scripts/setup_wizard.py --non-interactive --role validator --mode simulation
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# ANSI color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

def colored(text: str, color: str) -> str:
    """Apply color to text if terminal supports it."""
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.END}"
    return text

def print_header(text: str):
    """Print a styled header."""
    print()
    print(colored("═" * 60, Colors.CYAN))
    print(colored(f"  {text}", Colors.BOLD + Colors.CYAN))
    print(colored("═" * 60, Colors.CYAN))
    print()

def print_step(step: int, total: int, text: str):
    """Print a step indicator."""
    print()
    print(colored(f"Step {step}/{total}: {text}", Colors.BOLD + Colors.BLUE))
    print(colored("─" * 40, Colors.BLUE))

def print_success(text: str):
    """Print a success message."""
    print(colored(f"✅ {text}", Colors.GREEN))

def print_warning(text: str):
    """Print a warning message."""
    print(colored(f"⚠️  {text}", Colors.YELLOW))

def print_error(text: str):
    """Print an error message."""
    print(colored(f"❌ {text}", Colors.RED))

def print_info(text: str):
    """Print an info message."""
    print(colored(f"ℹ️  {text}", Colors.CYAN))

def prompt(question: str, default: str = "", password: bool = False) -> str:
    """Prompt user for input with optional default value."""
    if default:
        prompt_text = f"{question} [{default}]: "
    else:
        prompt_text = f"{question}: "
    
    try:
        if password:
            import getpass
            value = getpass.getpass(prompt_text)
        else:
            value = input(prompt_text)
        return value.strip() if value.strip() else default
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

def prompt_choice(question: str, choices: list, default: int = 1) -> int:
    """Prompt user to select from numbered choices."""
    print(f"\n{question}")
    for i, choice in enumerate(choices, 1):
        marker = "→" if i == default else " "
        print(f"  {marker} [{i}] {choice}")
    
    while True:
        try:
            value = input(f"\nEnter choice [1-{len(choices)}] (default: {default}): ").strip()
            if not value:
                return default
            choice = int(value)
            if 1 <= choice <= len(choices):
                return choice
            print_error(f"Please enter a number between 1 and {len(choices)}")
        except ValueError:
            print_error("Please enter a valid number")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer."""
    default_str = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{question} [{default_str}]: ").strip().lower()
            if not value:
                return default
            if value in ('y', 'yes'):
                return True
            if value in ('n', 'no'):
                return False
            print_error("Please enter 'y' or 'n'")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)


class SystemChecker:
    """Check system requirements and dependencies."""
    
    @staticmethod
    def check_docker() -> Tuple[bool, str]:
        """Check if Docker is installed and running."""
        try:
            result = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return False, "Docker not found"
            
            version = result.stdout.strip()
            
            # Check if Docker daemon is running
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return False, "Docker daemon not running"
            
            return True, version
        except FileNotFoundError:
            return False, "Docker not installed"
        except subprocess.TimeoutExpired:
            return False, "Docker command timed out"
        except Exception as e:
            return False, str(e)
    
    @staticmethod
    def check_docker_compose() -> Tuple[bool, str]:
        """Check if Docker Compose is available."""
        # Try docker compose (v2)
        try:
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
        except:
            pass
        
        # Try docker-compose (v1)
        try:
            result = subprocess.run(
                ["docker-compose", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
        except:
            pass
        
        return False, "Docker Compose not found"
    
    @staticmethod
    def test_rpc_url(url: str, expected_chain_id: Optional[int] = None) -> Tuple[bool, str]:
        """Test if an RPC URL is accessible and returns expected chain ID."""
        try:
            import json
            import urllib.request
            
            # Prepare JSON-RPC request
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
                return False, f"RPC error: {result['error'].get('message', 'Unknown error')}"
            
            chain_id = int(result.get("result", "0x0"), 16)
            
            if expected_chain_id and chain_id != expected_chain_id:
                return False, f"Wrong chain ID: expected {expected_chain_id}, got {chain_id}"
            
            chain_names = {1: "Ethereum Mainnet", 8453: "Base"}
            chain_name = chain_names.get(chain_id, f"Chain {chain_id}")
            
            return True, f"{chain_name} (chain ID: {chain_id})"
            
        except Exception as e:
            return False, str(e)
    
    @staticmethod
    def test_aggregator_url(url: str, api_key: Optional[str] = None) -> Tuple[bool, str]:
        """Test if the aggregator URL is accessible."""
        try:
            import json
            import urllib.request
            
            health_url = f"{url.rstrip('/')}/health"
            req = urllib.request.Request(health_url)
            
            if api_key:
                req.add_header("X-API-Key", api_key)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
                
            status = result.get("status", "unknown")
            version = result.get("version", "unknown")
            
            if status == "healthy":
                return True, f"Healthy (v{version})"
            elif status == "degraded":
                return True, f"Degraded (v{version}) - some solvers may be offline"
            else:
                return False, f"Status: {status}"
                
        except Exception as e:
            return False, str(e)
    
    @staticmethod
    def pull_docker_image(image: str) -> Tuple[bool, str]:
        """Pull a Docker image."""
        try:
            print_info(f"Pulling {image}...")
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=600  # 10 minutes
            )
            if result.returncode == 0:
                return True, "Image pulled successfully"
            return False, result.stderr
        except subprocess.TimeoutExpired:
            return False, "Pull timed out"
        except Exception as e:
            return False, str(e)


class ConfigGenerator:
    """Generate configuration files for Minotaur services."""
    
    VALIDATOR_TEMPLATE = """# Minotaur Validator Configuration
# Generated by setup wizard on {timestamp}

# ============================================
# Validator Mode
# ============================================
VALIDATOR_MODE={validator_mode}

# ============================================
# Aggregator API
# ============================================
AGGREGATOR_URL={aggregator_url}
VALIDATOR_API_KEY={validator_api_key}

# ============================================
# RPC Configuration (for order simulation)
# ============================================
ETHEREUM_RPC_URL={ethereum_rpc_url}
BASE_RPC_URL={base_rpc_url}

# ============================================
# Simulator Settings
# ============================================
SIMULATOR_MAX_CONCURRENT={simulator_max_concurrent}
SIMULATOR_TIMEOUT_SECONDS={simulator_timeout}
SIMULATOR_AUTO_PULL=true

# ============================================
# Epoch Configuration
# ============================================
VALIDATOR_EPOCH_MINUTES=5
VALIDATOR_CONTINUOUS=true
VALIDATOR_POLL_SECONDS=12

# ============================================
# Bittensor Configuration (production only)
# ============================================
{bittensor_config}

# ============================================
# Logging
# ============================================
LOGURU_LEVEL={log_level}
"""

    MINER_TEMPLATE = """# Minotaur Miner Configuration
# Generated by setup wizard on {timestamp}

# ============================================
# Miner Mode
# ============================================
MINER_MODE={miner_mode}
MINER_ID={miner_id}

# ============================================
# Aggregator API
# ============================================
AGGREGATOR_URL={aggregator_url}
MINER_API_KEY={miner_api_key}

# ============================================
# Solver Configuration
# ============================================
MINER_SOLVER_HOST={solver_host}
MINER_BASE_PORT=8000
MINER_NUM_SOLVERS=1

# ============================================
# RPC Configuration (for price quotes)
# ============================================
ETHEREUM_RPC_URL={ethereum_rpc_url}
BASE_RPC_URL={base_rpc_url}

# ============================================
# Bittensor Configuration (production only)
# ============================================
{bittensor_config}

# ============================================
# Logging
# ============================================
LOGURU_LEVEL={log_level}
"""

    @staticmethod
    def generate_validator_config(config: Dict[str, Any]) -> str:
        """Generate validator .env configuration."""
        from datetime import datetime
        
        bittensor_config = ""
        if config.get("mode") == "bittensor":
            bittensor_config = f"""NETUID={config.get('netuid', 112)}
WALLET_NAME={config.get('wallet_name', 'validator')}
WALLET_HOTKEY={config.get('wallet_hotkey', 'default')}
SUBTENSOR_NETWORK={config.get('subtensor_network', 'finney')}"""
        else:
            bittensor_config = "# Bittensor disabled in simulation mode"
        
        return ConfigGenerator.VALIDATOR_TEMPLATE.format(
            timestamp=datetime.now().isoformat(),
            validator_mode="mock" if config.get("mode") == "simulation" else "bittensor",
            aggregator_url=config.get("aggregator_url", "https://aggregator.minotaursubnet.com"),
            validator_api_key=config.get("validator_api_key", ""),
            ethereum_rpc_url=config.get("ethereum_rpc_url", ""),
            base_rpc_url=config.get("base_rpc_url", ""),
            simulator_max_concurrent=config.get("simulator_max_concurrent", 5),
            simulator_timeout=config.get("simulator_timeout", 300),
            bittensor_config=bittensor_config,
            log_level=config.get("log_level", "INFO"),
        )
    
    @staticmethod
    def generate_miner_config(config: Dict[str, Any]) -> str:
        """Generate miner .env configuration."""
        from datetime import datetime
        
        bittensor_config = ""
        if config.get("mode") == "bittensor":
            bittensor_config = f"""NETUID={config.get('netuid', 112)}
WALLET_NAME={config.get('wallet_name', 'miner')}
WALLET_HOTKEY={config.get('wallet_hotkey', 'default')}
SUBTENSOR_NETWORK={config.get('subtensor_network', 'finney')}"""
        else:
            bittensor_config = "# Bittensor disabled in simulation mode"
        
        return ConfigGenerator.MINER_TEMPLATE.format(
            timestamp=datetime.now().isoformat(),
            miner_mode="simulation" if config.get("mode") == "simulation" else "bittensor",
            miner_id=config.get("miner_id", "miner-001"),
            aggregator_url=config.get("aggregator_url", "https://aggregator.minotaursubnet.com"),
            miner_api_key=config.get("miner_api_key", ""),
            solver_host=config.get("solver_host", "localhost"),
            ethereum_rpc_url=config.get("ethereum_rpc_url", ""),
            base_rpc_url=config.get("base_rpc_url", ""),
            bittensor_config=bittensor_config,
            log_level=config.get("log_level", "INFO"),
        )


class SetupWizard:
    """Interactive setup wizard for Minotaur."""
    
    def __init__(self, output_dir: str = "."):
        self.output_dir = Path(output_dir)
        self.config: Dict[str, Any] = {}
        self.checker = SystemChecker()
    
    def run(self):
        """Run the interactive setup wizard."""
        print_header("🧙 MINOTAUR SETUP WIZARD")
        print("Welcome! This wizard will help you configure Minotaur for")
        print("validating and/or mining on the swap intent subnet.")
        print()
        print(colored("Press Ctrl+C at any time to exit.", Colors.YELLOW))
        
        # Step 1: Check system requirements
        self._check_system_requirements()
        
        # Step 2: Choose role
        self._choose_role()
        
        # Step 3: Choose mode
        self._choose_mode()
        
        # Step 4: Configure RPC
        self._configure_rpc()
        
        # Step 5: Configure aggregator
        self._configure_aggregator()
        
        # Step 6: Configure Bittensor (if production mode)
        if self.config.get("mode") == "bittensor":
            self._configure_bittensor()
        
        # Step 7: Generate configuration
        self._generate_configuration()
        
        # Step 8: Offer to start services
        self._offer_start_services()
        
        print_header("🎉 SETUP COMPLETE")
        print("Your Minotaur configuration has been created successfully!")
        print()
        self._print_next_steps()
    
    def _check_system_requirements(self):
        """Check system requirements."""
        print_step(1, 7, "Checking System Requirements")
        
        all_ok = True
        
        # Check Docker
        docker_ok, docker_msg = self.checker.check_docker()
        if docker_ok:
            print_success(f"Docker: {docker_msg}")
        else:
            print_error(f"Docker: {docker_msg}")
            all_ok = False
        
        # Check Docker Compose
        compose_ok, compose_msg = self.checker.check_docker_compose()
        if compose_ok:
            print_success(f"Docker Compose: {compose_msg}")
        else:
            print_warning(f"Docker Compose: {compose_msg} (optional but recommended)")
        
        # Check Python version
        python_version = sys.version.split()[0]
        if sys.version_info >= (3, 10):
            print_success(f"Python: {python_version}")
        else:
            print_warning(f"Python: {python_version} (3.10+ recommended)")
        
        if not all_ok:
            print()
            print_error("Some requirements are not met. Please install Docker before continuing.")
            print_info("Install Docker: https://docs.docker.com/get-docker/")
            if not prompt_yes_no("Continue anyway?", default=False):
                sys.exit(1)
    
    def _choose_role(self):
        """Choose validator or miner role."""
        print_step(2, 7, "Choose Your Role")
        
        print("What would you like to run?")
        print()
        print(colored("Validator:", Colors.BOLD), "Validates swap orders and earns TAO rewards")
        print("           Best for: Users with reliable servers and stable connections")
        print()
        print(colored("Miner:", Colors.BOLD), "Provides swap quotes and earns TAO rewards")
        print("        Best for: Users who want to optimize swap execution")
        print()
        print(colored("Both:", Colors.BOLD), "Run validator and miner on the same machine")
        print("       Best for: Testing or maximizing participation")
        
        choice = prompt_choice("Select role:", [
            "Validator only",
            "Miner only", 
            "Both (Validator + Miner)"
        ], default=1)
        
        roles = {1: "validator", 2: "miner", 3: "both"}
        self.config["role"] = roles[choice]
        print_success(f"Selected: {roles[choice].title()}")
    
    def _choose_mode(self):
        """Choose simulation or production mode."""
        print_step(3, 7, "Choose Mode")
        
        print("Which mode would you like to run in?")
        print()
        print(colored("Simulation:", Colors.BOLD), "Test with the real aggregator but WITHOUT Bittensor")
        print("             No TAO required, no real rewards, great for learning")
        print()
        print(colored("Production:", Colors.BOLD), "Full Bittensor integration on mainnet (finney)")
        print("             Requires registered wallet, earns real TAO rewards")
        
        choice = prompt_choice("Select mode:", [
            "Simulation (recommended for beginners)",
            "Production (Bittensor mainnet)"
        ], default=1)
        
        self.config["mode"] = "simulation" if choice == 1 else "bittensor"
        print_success(f"Selected: {self.config['mode'].title()} mode")
    
    def _configure_rpc(self):
        """Configure RPC endpoints."""
        print_step(4, 7, "Configure RPC Endpoints")
        
        print("Minotaur needs access to Ethereum and Base RPC endpoints")
        print("for simulating/quoting swap orders.")
        print()
        print("Options:")
        print("  • Alchemy (recommended): Free tier available at https://alchemy.com")
        print("  • Infura: https://infura.io")
        print("  • Public RPCs: Free but may be rate-limited")
        print()
        
        use_alchemy = prompt_yes_no("Do you have an Alchemy API key?", default=True)
        
        if use_alchemy:
            alchemy_key = prompt("Enter your Alchemy API key", password=True)
            if alchemy_key:
                eth_rpc = f"https://eth-mainnet.g.alchemy.com/v2/{alchemy_key}"
                base_rpc = f"https://base-mainnet.g.alchemy.com/v2/{alchemy_key}"
                
                # Test Ethereum RPC
                print_info("Testing Ethereum RPC...")
                eth_ok, eth_msg = self.checker.test_rpc_url(eth_rpc, expected_chain_id=1)
                if eth_ok:
                    print_success(f"Ethereum RPC: {eth_msg}")
                else:
                    print_error(f"Ethereum RPC failed: {eth_msg}")
                    eth_rpc = None
                
                # Test Base RPC
                print_info("Testing Base RPC...")
                base_ok, base_msg = self.checker.test_rpc_url(base_rpc, expected_chain_id=8453)
                if base_ok:
                    print_success(f"Base RPC: {base_msg}")
                else:
                    print_error(f"Base RPC failed: {base_msg}")
                    base_rpc = None
                
                if eth_rpc and base_rpc:
                    self.config["ethereum_rpc_url"] = eth_rpc
                    self.config["base_rpc_url"] = base_rpc
                    self.config["alchemy_key"] = alchemy_key
                    return
        
        # Manual RPC configuration
        print()
        print_info("Enter your RPC URLs manually:")
        
        # Ethereum RPC
        while True:
            eth_rpc = prompt("Ethereum RPC URL", default="https://cloudflare-eth.com")
            print_info("Testing connection...")
            ok, msg = self.checker.test_rpc_url(eth_rpc, expected_chain_id=1)
            if ok:
                print_success(f"Connected: {msg}")
                self.config["ethereum_rpc_url"] = eth_rpc
                break
            else:
                print_error(f"Failed: {msg}")
                if not prompt_yes_no("Try again?"):
                    self.config["ethereum_rpc_url"] = eth_rpc
                    break
        
        # Base RPC
        while True:
            base_rpc = prompt("Base RPC URL", default="https://mainnet.base.org")
            print_info("Testing connection...")
            ok, msg = self.checker.test_rpc_url(base_rpc, expected_chain_id=8453)
            if ok:
                print_success(f"Connected: {msg}")
                self.config["base_rpc_url"] = base_rpc
                break
            else:
                print_error(f"Failed: {msg}")
                if not prompt_yes_no("Try again?"):
                    self.config["base_rpc_url"] = base_rpc
                    break
    
    def _configure_aggregator(self):
        """Configure aggregator connection."""
        print_step(5, 7, "Configure Aggregator")
        
        print("The aggregator coordinates swap orders between validators and miners.")
        print()
        
        aggregator_url = prompt(
            "Aggregator URL",
            default="https://aggregator.minotaursubnet.com"
        )
        self.config["aggregator_url"] = aggregator_url
        
        # Test aggregator connection
        print_info("Testing aggregator connection...")
        ok, msg = self.checker.test_aggregator_url(aggregator_url)
        if ok:
            print_success(f"Aggregator: {msg}")
        else:
            print_warning(f"Aggregator: {msg}")
        
        # Get API keys based on role
        role = self.config.get("role", "validator")
        
        if role in ("validator", "both"):
            print()
            print_info("Validator API key is required for validator endpoints")
            validator_key = prompt("Validator API key", password=True)
            self.config["validator_api_key"] = validator_key
        
        if role in ("miner", "both"):
            print()
            print_info("Miner API key is required for miner endpoints")
            miner_key = prompt("Miner API key", password=True)
            self.config["miner_api_key"] = miner_key
            
            # Miner ID
            miner_id = prompt("Miner ID (unique identifier)", default="miner-001")
            self.config["miner_id"] = miner_id
            
            # Solver host
            print()
            print_info("Solver host is the IP/hostname that the aggregator will use to reach your solver")
            solver_host = prompt("Solver host", default="localhost")
            self.config["solver_host"] = solver_host
    
    def _configure_bittensor(self):
        """Configure Bittensor settings for production mode."""
        print_step(6, 7, "Configure Bittensor")
        
        print("For production mode, you need a registered Bittensor wallet.")
        print()
        print_info("Make sure your wallet is registered on subnet 112 (Minotaur)")
        print()
        
        self.config["netuid"] = 112
        self.config["subtensor_network"] = "finney"
        
        wallet_name = prompt("Wallet name", default="default")
        self.config["wallet_name"] = wallet_name
        
        wallet_hotkey = prompt("Wallet hotkey", default="default")
        self.config["wallet_hotkey"] = wallet_hotkey
        
        print()
        print_warning("Make sure your wallet has sufficient TAO for registration")
        print_info("Check balance: btcli w balance --wallet.name " + wallet_name)
    
    def _generate_configuration(self):
        """Generate configuration files."""
        print_step(7, 7, "Generating Configuration")
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        role = self.config.get("role", "validator")
        generator = ConfigGenerator()
        
        if role in ("validator", "both"):
            validator_config = generator.generate_validator_config(self.config)
            validator_path = self.output_dir / "validator.env"
            validator_path.write_text(validator_config)
            print_success(f"Created: {validator_path}")
        
        if role in ("miner", "both"):
            miner_config = generator.generate_miner_config(self.config)
            miner_path = self.output_dir / "miner.env"
            miner_path.write_text(miner_config)
            print_success(f"Created: {miner_path}")
        
        # Create docker-compose.yml if role is "both"
        if role == "both":
            compose_path = self.output_dir / "docker-compose.yml"
            compose_content = self._generate_docker_compose()
            compose_path.write_text(compose_content)
            print_success(f"Created: {compose_path}")
    
    def _generate_docker_compose(self) -> str:
        """Generate docker-compose.yml for running both validator and miner."""
        return f"""# Minotaur Docker Compose Configuration
# Generated by setup wizard
# Usage: docker compose up -d

version: '3.8'

services:
  validator:
    image: ghcr.io/subnet112/minotaur_subnet:latest
    container_name: minotaur-validator
    command: validator
    env_file: validator.env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    network_mode: host
    restart: unless-stopped

  miner-v3:
    image: ghcr.io/subnet112/minotaur_subnet:latest
    container_name: miner-v3
    command: miner
    env_file: miner.env
    environment:
      - MINER_SOLVER_TYPE=v3
      - MINER_BASE_PORT=8000
      - MINER_ID=miner-mainnet-v3
    network_mode: host
    restart: unless-stopped

  miner-v2:
    image: ghcr.io/subnet112/minotaur_subnet:latest
    container_name: miner-v2
    command: miner
    env_file: miner.env
    environment:
      - MINER_SOLVER_TYPE=v2
      - MINER_BASE_PORT=8001
      - MINER_ID=miner-mainnet-v2
    network_mode: host
    restart: unless-stopped

  miner-base:
    image: ghcr.io/subnet112/minotaur_subnet:latest
    container_name: miner-base
    command: miner
    env_file: miner.env
    environment:
      - MINER_SOLVER_TYPE=base
      - MINER_BASE_PORT=8002
      - MINER_ID=miner-base-v3
    network_mode: host
    restart: unless-stopped
"""
    
    def _offer_start_services(self):
        """Offer to start the services."""
        print()
        if prompt_yes_no("Would you like to pull the Docker images now?", default=True):
            images = [
                "ghcr.io/subnet112/minotaur_subnet:latest",
                "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"
            ]
            for image in images:
                ok, msg = self.checker.pull_docker_image(image)
                if ok:
                    print_success(f"Pulled: {image}")
                else:
                    print_error(f"Failed to pull {image}: {msg}")
    
    def _print_next_steps(self):
        """Print next steps for the user."""
        role = self.config.get("role", "validator")
        output_dir = self.output_dir
        
        print("Next steps:")
        print()
        
        if role == "both":
            print(colored("Start all services:", Colors.BOLD))
            print(f"  cd {output_dir}")
            print("  docker compose up -d")
            print()
            print(colored("View logs:", Colors.BOLD))
            print("  docker compose logs -f")
        else:
            if role == "validator":
                print(colored("Start validator:", Colors.BOLD))
                print(f"  docker run -d --name minotaur-validator \\")
                print(f"    --network host \\")
                print(f"    --env-file {output_dir}/validator.env \\")
                print(f"    -v /var/run/docker.sock:/var/run/docker.sock \\")
                print(f"    ghcr.io/subnet112/minotaur_subnet:latest validator")
            else:
                print(colored("Start miner:", Colors.BOLD))
                print(f"  docker run -d --name minotaur-miner \\")
                print(f"    --network host \\")
                print(f"    --env-file {output_dir}/miner.env \\")
                print(f"    ghcr.io/subnet112/minotaur_subnet:latest miner")
            print()
            print(colored("View logs:", Colors.BOLD))
            print(f"  docker logs -f minotaur-{role}")
        
        print()
        print(colored("Check status:", Colors.BOLD))
        print("  python scripts/status.py")
        print()
        print(colored("Documentation:", Colors.BOLD))
        print("  https://github.com/subnet112/minotaur_subnet/tree/main/docs")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Minotaur Setup Wizard - Interactive configuration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/setup_wizard.py                          # Interactive mode
  python scripts/setup_wizard.py --output-dir /opt/mino   # Custom output directory
        """
    )
    
    parser.add_argument(
        "--output-dir", "-o",
        default=".",
        help="Directory to save configuration files (default: current directory)"
    )
    
    args = parser.parse_args()
    
    try:
        wizard = SetupWizard(output_dir=args.output_dir)
        wizard.run()
    except KeyboardInterrupt:
        print()
        print_warning("Setup cancelled by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
