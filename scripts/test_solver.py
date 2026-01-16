#!/usr/bin/env python3
"""
Solver Quote Tester - Test solver quote generation.

Test your solver's ability to generate quotes for specific token pairs
before going live. Useful for debugging and verifying configuration.

Usage:
    python scripts/test_solver.py --health                    # Check solver health
    python scripts/test_solver.py --tokens                    # List supported tokens
    python scripts/test_solver.py --quote USDC WETH 1000      # Test a quote
    python scripts/test_solver.py --benchmark                 # Run performance test
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Any


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


# Common token addresses
KNOWN_TOKENS = {
    # Ethereum Mainnet
    "ETH": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI": "0x6B175474E89094C44Da98b954EesfdCf3e48Bfd5",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    # Base
    "USDC_BASE": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "WETH_BASE": "0x4200000000000000000000000000000000000006",
}


class SolverTester:
    """Test solver quote generation."""
    
    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip('/')
    
    def _request(self, path: str, data: dict = None, method: str = "GET") -> tuple[Optional[dict], float]:
        """Make request to solver and return response + latency."""
        url = f"{self.endpoint}{path}"
        start = time.time()
        
        try:
            if data:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json"},
                    method=method
                )
            else:
                req = urllib.request.Request(url, method=method)
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode())
            
            latency = (time.time() - start) * 1000
            return result, latency
        except Exception as e:
            latency = (time.time() - start) * 1000
            return {"error": str(e)}, latency
    
    def check_health(self) -> dict:
        """Check solver health."""
        result, latency = self._request("/health")
        return {
            "healthy": "error" not in result and result.get("status") in ["healthy", "ok"],
            "latency_ms": round(latency, 1),
            "response": result
        }
    
    def get_tokens(self) -> List[str]:
        """Get supported tokens."""
        result, _ = self._request("/tokens")
        if "error" in result:
            return []
        return result.get("tokens", [])
    
    def get_quote(self, token_in: str, token_out: str, amount_in: str) -> dict:
        """Request a quote from the solver."""
        # Resolve token symbols to addresses
        token_in_addr = KNOWN_TOKENS.get(token_in.upper(), token_in)
        token_out_addr = KNOWN_TOKENS.get(token_out.upper(), token_out)
        
        payload = {
            "token_in": token_in_addr,
            "token_out": token_out_addr,
            "amount_in": str(amount_in),
            "chain_id": 1  # Ethereum mainnet
        }
        
        result, latency = self._request("/quote", data=payload, method="POST")
        
        return {
            "success": "error" not in result,
            "latency_ms": round(latency, 1),
            "request": payload,
            "response": result
        }
    
    def benchmark(self, iterations: int = 10) -> dict:
        """Run performance benchmark."""
        # Use USDC -> WETH as benchmark pair
        token_in = KNOWN_TOKENS["USDC"]
        token_out = KNOWN_TOKENS["WETH"]
        amount = "1000000000"  # 1000 USDC (6 decimals)
        
        latencies = []
        successes = 0
        errors = []
        
        for i in range(iterations):
            payload = {
                "token_in": token_in,
                "token_out": token_out,
                "amount_in": amount,
                "chain_id": 1
            }
            
            result, latency = self._request("/quote", data=payload, method="POST")
            latencies.append(latency)
            
            if "error" not in result:
                successes += 1
            else:
                errors.append(result.get("error", "Unknown error"))
        
        latencies.sort()
        
        return {
            "iterations": iterations,
            "successes": successes,
            "success_rate": round(successes / iterations * 100, 1),
            "latency": {
                "min": round(min(latencies), 1),
                "max": round(max(latencies), 1),
                "avg": round(sum(latencies) / len(latencies), 1),
                "p50": round(latencies[len(latencies) // 2], 1),
                "p95": round(latencies[int(len(latencies) * 0.95)], 1) if len(latencies) >= 20 else None,
                "p99": round(latencies[int(len(latencies) * 0.99)], 1) if len(latencies) >= 100 else None,
            },
            "errors": list(set(errors))[:5]  # Unique errors, max 5
        }


def find_solver_endpoints() -> List[str]:
    """Find solver endpoints from environment or running containers."""
    endpoints = []
    
    # Check environment
    host = os.getenv("MINER_SOLVER_HOST", "localhost")
    base_port = int(os.getenv("MINER_BASE_PORT", "8000"))
    num_solvers = int(os.getenv("MINER_NUM_SOLVERS", "3"))
    
    for i in range(num_solvers):
        endpoints.append(f"http://{host}:{base_port + i}")
    
    return endpoints


def main():
    parser = argparse.ArgumentParser(
        description="Solver Quote Tester - Test and debug your solvers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/test_solver.py --health                    # Check all solvers
  python scripts/test_solver.py --health --endpoint http://localhost:8000
  python scripts/test_solver.py --tokens                    # List supported tokens
  python scripts/test_solver.py --quote USDC WETH 1000      # Get quote for 1000 USDC -> WETH
  python scripts/test_solver.py --benchmark --iterations 50 # Performance test

Token shortcuts: ETH, WETH, USDC, USDT, DAI, WBTC, UNI, LINK
        """
    )
    
    parser.add_argument("--endpoint", "-e", type=str, help="Solver endpoint URL")
    parser.add_argument("--health", action="store_true", help="Check solver health")
    parser.add_argument("--tokens", action="store_true", help="List supported tokens")
    parser.add_argument("--quote", nargs=3, metavar=("FROM", "TO", "AMOUNT"), 
                       help="Get quote: token_in token_out amount")
    parser.add_argument("--benchmark", action="store_true", help="Run performance benchmark")
    parser.add_argument("--iterations", "-n", type=int, default=10, help="Benchmark iterations")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    args = parser.parse_args()
    
    # Determine endpoints
    if args.endpoint:
        endpoints = [args.endpoint]
    else:
        endpoints = find_solver_endpoints()
    
    if not any([args.health, args.tokens, args.quote, args.benchmark]):
        args.health = True  # Default action
    
    results = []
    
    for endpoint in endpoints:
        tester = SolverTester(endpoint)
        result = {"endpoint": endpoint}
        
        if args.health:
            health = tester.check_health()
            result["health"] = health
            
            if not args.json:
                icon = "✅" if health["healthy"] else "❌"
                print(f"\n{icon} {endpoint}")
                print(f"   Latency: {health['latency_ms']}ms")
                if not health["healthy"]:
                    print(colored(f"   Error: {health['response'].get('error', 'Unknown')}", Colors.RED))
        
        if args.tokens:
            tokens = tester.get_tokens()
            result["tokens"] = tokens
            
            if not args.json:
                print(f"\n📋 Supported Tokens ({endpoint}):")
                if tokens:
                    for i, token in enumerate(tokens[:20]):
                        print(f"   {i+1}. {token}")
                    if len(tokens) > 20:
                        print(f"   ... and {len(tokens) - 20} more")
                else:
                    print("   No tokens found or endpoint unavailable")
        
        if args.quote:
            token_in, token_out, amount = args.quote
            quote = tester.get_quote(token_in, token_out, amount)
            result["quote"] = quote
            
            if not args.json:
                print(f"\n💱 Quote ({endpoint}):")
                print(f"   {token_in} -> {token_out}, Amount: {amount}")
                if quote["success"]:
                    print(colored(f"   ✅ Success ({quote['latency_ms']}ms)", Colors.GREEN))
                    resp = quote["response"]
                    if "amount_out" in resp:
                        print(f"   Amount Out: {resp['amount_out']}")
                    if "price" in resp:
                        print(f"   Price: {resp['price']}")
                    if "gas_estimate" in resp:
                        print(f"   Gas Estimate: {resp['gas_estimate']}")
                else:
                    print(colored(f"   ❌ Failed: {quote['response'].get('error', 'Unknown')}", Colors.RED))
        
        if args.benchmark:
            print(f"\n⏱️  Benchmarking {endpoint} ({args.iterations} iterations)...")
            bench = tester.benchmark(iterations=args.iterations)
            result["benchmark"] = bench
            
            if not args.json:
                print(f"\n📊 Benchmark Results ({endpoint}):")
                print(f"   Success Rate: {bench['success_rate']}%")
                print(f"   Latency:")
                print(f"      Min: {bench['latency']['min']}ms")
                print(f"      Avg: {bench['latency']['avg']}ms")
                print(f"      Max: {bench['latency']['max']}ms")
                print(f"      P50: {bench['latency']['p50']}ms")
                if bench['latency']['p95']:
                    print(f"      P95: {bench['latency']['p95']}ms")
                if bench['errors']:
                    print(f"   Errors: {', '.join(bench['errors'][:3])}")
        
        results.append(result)
    
    if args.json:
        print(json.dumps(results, indent=2))
    elif not any([args.health, args.tokens, args.quote, args.benchmark]):
        print("No action specified. Use --help for usage.")


if __name__ == "__main__":
    main()
