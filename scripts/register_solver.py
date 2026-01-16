#!/usr/bin/env python3
"""
Solver Registration CLI - Register and manage external solvers.

This tool allows miners to register any solver that implements the OIF v1 API,
without modifying any code. Simply start your solver and use this CLI.

Usage:
    # Register a solver
    python scripts/register_solver.py register \\
        --endpoint http://localhost:9000 \\
        --solver-id my-solver-001 \\
        --name "My Custom Solver"

    # Deregister a solver
    python scripts/register_solver.py deregister --solver-id my-solver-001

    # List registered solvers
    python scripts/register_solver.py list

    # Check solver status
    python scripts/register_solver.py status --solver-id my-solver-001
"""

import argparse
import json
import os
import sys
from typing import Optional

# Note: SolverRegistry import is deferred to avoid bittensor argparser conflict


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


def print_banner():
    print()
    print(colored("═" * 60, Colors.CYAN))
    print(colored("           SOLVER REGISTRATION CLI", Colors.BOLD + Colors.CYAN))
    print(colored("═" * 60, Colors.CYAN))
    print()


def create_registry(args) -> "SolverRegistry":
    """Create a SolverRegistry instance from CLI args and environment."""
    # Deferred import to avoid bittensor argparser conflict
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from neurons.solver_registry import SolverRegistry
    
    aggregator_url = args.aggregator_url or os.getenv("AGGREGATOR_URL", "https://aggregator.minotaursubnet.com")
    api_key = args.api_key or os.getenv("MINER_API_KEY", "")
    miner_id = args.miner_id or os.getenv("MINER_ID", "")
    mode = args.mode or os.getenv("MINER_MODE", "simulation")
    
    if not api_key:
        print(colored("❌ MINER_API_KEY is required", Colors.RED))
        print("   Set via --api-key or MINER_API_KEY environment variable")
        sys.exit(1)
    
    if mode == "simulation" and not miner_id:
        print(colored("❌ MINER_ID is required in simulation mode", Colors.RED))
        print("   Set via --miner-id or MINER_ID environment variable")
        sys.exit(1)
    
    wallet = None
    if mode == "bittensor":
        try:
            import bittensor as bt
            wallet_name = args.wallet_name or os.getenv("WALLET_NAME", "default")
            wallet_hotkey = args.wallet_hotkey or os.getenv("WALLET_HOTKEY", "default")
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        except ImportError:
            print(colored("❌ bittensor package required for bittensor mode", Colors.RED))
            sys.exit(1)
        except Exception as e:
            print(colored(f"❌ Failed to load wallet: {e}", Colors.RED))
            sys.exit(1)
    
    return SolverRegistry(
        aggregator_url=aggregator_url,
        api_key=api_key,
        miner_id=miner_id if mode == "simulation" else None,
        mode=mode,
        wallet=wallet,
    )


def cmd_register(args):
    """Register a solver."""
    if not args.endpoint:
        print(colored("❌ --endpoint is required", Colors.RED))
        sys.exit(1)
    
    if not args.solver_id:
        # Generate solver ID from endpoint
        import hashlib
        hash_input = f"{args.endpoint}{os.getenv('MINER_ID', 'default')}"
        args.solver_id = f"solver-{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"
        print(f"   Generated solver ID: {args.solver_id}")
    
    registry = create_registry(args)
    
    print(f"   Aggregator: {registry.aggregator_url}")
    print(f"   Miner ID: {registry.miner_id[:20]}..." if len(registry.miner_id) > 20 else f"   Miner ID: {registry.miner_id}")
    print(f"   Mode: {registry.mode}")
    print()
    
    # Check aggregator health
    print("🔍 Checking aggregator...")
    agg_health = registry.check_aggregator_health()
    if not agg_health["healthy"]:
        print(colored(f"❌ Aggregator not healthy: {agg_health.get('error', 'Unknown')}", Colors.RED))
        sys.exit(1)
    print(colored(f"   ✅ Aggregator: {agg_health['status']}", Colors.GREEN))
    
    # Check solver health
    print(f"🔍 Checking solver at {args.endpoint}...")
    solver_health = registry.check_solver_health(args.endpoint)
    if not solver_health["healthy"]:
        print(colored(f"❌ Solver not healthy: {solver_health.get('error', 'Unknown')}", Colors.RED))
        sys.exit(1)
    print(colored(f"   ✅ Solver: healthy ({solver_health['latency_ms']}ms)", Colors.GREEN))
    
    # Register
    print(f"📝 Registering solver: {args.solver_id}...")
    result = registry.register(
        solver_id=args.solver_id,
        endpoint=args.endpoint,
        name=args.name,
        description=args.description,
        wait_for_tokens=not args.skip_tokens,
    )
    
    if result["success"]:
        print(colored(f"   ✅ {result['message']}", Colors.GREEN))
        if args.json:
            print(json.dumps(result, indent=2))
    else:
        print(colored(f"   ❌ {result['message']}", Colors.RED))
        if args.json:
            print(json.dumps(result, indent=2))
        sys.exit(1)


