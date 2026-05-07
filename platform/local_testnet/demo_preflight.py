"""Preflight checks for the local Minotaur demo stack."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def _required_ports() -> tuple[tuple[int, str], ...]:
    return (
        (int(os.environ.get("HOST_FRONTEND_PORT", "4000")), "frontend"),
        (int(os.environ.get("HOST_API_PORT", "8080")), "api"),
        (int(os.environ.get("HOST_RELAYER_PORT", "8091")), "relayer"),
        (int(os.environ.get("HOST_ANVIL_ETH_PORT", "8545")), "anvil-eth"),
        (int(os.environ.get("HOST_ANVIL_BASE_PORT", "8546")), "anvil-base"),
        (int(os.environ.get("HOST_SUBTENSOR_WS_PORT", "9944")), "subtensor"),
        (int(os.environ.get("HOST_LIT_BRIDGE_PORT", "3100")), "lit-bridge"),
    )


def _api_is_ready(api_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{api_url.rstrip('/')}/health", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return resp.status == 200 and payload.get("status") == "ok"
    except Exception:
        return False


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _port_details(port: int) -> str:
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"( sport = :{port} )"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local demo prerequisites.")
    parser.add_argument(
        "--api-url",
        default=f"http://localhost:{os.environ.get('HOST_API_PORT', '8080')}",
        help="If the demo API is already healthy, skip port preflight.",
    )
    args = parser.parse_args()

    if _api_is_ready(args.api_url):
        print(f"Demo API already healthy at {args.api_url}; skipping port preflight.")
        return 0

    conflicts: list[tuple[int, str, str]] = []
    for port, label in _required_ports():
        if _port_open(port):
            conflicts.append((port, label, _port_details(port)))

    if conflicts:
        print("Demo preflight failed: required host ports are already in use.", file=sys.stderr)
        for port, label, details in conflicts:
            print(f"- `{label}` requires host port `{port}`", file=sys.stderr)
            if details:
                print(details, file=sys.stderr)
        print(
            "Free those ports before running `make demo-prep`. "
            "If this is a stale Minotaur testnet stack, try `make testnet-down`.",
            file=sys.stderr,
        )
        return 1

    print("Demo preflight passed: required host ports look available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
