#!/usr/bin/env python3
"""
Minotaur Status Dashboard - Real-time monitoring for validators and miners.

This script provides a comprehensive view of your Minotaur services status,
including container health, RPC connectivity, and performance metrics.

Usage:
    python scripts/status.py              # One-time status check
    python scripts/status.py --watch      # Live updating dashboard
    python scripts/status.py --json       # JSON output for scripting
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any


# ANSI color codes
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'


def colored(text: str, color: str) -> str:
    """Apply color to text if terminal supports it."""
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.END}"
    return text


def clear_screen():
    """Clear the terminal screen."""
    if sys.stdout.isatty():
        os.system('cls' if os.name == 'nt' else 'clear')


@dataclass
class ContainerStatus:
    """Status of a Docker container."""
    name: str
    status: str
    running: bool
    uptime: str = ""
    image: str = ""
    health: str = "unknown"
    env_vars: Dict[str, str] = field(default_factory=dict)


@dataclass
class RPCStatus:
    """Status of an RPC endpoint."""
    name: str
    url: str
    chain_id: Optional[int] = None
    latency_ms: Optional[float] = None
    healthy: bool = False
    error: Optional[str] = None
    using_alchemy: bool = False


@dataclass
class ServiceMetrics:
    """Metrics for a service."""
    epochs_completed: int = 0
    orders_validated: int = 0
    quotes_provided: int = 0
    errors: int = 0
    last_activity: Optional[datetime] = None


class DockerInspector:
    """Inspect Docker containers for Minotaur services."""
    
    CONTAINER_PATTERNS = [
        "minotaur-validator",
        "minotaur-miner",
        "miner-v3",
        "miner-v2", 
        "miner-base",
        "mino-simulation"
    ]
    
    @staticmethod
    def get_containers() -> List[ContainerStatus]:
        """Get status of all Minotaur-related containers."""
        containers = []
        
        try:
            # Get all containers matching our patterns
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return containers
            
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                    
                parts = line.split('\t')
                if len(parts) < 3:
                    continue
                    
                name, status, image = parts[0], parts[1], parts[2]
                
                # Check if this is a Minotaur container
                is_minotaur = any(
                    pattern in name.lower() 
                    for pattern in ["minotaur", "miner-v", "miner-base", "mino-simulation"]
                )
                
                if not is_minotaur:
                    continue
                
                running = "Up" in status
                uptime = ""
                if running:
                    # Extract uptime from status like "Up 2 hours"
                    uptime = status.replace("Up ", "").split(" (")[0]
                
                container = ContainerStatus(
                    name=name,
                    status=status,
                    running=running,
                    uptime=uptime,
                    image=image
                )
                
                # Get environment variables
                if running:
                    container.env_vars = DockerInspector._get_container_env(name)
                
                containers.append(container)
                
        except Exception as e:
            pass
        
        return containers
    
    @staticmethod
    def _get_container_env(container_name: str) -> Dict[str, str]:
        """Get environment variables from a running container."""
        env_vars = {}
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "env"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if '=' in line:
                        key, _, value = line.partition('=')
                        # Only capture relevant vars
                        if any(k in key for k in ['RPC', 'ALCHEMY', 'AGGREGATOR', 'MINER', 'VALIDATOR', 'SIMULATOR']):
                            env_vars[key] = value
        except:
            pass
        
        return env_vars
    
    @staticmethod
    def get_container_logs(container_name: str, lines: int = 50) -> List[str]:
        """Get recent logs from a container."""
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(lines), container_name],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.split('\n') + result.stderr.split('\n')
        except:
            return []


class RPCChecker:
    """Check RPC endpoint health."""
    
    @staticmethod
    def check_rpc(url: str, expected_chain_id: Optional[int] = None) -> RPCStatus:
        """Check an RPC endpoint's health and latency."""
        status = RPCStatus(
            name="",
            url=url,
            using_alchemy="alchemy.com" in url.lower()
        )
        
        # Determine chain name from URL
        if "base" in url.lower():
            status.name = "Base"
        elif "eth" in url.lower() or "mainnet" in url.lower():
            status.name = "Ethereum"
        else:
            status.name = "RPC"
        
        try:
            import urllib.request
            import time
            
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
            
            start_time = time.time()
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
            latency = (time.time() - start_time) * 1000
            
            if "error" in result:
                status.error = result['error'].get('message', 'Unknown error')
                return status
            
            status.chain_id = int(result.get("result", "0x0"), 16)
            status.latency_ms = round(latency, 1)
            status.healthy = True
            
            if expected_chain_id and status.chain_id != expected_chain_id:
                status.error = f"Wrong chain: expected {expected_chain_id}, got {status.chain_id}"
                status.healthy = False
                
        except Exception as e:
            status.error = str(e)
        
        return status


