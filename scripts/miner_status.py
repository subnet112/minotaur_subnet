#!/usr/bin/env python3
"""
Miner Status Dashboard - Real-time monitoring for solvers.

Shows solver registration status, performance metrics, quote success rates,
and RPC health for miner operators.

Usage:
    python scripts/miner_status.py              # One-time status check
    python scripts/miner_status.py --watch      # Live updating dashboard
    python scripts/miner_status.py --json       # JSON output for scripting
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
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
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.END}"
    return text


def clear_screen():
    if sys.stdout.isatty():
        os.system('cls' if os.name == 'nt' else 'clear')


@dataclass
class SolverStatus:
    """Status of a registered solver."""
    solver_id: str
    miner_id: str
    solver_type: str
    endpoint: str
    status: str
    healthy: bool = False
    latency_ms: Optional[float] = None
    supported_tokens: int = 0
    error: Optional[str] = None


@dataclass 
class ContainerStatus:
    """Status of a Docker container."""
    name: str
    status: str
    running: bool
    uptime: str = ""
    port: Optional[int] = None


class AggregatorClient:
    """Client for querying aggregator API."""
    
    def __init__(self, url: str, api_key: Optional[str] = None):
        self.url = url.rstrip('/')
        self.api_key = api_key
    
    def _request(self, endpoint: str, method: str = "GET") -> Optional[dict]:
        """Make an API request."""
        try:
            url = f"{self.url}{endpoint}"
            req = urllib.request.Request(url, method=method)
            req.add_header("Content-Type", "application/json")
            if self.api_key:
                req.add_header("X-API-Key", self.api_key)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode())
        except Exception as e:
            return None
    
    def get_health(self) -> Optional[dict]:
        """Get aggregator health status."""
        return self._request("/health")
    
    def get_solvers(self, miner_id: Optional[str] = None) -> List[dict]:
        """Get registered solvers."""
        endpoint = "/v1/solvers"
        if miner_id:
            endpoint += f"?miner_id={miner_id}"
        result = self._request(endpoint)
        if result and isinstance(result, dict):
            return result.get("solvers", [])
        elif result and isinstance(result, list):
            return result
        return []


class SolverChecker:
    """Check solver health and performance."""
    
    @staticmethod
    def check_solver_health(endpoint: str) -> tuple[bool, Optional[float], Optional[str]]:
        """Check if a solver endpoint is healthy."""
        try:
            health_url = f"{endpoint}/health"
            start = time.time()
            
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=5) as response:
                latency = (time.time() - start) * 1000
                result = json.loads(response.read().decode())
                
            status = result.get("status", "unknown")
            if status == "healthy" or status == "ok":
                return True, round(latency, 1), None
            return False, round(latency, 1), f"Status: {status}"
        except Exception as e:
            return False, None, str(e)
    
    @staticmethod
    def get_supported_tokens(endpoint: str) -> List[str]:
        """Get list of supported tokens from solver."""
        try:
            url = f"{endpoint}/tokens"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode())
            return result.get("tokens", [])
        except:
            return []


class RPCChecker:
    """Check RPC endpoint health."""
    
    @staticmethod
    def check_rpc(url: str) -> tuple[bool, Optional[float], Optional[str]]:
        """Check RPC health and latency."""
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
            
            start = time.time()
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
            latency = (time.time() - start) * 1000
            
            if "error" in result:
                return False, None, result['error'].get('message', 'Unknown error')
            
            chain_id = int(result.get("result", "0x0"), 16)
            using_alchemy = "alchemy.com" in url.lower()
            provider = "Alchemy" if using_alchemy else "Public RPC"
            
            return True, round(latency, 1), provider
        except Exception as e:
            return False, None, str(e)


class DockerInspector:
    """Inspect miner Docker containers."""
    
    @staticmethod
    def get_miner_containers() -> List[ContainerStatus]:
        """Get miner-related containers."""
        containers = []
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode != 0:
                return containers
            
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) < 2:
                    continue
                
                name, status = parts[0], parts[1]
                ports = parts[2] if len(parts) > 2 else ""
                
                # Check if miner container
                if not any(p in name.lower() for p in ["miner", "solver"]):
                    continue
                
                running = "Up" in status
                uptime = status.replace("Up ", "").split(" (")[0] if running else ""
                
                # Extract port
                port = None
                if ":" in ports:
                    try:
                        port = int(ports.split(":")[1].split("->")[0])
                    except:
                        pass
                
                containers.append(ContainerStatus(
                    name=name,
                    status=status,
                    running=running,
                    uptime=uptime,
                    port=port
                ))
        except:
            pass
        return containers
    
    @staticmethod
    def get_container_env(name: str) -> Dict[str, str]:
        """Get environment variables from container."""
        env_vars = {}
        try:
            result = subprocess.run(
                ["docker", "exec", name, "env"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if '=' in line:
                        key, _, value = line.partition('=')
                        if any(k in key for k in ['RPC', 'MINER', 'AGGREGATOR', 'SOLVER']):
                            env_vars[key] = value
        except:
            pass
        return env_vars


class MinerStatusDashboard:
    """Main miner status dashboard."""
    
    def __init__(self):
        self.aggregator_url = os.getenv("AGGREGATOR_URL", "https://aggregator.minotaursubnet.com")
        self.miner_api_key = os.getenv("MINER_API_KEY", "")
        self.miner_id = os.getenv("MINER_ID", "")
        self.eth_rpc = os.getenv("ETHEREUM_RPC_URL", "")
        self.base_rpc = os.getenv("BASE_RPC_URL", "")
        
        self.aggregator = AggregatorClient(self.aggregator_url, self.miner_api_key)
        self.solver_checker = SolverChecker()
        self.rpc_checker = RPCChecker()
        self.docker = DockerInspector()
    
    def gather_status(self) -> Dict[str, Any]:
        """Gather all status information."""
        status = {
            "timestamp": datetime.now().isoformat(),
            "aggregator": {"url": self.aggregator_url, "healthy": False, "status": "unknown"},
            "solvers": [],
            "containers": [],
            "rpc": {},
            "summary": {
                "solvers_registered": 0,
                "solvers_healthy": 0,
                "all_healthy": True
            }
        }
        
        # Check aggregator health
        health = self.aggregator.get_health()
        if health:
            status["aggregator"]["healthy"] = health.get("status") in ["healthy", "degraded"]
            status["aggregator"]["status"] = health.get("status", "unknown")
            status["aggregator"]["version"] = health.get("version", "unknown")
            solver_info = health.get("solvers", {})
            status["aggregator"]["total_solvers"] = solver_info.get("total", 0)
            status["aggregator"]["active_solvers"] = solver_info.get("active", 0)
        
        # Get registered solvers
        solvers = self.aggregator.get_solvers(self.miner_id if self.miner_id else None)
        status["summary"]["solvers_registered"] = len(solvers)
        
        for solver in solvers:
            solver_status = SolverStatus(
                solver_id=solver.get("solver_id", "unknown"),
                miner_id=solver.get("miner_id", "unknown"),
                solver_type=solver.get("solver_type", "unknown"),
                endpoint=solver.get("endpoint", ""),
                status=solver.get("status", "unknown")
            )
            
            # Check solver health
            if solver_status.endpoint and solver_status.status == "active":
                healthy, latency, error = self.solver_checker.check_solver_health(solver_status.endpoint)
                solver_status.healthy = healthy
                solver_status.latency_ms = latency
                solver_status.error = error
                
                if healthy:
                    status["summary"]["solvers_healthy"] += 1
                    tokens = self.solver_checker.get_supported_tokens(solver_status.endpoint)
                    solver_status.supported_tokens = len(tokens)
                else:
                    status["summary"]["all_healthy"] = False
            
            status["solvers"].append({
                "solver_id": solver_status.solver_id,
                "miner_id": solver_status.miner_id,
                "solver_type": solver_status.solver_type,
                "endpoint": solver_status.endpoint,
                "status": solver_status.status,
                "healthy": solver_status.healthy,
                "latency_ms": solver_status.latency_ms,
                "supported_tokens": solver_status.supported_tokens,
                "error": solver_status.error
            })
        
        # Get container status
        containers = self.docker.get_miner_containers()
        for c in containers:
            env = self.docker.get_container_env(c.name) if c.running else {}
            status["containers"].append({
                "name": c.name,
                "status": c.status,
                "running": c.running,
                "uptime": c.uptime,
                "port": c.port,
                "env": env
            })
        
        # Check RPC health
        if self.eth_rpc:
            healthy, latency, info = self.rpc_checker.check_rpc(self.eth_rpc)
            status["rpc"]["ethereum"] = {
                "healthy": healthy,
                "latency_ms": latency,
                "provider": info if healthy else None,
                "error": info if not healthy else None
            }
        
        if self.base_rpc:
            healthy, latency, info = self.rpc_checker.check_rpc(self.base_rpc)
            status["rpc"]["base"] = {
                "healthy": healthy,
                "latency_ms": latency,
                "provider": info if healthy else None,
                "error": info if not healthy else None
            }
        
        return status
    
    def print_dashboard(self, status: Dict[str, Any]):
        """Print the status dashboard."""
        print()
        print(colored("═" * 70, Colors.CYAN))
        print(colored("                    MINER STATUS DASHBOARD", Colors.BOLD + Colors.CYAN))
        print(colored("═" * 70, Colors.CYAN))
        print(colored(f"  Updated: {status['timestamp']}", Colors.DIM))
        print()
        
        # Aggregator status
        agg = status["aggregator"]
        agg_icon = "✅" if agg["healthy"] else "❌"
        print(colored("📡 AGGREGATOR", Colors.BOLD))
        print(colored("─" * 50, Colors.DIM))
        print(f"   {agg_icon} {agg['url']}")
        if agg["healthy"]:
            print(f"      Status: {agg['status']} (v{agg.get('version', '?')})")
            print(f"      Solvers: {agg.get('active_solvers', 0)}/{agg.get('total_solvers', 0)} active")
        else:
            print(colored(f"      Status: {agg['status']}", Colors.RED))
        print()
        
        # Solvers
        solvers = status["solvers"]
        if solvers:
            print(colored(f"📊 REGISTERED SOLVERS ({len(solvers)})", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            print(f"   {'Solver ID':<30} {'Type':<8} {'Status':<12} {'Latency':<10}")
            print(f"   {'-'*30} {'-'*8} {'-'*12} {'-'*10}")
            
            for s in solvers:
                if s["status"] == "active" and s["healthy"]:
                    icon = "✅"
                    status_text = "Active"
                elif s["status"] == "active":
                    icon = "⚠️"
                    status_text = "Unhealthy"
                else:
                    icon = "❌"
                    status_text = s["status"].title()
                
                latency = f"{s['latency_ms']}ms" if s["latency_ms"] else "-"
                solver_id_short = s["solver_id"][:28] + ".." if len(s["solver_id"]) > 30 else s["solver_id"]
                print(f"   {solver_id_short:<30} {s['solver_type']:<8} {icon} {status_text:<10} {latency:<10}")
            print()
        else:
            print(colored("📊 NO SOLVERS REGISTERED", Colors.YELLOW))
            print(colored("─" * 50, Colors.DIM))
            print("   No solvers found. Start a miner to register solvers.")
            print()
        
        # Containers
        containers = status["containers"]
        if containers:
            print(colored(f"🐳 CONTAINERS ({len(containers)})", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            for c in containers:
                icon = "✅" if c["running"] else "❌"
                uptime = f"(uptime: {c['uptime']})" if c["uptime"] else ""
                print(f"   {icon} {c['name']} {uptime}")
            print()
        
        # RPC Health
        rpc = status["rpc"]
        if rpc:
            print(colored("💰 RPC HEALTH", Colors.BOLD))
            print(colored("─" * 50, Colors.DIM))
            for chain, info in rpc.items():
                if info["healthy"]:
                    print(f"   {chain.title()}: ✅ {info['provider']} ({info['latency_ms']}ms)")
                else:
                    print(colored(f"   {chain.title()}: ❌ {info['error']}", Colors.RED))
            print()
        
        # Tips
        print(colored("💡 TIPS", Colors.BOLD))
        print(colored("─" * 50, Colors.DIM))
        print("   • Run 'python scripts/miner_status.py --watch' for live updates")
        print("   • Run 'python scripts/test_solver.py --health' to test solvers")
        print("   • Run 'docker logs -f <container>' for detailed logs")
        print()
    
    def print_json(self, status: Dict[str, Any]):
        """Print status as JSON."""
        print(json.dumps(status, indent=2, default=str))
    
    def run_watch_mode(self, interval: int = 5):
        """Run in watch mode."""
        try:
            while True:
                clear_screen()
                status = self.gather_status()
                self.print_dashboard(status)
                print(colored(f"  Refreshing in {interval} seconds... (Ctrl+C to exit)", Colors.DIM))
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nExiting...")


def main():
    parser = argparse.ArgumentParser(
        description="Miner Status Dashboard - Monitor your solvers",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--watch", "-w", action="store_true", help="Live updating mode")
    parser.add_argument("--interval", "-i", type=int, default=5, help="Refresh interval (default: 5s)")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    args = parser.parse_args()
    
    dashboard = MinerStatusDashboard()
    
    if args.watch:
        dashboard.run_watch_mode(args.interval)
    else:
        status = dashboard.gather_status()
        if args.json:
            dashboard.print_json(status)
        else:
            dashboard.print_dashboard(status)


if __name__ == "__main__":
    main()
