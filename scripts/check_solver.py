#!/usr/bin/env python3
"""Check solver status on the aggregator."""
import requests
import os
import sys
import json

def check_solver(solver_id: str, aggregator_url: str, api_key: str = None):
    """Check if a solver is registered and its status."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    try:
        response = requests.get(
            f"{aggregator_url}/v1/solvers/{solver_id}",
            headers=headers,
            timeout=5
        )
        
        if response.status_code == 200:
            data = response.json()
            status = data.get('status', 'unknown')
            print(f"✅ Solver found: {solver_id}")
            print(f"   Status: {status}")
            print(f"   Endpoint: {data.get('endpoint', 'N/A')}")
            print(f"   Miner ID: {data.get('minerId', 'N/A')}")
            print(f"   Adapter: {data.get('adapterId', 'N/A')}")
            
            if status == 'inactive':
                print("   ⚠️  WARNING: Solver is INACTIVE - needs to be re-registered")
                return False
            elif status == 'active':
                print("   ✅ Solver is ACTIVE")
                return True
            else:
                print(f"   ⚠️  Unknown status: {status}")
                return None
        elif response.status_code == 404:
            print(f"❌ Solver not found: {solver_id}")
            return False
        else:
            print(f"❌ Error: HTTP {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Error checking solver: {e}")
        return False

def list_all_solvers(aggregator_url: str, api_key: str = None):
    """List all solvers from health endpoint."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    try:
        response = requests.get(f"{aggregator_url}/health", headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            solvers = data.get("solvers", {})
            health_details = solvers.get("healthDetails", {})
            
            print("📊 Aggregator Solver Summary:")
            print(f"   Total: {solvers.get('total', 0)}")
            print(f"   Active: {solvers.get('active', 0)}")
            print(f"   Inactive: {solvers.get('inactive', 0)}")
            print(f"   Healthy: {solvers.get('healthy', 0)}")
            print(f"   Unhealthy: {solvers.get('unhealthy', 0)}")
            
            if health_details:
                print("\n🔍 Solver Details:")
                for solver_id, is_healthy in health_details.items():
                    # Get detailed status
                    solver_info = check_solver(solver_id, aggregator_url, api_key)
                    print()
        else:
            print(f"❌ Health check failed: HTTP {response.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    aggregator_url = os.getenv("AGGREGATOR_URL", "http://localhost:4000")
    api_key = os.getenv("AGGREGATOR_API_KEY")
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--all":
            list_all_solvers(aggregator_url, api_key)
        else:
            solver_id = sys.argv[1]
            check_solver(solver_id, aggregator_url, api_key)
    else:
        print("Usage:")
        print(f"  {sys.argv[0]} <solver_id>  # Check specific solver")
        print(f"  {sys.argv[0]} --all        # List all solvers")
        print("\nExample:")
        print(f"  {sys.argv[0]} 5GxnM365xyjm9WvT2GsHQ6erhi7yEGREr8gJJ74bkazAQqfV-solver-00")
        print(f"\nEnvironment variables:")
        print(f"  AGGREGATOR_URL={aggregator_url}")
        print(f"  AGGREGATOR_API_KEY={'SET' if api_key else 'NOT SET'}")