class LogAnalyzer:
    """Analyze container logs for metrics."""
    
    @staticmethod
    def analyze_validator_logs(logs: List[str]) -> ServiceMetrics:
        """Analyze validator logs for metrics."""
        metrics = ServiceMetrics()
        
        for line in logs:
            if "Epoch collection complete" in line:
                metrics.epochs_completed += 1
            if "validated" in line.lower() and "order" in line.lower():
                metrics.orders_validated += 1
            if "ERROR" in line or "error" in line:
                metrics.errors += 1
        
        return metrics
    
    @staticmethod
    def analyze_miner_logs(logs: List[str]) -> ServiceMetrics:
        """Analyze miner logs for metrics."""
        metrics = ServiceMetrics()
        
        for line in logs:
            if "quote" in line.lower() and ("provided" in line.lower() or "200" in line):
                metrics.quotes_provided += 1
            if "ERROR" in line or "error" in line:
                metrics.errors += 1
        
        return metrics


class StatusDashboard:
    """Main status dashboard."""
    
    def __init__(self):
        self.docker = DockerInspector()
        self.rpc_checker = RPCChecker()
        self.log_analyzer = LogAnalyzer()
    
    def gather_status(self) -> Dict[str, Any]:
        """Gather all status information."""
        status = {
            "timestamp": datetime.now().isoformat(),
            "containers": [],
            "rpc_endpoints": [],
            "aggregator": None,
            "summary": {
                "validators_running": 0,
                "miners_running": 0,
                "simulation_containers": 0,
                "all_healthy": True
            }
        }
        
        # Get container status
        containers = self.docker.get_containers()
        
        for container in containers:
            container_info = {
                "name": container.name,
                "status": container.status,
                "running": container.running,
                "uptime": container.uptime,
                "image": container.image,
                "rpc_health": {}
            }
            
            # Check RPC endpoints from container env
            if container.running:
                eth_rpc = container.env_vars.get("ETHEREUM_RPC_URL")
                base_rpc = container.env_vars.get("BASE_RPC_URL")
                
                if eth_rpc:
                    rpc_status = self.rpc_checker.check_rpc(eth_rpc, expected_chain_id=1)
                    container_info["rpc_health"]["ethereum"] = {
                        "healthy": rpc_status.healthy,
                        "latency_ms": rpc_status.latency_ms,
                        "using_alchemy": rpc_status.using_alchemy,
                        "error": rpc_status.error
                    }
                    if not rpc_status.healthy:
                        status["summary"]["all_healthy"] = False
                
                if base_rpc:
                    rpc_status = self.rpc_checker.check_rpc(base_rpc, expected_chain_id=8453)
                    container_info["rpc_health"]["base"] = {
                        "healthy": rpc_status.healthy,
                        "latency_ms": rpc_status.latency_ms,
                        "using_alchemy": rpc_status.using_alchemy,
                        "error": rpc_status.error
                    }
                    if not rpc_status.healthy:
                        status["summary"]["all_healthy"] = False
            
            # Update summary counts
            if "validator" in container.name.lower() and container.running:
                status["summary"]["validators_running"] += 1
            elif "miner" in container.name.lower() and "simulation" not in container.name.lower() and container.running:
                status["summary"]["miners_running"] += 1
            elif "simulation" in container.name.lower() and container.running:
                status["summary"]["simulation_containers"] += 1
            
            status["containers"].append(container_info)
        
        return status
    
    def print_dashboard(self, status: Dict[str, Any]):
        """Print the status dashboard."""
        print()
        print(colored("═" * 70, Colors.CYAN))
        print(colored("                    MINOTAUR STATUS DASHBOARD", Colors.BOLD + Colors.CYAN))
        print(colored("═" * 70, Colors.CYAN))
        print(colored(f"  Updated: {status['timestamp']}", Colors.DIM))
        print()
        
        # Summary
        summary = status["summary"]
        health_icon = "✅" if summary["all_healthy"] else "⚠️"
        print(colored(f"  {health_icon} Overall Status: ", Colors.BOLD), end="")
        if summary["all_healthy"]:
            print(colored("Healthy", Colors.GREEN))
        else:
            print(colored("Issues Detected", Colors.YELLOW))
        print()
        
        # Validators section
        validators = [c for c in status["containers"] if "validator" in c["name"].lower() and "simulation" not in c["name"].lower()]
        if validators:
            print(colored("📊 VALIDATORS", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            for v in validators:
                status_icon = "✅" if v["running"] else "❌"
                print(f"   {status_icon} {v['name']}")
                if v["running"]:
                    print(f"      Status: Running (uptime: {v['uptime']})")
                    if v.get("rpc_health"):
                        for chain, health in v["rpc_health"].items():
                            if health["healthy"]:
                                provider = "Alchemy" if health["using_alchemy"] else "Public RPC"
                                print(f"      {chain.title()} RPC: ✅ {provider} ({health['latency_ms']}ms)")
                            else:
                                print(colored(f"      {chain.title()} RPC: ❌ {health.get('error', 'Unknown error')}", Colors.RED))
                else:
                    print(colored(f"      Status: {v['status']}", Colors.RED))
            print()
        
        # Miners section
        miners = [c for c in status["containers"] if "miner" in c["name"].lower() and "simulation" not in c["name"].lower()]
        if miners:
            print(colored("📊 MINERS", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            print(f"   {'Name':<20} {'Status':<12} {'RPC Health':<20}")
            print(f"   {'-'*20} {'-'*12} {'-'*20}")
            for m in miners:
                status_icon = "✅" if m["running"] else "❌"
                status_text = "Running" if m["running"] else "Stopped"
                
                rpc_text = ""
                if m["running"] and m.get("rpc_health"):
                    eth_health = m["rpc_health"].get("ethereum", {})
                    if eth_health.get("healthy"):
                        provider = "Alchemy" if eth_health.get("using_alchemy") else "Public"
                        rpc_text = f"✅ {provider}"
                    else:
                        rpc_text = "❌ Error"
                
                print(f"   {m['name']:<20} {status_icon} {status_text:<10} {rpc_text}")
            print()
        
        # Simulation containers
        sim_containers = [c for c in status["containers"] if "simulation" in c["name"].lower()]
        if sim_containers:
            running = sum(1 for c in sim_containers if c["running"])
            print(colored("📊 SIMULATION CONTAINERS", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            print(f"   Running: {running}/{len(sim_containers)}")
            print()
        
        # Tips
        print(colored("💡 TIPS", Colors.BOLD))
        print(colored("─" * 50, Colors.DIM))
        print("   • Run 'python scripts/status.py --watch' for live updates")
        print("   • Run 'docker logs -f <container>' for detailed logs")
        print("   • Run 'python scripts/setup_wizard.py' to reconfigure")
        print()
    
    def print_json(self, status: Dict[str, Any]):
        """Print status as JSON."""
        print(json.dumps(status, indent=2, default=str))
    
    def run_watch_mode(self, interval: int = 5):
        """Run in watch mode with periodic updates."""
        try:
            while True:
                clear_screen()
                status = self.gather_status()
                self.print_dashboard(status)
                print(colored(f"  Refreshing in {interval} seconds... (Ctrl+C to exit)", Colors.DIM))
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
            print("Exiting...")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Minotaur Status Dashboard - Monitor your validator and miner services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/status.py              # One-time status check
  python scripts/status.py --watch      # Live updating dashboard (5s refresh)
  python scripts/status.py --watch -i 2 # Live updates every 2 seconds
  python scripts/status.py --json       # JSON output for scripting
        """
    )
    
    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        help="Enable watch mode with periodic updates"
    )
    
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=5,
        help="Refresh interval in seconds for watch mode (default: 5)"
    )
    
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output status as JSON"
    )
    
    args = parser.parse_args()
    
    dashboard = StatusDashboard()
    
    if args.watch:
        dashboard.run_watch_mode(interval=args.interval)
    else:
        status = dashboard.gather_status()
        if args.json:
            dashboard.print_json(status)
        else:
            dashboard.print_dashboard(status)


if __name__ == "__main__":
    main()
