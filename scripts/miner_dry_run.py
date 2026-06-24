#!/usr/bin/env python3
"""Reference client for miners to call ``/v1/orders/{id}/dry-run``.

The endpoint is gated by either admin key OR a bittensor-hotkey-signed
request. This script demonstrates the miner path: it loads your local
bittensor wallet, signs the canonical message, and POSTs the dry-run
request.

The ``/orders/{id}/dry-run`` path below scores a plan with a MOCK simulation
(fast, JS score only). For the full REAL-simulation report — on-chain score,
gas, transfers, and the decoded on-chain ``revert_reason`` when it fails — POST
the SAME signed headers to ``/v1/apps/{app_id}/score`` instead (it runs the
validator's fork so you don't need your own archive node). Only the path + body
change; the signing protocol below is identical.

Usage::

    # Default leader API
    python scripts/miner_dry_run.py \\
        --wallet-name my_wallet --hotkey-name my_hotkey \\
        --order-id order_abc123 \\
        --plan plan.json

    # Custom API endpoint (eg. your own validator for local testing)
    python scripts/miner_dry_run.py \\
        --api-url http://localhost:8080 \\
        --wallet-name my_wallet --hotkey-name my_hotkey \\
        --order-id order_abc123 \\
        --plan plan.json

``plan.json`` is a JSON document of the form::

    {
        "interactions": [
            {
                "target":    "0xUniswapRouter...",
                "value":     0,
                "call_data": "0x38ed1739...",
                "chain_id":  8453
            }
        ],
        "deadline": 0,
        "nonce":    0,
        "metadata": {}
    }

The script prints the validator's response: ``{score, valid, reason, breakdown}``.

Rate limit: 60 calls/hour/hotkey by default. Subject to change — check
the validator's response for ``429 Too Many Requests`` and back off.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def build_canonical_message(method: str, path: str, timestamp: int) -> str:
    """Match the validator's _require_admin_or_signed_miner construction.

    Format: ``f"{METHOD} {PATH} {TIMESTAMP}"``. Whitespace-sensitive —
    do NOT add or strip spaces.
    """
    return f"{method} {path} {timestamp}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default="https://api.minotaursubnet.com",
                        help="Validator API base URL (default: leader)")
    parser.add_argument("--wallet-name", required=True,
                        help="bittensor wallet name (e.g. 'my_wallet')")
    parser.add_argument("--hotkey-name", required=True,
                        help="bittensor hotkey name (e.g. 'my_hotkey')")
    parser.add_argument("--order-id", required=True,
                        help="Order ID to score the plan against")
    parser.add_argument("--plan", required=True, type=Path,
                        help="Path to plan.json (see module docstring for schema)")
    args = parser.parse_args()

    if not args.plan.is_file():
        print(f"error: plan file not found: {args.plan}", file=sys.stderr)
        return 1

    body = json.loads(args.plan.read_text())

    # Load wallet + sign the canonical message
    try:
        import bittensor as bt
    except ImportError:
        print("error: bittensor not installed; pip install bittensor", file=sys.stderr)
        return 1

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.hotkey_name)
    hotkey_ss58 = wallet.hotkey.ss58_address

    path = f"/v1/orders/{args.order_id}/dry-run"
    timestamp = int(time.time())
    message = build_canonical_message("POST", path, timestamp)
    sig_bytes = wallet.hotkey.sign(message)
    signature_hex = "0x" + sig_bytes.hex()

    headers = {
        "Content-Type": "application/json",
        "X-Bittensor-Hotkey": hotkey_ss58,
        "X-Bittensor-Signature": signature_hex,
        "X-Bittensor-Timestamp": str(timestamp),
    }

    url = args.api_url.rstrip("/") + path
    print(f"[miner-dry-run] POST {url}", file=sys.stderr)
    print(f"[miner-dry-run]   hotkey={hotkey_ss58[:16]}…  ts={timestamp}", file=sys.stderr)

    resp = requests.post(url, headers=headers, json=body, timeout=60)
    if resp.status_code != 200:
        print(f"error: {resp.status_code} {resp.reason}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        return 1

    print(json.dumps(resp.json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