def cmd_deregister(args):
    """Deregister a solver."""
    if not args.solver_id:
        print(colored("❌ --solver-id is required", Colors.RED))
        sys.exit(1)
    
    registry = create_registry(args)
    
    print(f"🗑️  Deregistering solver: {args.solver_id}...")
    result = registry.deregister(args.solver_id)
    
    if result["success"]:
        print(colored(f"   ✅ {result['message']}", Colors.GREEN))
    else:
        print(colored(f"   ❌ {result['message']}", Colors.RED))
        sys.exit(1)


def cmd_list(args):
    """List registered solvers."""
    registry = create_registry(args)
    
    miner_id = args.filter_miner or registry.miner_id
    
    print(f"📋 Listing solvers for: {miner_id[:30]}...")
    solvers = registry.list_solvers(miner_id=miner_id if not args.all else None)
    
    if args.json:
        print(json.dumps(solvers, indent=2))
        return
    
    if not solvers:
        print(colored("   No solvers found", Colors.YELLOW))
        return
    
    print()
    print(f"   {'Solver ID':<35} {'Status':<12} {'Endpoint':<35}")
    print(f"   {'-'*35} {'-'*12} {'-'*35}")
    
    for solver in solvers:
        solver_id = solver.get("solver_id", solver.get("solverId", "?"))[:33]
        status = solver.get("status", "?")
        endpoint = solver.get("endpoint", "?")[:33]
        
        status_color = Colors.GREEN if status == "active" else Colors.YELLOW
        status_str = colored(f"{status:<12}", status_color)
        print(f"   {solver_id:<35} {status_str} {endpoint:<35}")
    
    print()
    print(f"   Total: {len(solvers)} solver(s)")


def cmd_status(args):
    """Check solver status."""
    if not args.solver_id:
        print(colored("❌ --solver-id is required", Colors.RED))
        sys.exit(1)
    
    registry = create_registry(args)
    
    print(f"🔍 Checking solver: {args.solver_id}...")
    solver = registry.get_solver(args.solver_id)
    
    if args.json:
        print(json.dumps(solver, indent=2))
        return
    
    if not solver:
        print(colored("   ❌ Solver not found", Colors.RED))
        sys.exit(1)
    
    print()
    status = solver.get("status", "unknown")
    status_icon = "✅" if status == "active" else "⚠️"
    
    print(f"   {status_icon} Status: {status}")
    print(f"   Solver ID: {solver.get('solver_id', solver.get('solverId', '?'))}")
    print(f"   Miner ID: {solver.get('minerId', '?')[:40]}...")
    print(f"   Endpoint: {solver.get('endpoint', '?')}")
    print(f"   Adapter: {solver.get('adapterId', '?')}")
    
    assets = solver.get("supportedAssets", {})
    if isinstance(assets, dict):
        asset_list = assets.get("assets", [])
        print(f"   Supported Assets: {len(asset_list)}")
    
    # Check endpoint health
    endpoint = solver.get("endpoint")
    if endpoint:
        print()
        print(f"   Testing endpoint...")
        health = registry.check_solver_health(endpoint)
        if health["healthy"]:
            print(colored(f"   ✅ Endpoint reachable ({health['latency_ms']}ms)", Colors.GREEN))
        else:
            print(colored(f"   ❌ Endpoint unreachable: {health.get('error', 'Unknown')}", Colors.RED))


