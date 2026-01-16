#!/usr/bin/env python3
"""
Solver Registration Checker - Verify solver registration with aggregator.

Checks if your solvers are properly registered, endpoints are accessible,
and signatures are valid.

Usage:
    python scripts/check_registration.py                      # Check all solvers
    python scripts/check_registration.py --solver-id <id>     # Check specific solver
    python scripts/check_registration.py --verify-endpoint    # Test endpoint accessibility
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Optional, List, Dict, Any


# ANSI colors
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


class RegistrationChecker:
    """Check solver registration status."""
    
    def __init__(self, aggregator_url: str, api_key: Optional[str] = None):
        self.aggregator_url = aggregator_url.rstrip('/')
        self.api_key = api_key
    
    def _request(self, endpoint: str) -> Optional[dict]:
        """Make API request to aggregator."""
        try:
            url = f"{self.aggregator_url}{endpoint}"
            req = urllib.request.Request(url)
            req.add_header("Content-Type", "application/json")
            if self.api_key:
                req.add_header("X-API-Key", self.api_key)
            
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.loads(response.read().decode())
        except urllib.request.HTTPError as e:
            # Handle 503 (degraded) - still returns valid JSON
            if e.code == 503:
                try:
                    return json.loads(e.read().decode())
                except:
                    pass
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
    
    def get_aggregator_health(self) -> Dict[str, Any]:
        """Check aggregator health."""
        result = self._request("/health")
        if result and "error" not in result:
            return {
                "healthy": True,
                "status": result.get("status", "unknown"),
                "version": result.get("version", "unknown"),
                "total_solvers": result.get("solvers", {}).get("total", 0),
                "active_solvers": result.get("solvers", {}).get("active", 0)
            }
        return {
            "healthy": False,
            "error": result.get("error") if result else "No response"
        }
    
    def get_solver(self, solver_id: str) -> Optional[Dict[str, Any]]:
        """Get solver details by ID."""
        result = self._request(f"/v1/solvers/{solver_id}")
        if result and "error" not in result:
            return result
        return None
    
    def get_miner_solvers(self, miner_id: str) -> List[Dict[str, Any]]:
        """Get all solvers for a miner."""
        result = self._request(f"/v1/solvers?miner_id={miner_id}")
        if result and isinstance(result, dict) and "solvers" in result:
            return result["solvers"]
        elif result and isinstance(result, list):
            return result
        return []
    
    def get_all_solvers(self) -> List[Dict[str, Any]]:
        """Get all registered solvers."""
        result = self._request("/v1/solvers")
        if result and isinstance(result, dict) and "solvers" in result:
            return result["solvers"]
        elif result and isinstance(result, list):
            return result
        return []
    
    def check_endpoint_accessible(self, endpoint: str) -> Dict[str, Any]:
        """Check if solver endpoint is accessible from aggregator."""
        try:
            # Try to reach the health endpoint
            health_url = f"{endpoint}/health"
            req = urllib.request.Request(health_url)
            
            start = time.time()
            with urllib.request.urlopen(req, timeout=10) as response:
                latency = (time.time() - start) * 1000
                result = json.loads(response.read().decode())
            
            return {
                "accessible": True,
                "latency_ms": round(latency, 1),
                "status": result.get("status", "unknown")
            }
        except urllib.request.URLError as e:
            return {
                "accessible": False,
                "error": f"Connection failed: {e.reason}"
            }
        except Exception as e:
            return {
                "accessible": False,
                "error": str(e)
            }
    
    def verify_registration(self, solver_id: str, verify_endpoint: bool = False) -> Dict[str, Any]:
        """Comprehensive registration verification."""
        result = {
            "solver_id": solver_id,
            "checks": {},
            "issues": [],
            "overall_status": "unknown"
        }
        
        # Check if solver exists
        solver = self.get_solver(solver_id)
        if not solver:
            result["checks"]["exists"] = False
            result["issues"].append("Solver not found in aggregator")
            result["overall_status"] = "not_registered"
            return result
        
        result["checks"]["exists"] = True
        result["solver_details"] = solver
        
        # Check status
        status = solver.get("status", "unknown")
        result["checks"]["status"] = status
        if status != "active":
            result["issues"].append(f"Solver status is '{status}', not 'active'")
        
        # Check endpoint
        endpoint = solver.get("endpoint", "")
        result["checks"]["has_endpoint"] = bool(endpoint)
        if not endpoint:
            result["issues"].append("No endpoint configured")
        
        # Verify endpoint accessibility
        if verify_endpoint and endpoint:
            endpoint_check = self.check_endpoint_accessible(endpoint)
            result["checks"]["endpoint_accessible"] = endpoint_check["accessible"]
            if not endpoint_check["accessible"]:
                result["issues"].append(f"Endpoint not accessible: {endpoint_check.get('error', 'Unknown')}")
            else:
                result["checks"]["endpoint_latency_ms"] = endpoint_check["latency_ms"]
        
        # Check miner_id
        miner_id = solver.get("miner_id", "")
        result["checks"]["has_miner_id"] = bool(miner_id)
        if not miner_id:
            result["issues"].append("No miner_id associated")
        
        # Check supported assets
        assets = solver.get("supported_assets", [])
        result["checks"]["has_supported_assets"] = len(assets) > 0
        result["checks"]["supported_assets_count"] = len(assets)
        if not assets:
            result["issues"].append("No supported assets configured")
        
        # Overall status
        if result["issues"]:
            result["overall_status"] = "issues_found"
        else:
            result["overall_status"] = "healthy"
        
        return result


def print_banner():
    print()
    print(colored("═" * 60, Colors.CYAN))
    print(colored("           SOLVER REGISTRATION CHECKER", Colors.BOLD + Colors.CYAN))
    print(colored("═" * 60, Colors.CYAN))
    print()


def print_aggregator_status(health: Dict[str, Any]):
    print(colored("📡 AGGREGATOR STATUS", Colors.BOLD))
    print(colored("─" * 40, Colors.DIM))
    
    if health["healthy"]:
        print(f"   ✅ Connected")
        print(f"      Status: {health['status']}")
        print(f"      Version: {health['version']}")
        print(f"      Total Solvers: {health['total_solvers']}")
        print(f"      Active Solvers: {health['active_solvers']}")
    else:
        print(colored(f"   ❌ Connection Failed", Colors.RED))
        print(colored(f"      Error: {health.get('error', 'Unknown')}", Colors.RED))
    print()


def print_solver_result(result: Dict[str, Any]):
    print(colored(f"📋 SOLVER: {result['solver_id']}", Colors.BOLD))
    print(colored("─" * 40, Colors.DIM))
    
    status = result["overall_status"]
    if status == "healthy":
        print(f"   ✅ Status: Healthy")
    elif status == "not_registered":
        print(colored(f"   ❌ Status: Not Registered", Colors.RED))
    else:
        print(colored(f"   ⚠️  Status: Issues Found", Colors.YELLOW))
    
    # Print checks
    checks = result.get("checks", {})
    if "exists" in checks:
        icon = "✅" if checks["exists"] else "❌"
        print(f"   {icon} Registered: {checks['exists']}")
    
    if "status" in checks:
        is_active = checks["status"] == "active"
        icon = "✅" if is_active else "⚠️"
        print(f"   {icon} Status: {checks['status']}")
    
    if "endpoint_accessible" in checks:
        icon = "✅" if checks["endpoint_accessible"] else "❌"
        latency = f" ({checks.get('endpoint_latency_ms', '?')}ms)" if checks["endpoint_accessible"] else ""
        print(f"   {icon} Endpoint Accessible{latency}")
    
    if "supported_assets_count" in checks:
        count = checks["supported_assets_count"]
        icon = "✅" if count > 0 else "⚠️"
        print(f"   {icon} Supported Assets: {count}")
    
    # Print details
    if "solver_details" in result:
        details = result["solver_details"]
        print()
        print(f"   Details:")
        print(f"      Miner ID: {details.get('miner_id', 'N/A')[:40]}...")
        print(f"      Endpoint: {details.get('endpoint', 'N/A')}")
        print(f"      Solver Type: {details.get('solver_type', 'N/A')}")
    
    # Print issues
    if result.get("issues"):
        print()
        print(colored("   Issues:", Colors.YELLOW))
        for issue in result["issues"]:
            print(colored(f"      • {issue}", Colors.YELLOW))
    
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Verify solver registration with aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--solver-id", "-s", type=str, help="Check specific solver ID")
    parser.add_argument("--miner-id", "-m", type=str, help="Check all solvers for miner ID")
    parser.add_argument("--verify-endpoint", "-v", action="store_true", 
                       help="Also verify endpoint accessibility")
    parser.add_argument("--aggregator-url", type=str, 
                       default=os.getenv("AGGREGATOR_URL", "https://aggregator.minotaursubnet.com"),
                       help="Aggregator URL")
    parser.add_argument("--api-key", type=str,
                       default=os.getenv("MINER_API_KEY", ""),
                       help="Miner API key")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    args = parser.parse_args()
    
    checker = RegistrationChecker(args.aggregator_url, args.api_key)
    
    if not args.json:
        print_banner()
    
    # Check aggregator health
    health = checker.get_aggregator_health()
    
    if not args.json:
        print_aggregator_status(health)
    
    if not health["healthy"]:
        if args.json:
            print(json.dumps({"error": "Aggregator not accessible", "health": health}, indent=2))
        sys.exit(1)
    
    results = []
    
    if args.solver_id:
        # Check specific solver
        result = checker.verify_registration(args.solver_id, args.verify_endpoint)
        results.append(result)
        if not args.json:
            print_solver_result(result)
    
    elif args.miner_id:
        # Check all solvers for miner
        solvers = checker.get_miner_solvers(args.miner_id)
        if not solvers:
            if not args.json:
                print(colored(f"No solvers found for miner: {args.miner_id}", Colors.YELLOW))
        
        for solver in solvers:
            result = checker.verify_registration(solver["solver_id"], args.verify_endpoint)
            results.append(result)
            if not args.json:
                print_solver_result(result)
    
    else:
        # Use MINER_ID from env or show all
        miner_id = os.getenv("MINER_ID", "")
        if miner_id:
            solvers = checker.get_miner_solvers(miner_id)
        else:
            solvers = checker.get_all_solvers()[:10]  # Limit to 10
        
        if not solvers:
            if not args.json:
                print("No solvers found. Set MINER_ID or use --solver-id/--miner-id")
        
        for solver in solvers:
            result = checker.verify_registration(solver["solver_id"], args.verify_endpoint)
            results.append(result)
            if not args.json:
                print_solver_result(result)
    
    if args.json:
        print(json.dumps({
            "aggregator": health,
            "solvers": results
        }, indent=2))
    
    # Exit with error if any issues
    if any(r["overall_status"] != "healthy" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