def cmd_update(args):
    """Update solver endpoint."""
    if not args.solver_id:
        print(colored("❌ --solver-id is required", Colors.RED))
        sys.exit(1)
    if not args.endpoint:
        print(colored("❌ --endpoint is required", Colors.RED))
        sys.exit(1)
    
    registry = create_registry(args)
    
    print(f"🔄 Updating solver endpoint: {args.solver_id}...")
    result = registry.update_endpoint(args.solver_id, args.endpoint)
    
    if result["success"]:
        print(colored(f"   ✅ {result['message']}", Colors.GREEN))
    else:
        print(colored(f"   ❌ {result['message']}", Colors.RED))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Register and manage external solvers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register a new solver
  python scripts/register_solver.py register \\
      --endpoint http://localhost:9000 \\
      --solver-id my-solver-001 \\
      --name "My DEX Solver"

  # List your registered solvers
  python scripts/register_solver.py list

  # Check solver status
  python scripts/register_solver.py status --solver-id my-solver-001

  # Update solver endpoint
  python scripts/register_solver.py update \\
      --solver-id my-solver-001 \\
      --endpoint http://new-host:9000

  # Deregister a solver
  python scripts/register_solver.py deregister --solver-id my-solver-001

Environment Variables:
  AGGREGATOR_URL    Aggregator API URL (default: https://aggregator.minotaursubnet.com)
  MINER_API_KEY     API key for miner endpoints (required)
  MINER_ID          Miner identifier (required for simulation mode)
  MINER_MODE        Mode: "simulation" or "bittensor" (default: simulation)
  WALLET_NAME       Bittensor wallet name (bittensor mode)
  WALLET_HOTKEY     Bittensor wallet hotkey (bittensor mode)
        """
    )
    
    # Global arguments
    parser.add_argument("--aggregator-url", type=str, help="Aggregator URL")
    parser.add_argument("--api-key", type=str, help="Miner API key")
    parser.add_argument("--miner-id", type=str, help="Miner ID (simulation mode)")
    parser.add_argument("--mode", choices=["simulation", "bittensor"], help="Mode")
    parser.add_argument("--wallet-name", type=str, help="Wallet name (bittensor mode)")
    parser.add_argument("--wallet-hotkey", type=str, help="Wallet hotkey (bittensor mode)")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # Register command
    reg_parser = subparsers.add_parser("register", help="Register a solver")
    reg_parser.add_argument("--endpoint", "-e", type=str, required=True, help="Solver endpoint URL")
    reg_parser.add_argument("--solver-id", "-s", type=str, help="Solver ID (auto-generated if not provided)")
    reg_parser.add_argument("--name", "-n", type=str, help="Solver name")
    reg_parser.add_argument("--description", "-d", type=str, help="Solver description")
    reg_parser.add_argument("--skip-tokens", action="store_true", help="Skip token discovery")
    
    # Deregister command
    dereg_parser = subparsers.add_parser("deregister", help="Deregister a solver")
    dereg_parser.add_argument("--solver-id", "-s", type=str, required=True, help="Solver ID")
    
    # List command
    list_parser = subparsers.add_parser("list", help="List registered solvers")
    list_parser.add_argument("--all", "-a", action="store_true", help="List all solvers (not just yours)")
    list_parser.add_argument("--filter-miner", type=str, help="Filter by miner ID")
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Check solver status")
    status_parser.add_argument("--solver-id", "-s", type=str, required=True, help="Solver ID")
    
    # Update command
    update_parser = subparsers.add_parser("update", help="Update solver endpoint")
    update_parser.add_argument("--solver-id", "-s", type=str, required=True, help="Solver ID")
    update_parser.add_argument("--endpoint", "-e", type=str, required=True, help="New endpoint URL")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if not args.json:
        print_banner()
    
    commands = {
        "register": cmd_register,
        "deregister": cmd_deregister,
        "list": cmd_list,
        "status": cmd_status,
        "update": cmd_update,
    }
    
    commands[args.command](args)


if __name__ == "__main__":
    main()
