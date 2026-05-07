"""Local demo verifier for the seeded DexAggregatorApp.

This script is intended for presenter/operator use, not pytest. It verifies
that the local Minotaur demo stack is actually usable end-to-end:

1. API / relayer / block loop are healthy
2. A seeded `DexAggregatorApp` is present and order-ready
3. A managed wallet can be created and funded
4. A quote can be produced
5. A real order can be submitted and filled
6. The resulting relayed transaction succeeds on-chain
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


NON_TERMINAL_ORDER_STATUSES = {
    "open",
    "assigned",
    "solved",
    "scored",
    "approved",
    "submitted",
    "bridging",
    "pending",
}


def _request_json(
    method: str,
    url: str,
    data: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            parsed = json.loads(payload) if payload else {}
            if isinstance(parsed, dict):
                return resp.status, parsed
            return resp.status, {"value": parsed}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        return exc.code, parsed


def _rpc_json(
    url: str,
    method: str,
    params: list[Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params or [],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected RPC response from {url}: {payload!r}")
    return payload


def _wait_for_health(base_url: str, name: str, timeout: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            status, payload = _request_json("GET", f"{base_url.rstrip('/')}/health", timeout=10)
            last_payload = payload
            if status == 200 and payload.get("status") == "ok":
                return payload
        except Exception as exc:  # pragma: no cover - operator path only
            last_payload = {"error": str(exc)}
        time.sleep(2)
    raise RuntimeError(f"{name} health never became ready at {base_url}: {last_payload}")


def _wait_for_blockloop(api_url: str, timeout: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{api_url.rstrip('/')}/v1/blockloop/status", timeout=10)
        last_payload = payload
        if status == 200 and payload.get("running") is True:
            return payload
        time.sleep(2)
    raise RuntimeError(f"Block loop never became ready: {last_payload}")


def _wait_for_seeded_app(
    api_url: str,
    app_name: str,
    chain_id: int,
    timeout: int = 180,
) -> dict[str, str]:
    deadline = time.time() + timeout
    last_seen: dict[str, Any] | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{api_url.rstrip('/')}/v1/apps/", timeout=15)
        if status == 200:
            apps = payload.get("apps", [])
            for app in apps:
                if app.get("name") != app_name:
                    continue
                app_id = app.get("app_id", "")
                if not app_id:
                    continue
                s_status, s_payload = _request_json(
                    "GET",
                    f"{api_url.rstrip('/')}/v1/apps/{app_id}/status",
                    timeout=15,
                )
                if s_status != 200:
                    last_seen = {"app_id": app_id, "status_response": s_payload}
                    continue
                deployments = s_payload.get("deployments", {})
                deployment = deployments.get(str(chain_id), {})
                overall_status = s_payload.get("status", "")
                deployment_status = deployment.get("status", overall_status)
                contract_address = (
                    deployment.get("contract_address")
                    or s_payload.get("contract_address")
                    or ""
                )
                last_seen = {
                    "app_id": app_id,
                    "overall_status": overall_status,
                    "deployment_status": deployment_status,
                    "contract_address": contract_address,
                }
                if (
                    deployment_status in {"solved", "active"}
                    and isinstance(contract_address, str)
                    and contract_address.startswith("0x")
                ):
                    return {
                        "app_id": app_id,
                        "contract_address": contract_address,
                        "status": deployment_status,
                    }
        time.sleep(2)
    raise RuntimeError(
        f"Seeded app {app_name!r} never became order-ready on chain {chain_id}: {last_seen}"
    )


def _create_managed_wallet(api_url: str, chain_id: int) -> dict[str, Any]:
    status, payload = _request_json(
        "POST",
        f"{api_url.rstrip('/')}/v1/wallets/",
        {"chain_ids": [chain_id]},
    )
    if status != 200 or not str(payload.get("address", "")).startswith("0x"):
        raise RuntimeError(f"Managed wallet creation failed: HTTP {status} {payload}")
    return payload


def _fund_wallet(api_url: str, address: str, chain_id: int, token: str, amount: str) -> None:
    status, eth_payload = _request_json(
        "POST",
        f"{api_url.rstrip('/')}/v1/testnet/faucet",
        {"address": address, "amount_eth": 1.0, "chain_id": chain_id},
    )
    if status != 200:
        raise RuntimeError(f"ETH faucet failed: HTTP {status} {eth_payload}")

    status, token_payload = _request_json(
        "POST",
        f"{api_url.rstrip('/')}/v1/testnet/faucet_erc20",
        {"token": token, "address": address, "amount": amount, "chain_id": chain_id},
    )
    if status != 200:
        raise RuntimeError(f"{token} faucet failed: HTTP {status} {token_payload}")


def _get_wallet_balances(api_url: str, address: str, chain_id: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({"chain_id": chain_id})
    status, payload = _request_json(
        "GET",
        f"{api_url.rstrip('/')}/v1/wallets/{address}/balances?{query}",
    )
    if status != 200:
        raise RuntimeError(f"Balance lookup failed: HTTP {status} {payload}")
    return payload


def _prepare_swap(
    api_url: str,
    app_id: str,
    chain_id: int,
    submitted_by: str,
    input_token: str,
    output_token: str,
    input_amount: str,
) -> dict[str, Any]:
    status, payload = _request_json(
        "POST",
        f"{api_url.rstrip('/')}/v1/apps/{app_id}/prepare",
        {
            "chain_id": chain_id,
            "intent_function": "swap",
            "submitted_by": submitted_by,
            "params": {
                "input_token": input_token,
                "output_token": output_token,
                "input_amount": input_amount,
            },
        },
    )
    if status != 200:
        raise RuntimeError(f"Prepare failed: HTTP {status} {payload}")
    return payload


def _wait_for_quote(
    api_url: str,
    app_id: str,
    chain_id: int,
    params: dict[str, Any],
    timeout: int = 180,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    request = {
        "chain_id": chain_id,
        "intent_function": "swap",
        "params": params,
    }
    while time.time() < deadline:
        status, payload = _request_json(
            "POST",
            f"{api_url.rstrip('/')}/v1/apps/{app_id}/quote",
            request,
            timeout=30,
        )
        last_payload = payload
        if (
            status == 200
            and payload.get("estimated_output") not in (None, "", "0")
            and payload.get("suggested_min_output") not in (None, "", "0")
            and payload.get("ready_params", {}).get("min_output_amount") not in (None, "", "0")
        ):
            return payload
        time.sleep(2)
    raise RuntimeError(f"Quote never became ready for {app_id}: {last_payload}")


def _submit_order(
    api_url: str,
    app_id: str,
    chain_id: int,
    submitted_by: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    status, payload = _request_json(
        "POST",
        f"{api_url.rstrip('/')}/v1/apps/{app_id}/orders",
        {
            "chain_id": chain_id,
            "intent_function": "swap",
            "submitted_by": submitted_by,
            "params": params,
        },
        timeout=45,
    )
    if status != 201:
        raise RuntimeError(f"Order submission failed: HTTP {status} {payload}")
    return payload


def _wait_for_order_terminal(api_url: str, order_id: str, timeout: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{api_url.rstrip('/')}/v1/orders/{order_id}",
            timeout=15,
        )
        last_payload = payload
        if status == 200 and payload.get("status") not in NON_TERMINAL_ORDER_STATUSES:
            return payload
        time.sleep(3)
    raise RuntimeError(f"Order {order_id} never reached a terminal state: {last_payload}")


def _verify_receipt(eth_rpc_url: str, tx_hash: str, expected_to: str) -> dict[str, Any]:
    receipt = _rpc_json(eth_rpc_url, "eth_getTransactionReceipt", [tx_hash])
    result = receipt.get("result") or {}
    if result.get("status") != "0x1":
        raise RuntimeError(f"Transaction {tx_hash} did not succeed: {receipt}")
    receipt_to = (result.get("to") or "").lower()
    if expected_to and receipt_to != expected_to.lower():
        raise RuntimeError(
            f"Transaction target mismatch: expected {expected_to}, got {result.get('to')}"
        )
    return result


def _token_balances_by_symbol(payload: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for token in payload.get("tokens", []):
        symbol = str(token.get("symbol", "")).upper()
        raw = str(token.get("balance_raw", "0"))
        try:
            result[symbol] = int(raw)
        except ValueError:
            result[symbol] = 0
    return result


def _print_summary(summary: dict[str, Any]) -> None:
    print("")
    print("Minotaur local demo is ready.")
    print(f"Frontend:       {summary['frontend_url']}")
    print(f"API:            {summary['api_url']}")
    print(f"Seeded app:     {summary['app_id']} @ {summary['app_address']}")
    print(f"Managed wallet: {summary['wallet_address']}")
    print(f"Order ID:       {summary['order_id']}")
    print(f"Tx hash:        {summary['tx_hash']}")
    print(f"Route:          {summary['route_summary']}")
    print(f"Quoted out:     {summary['estimated_output']}")
    print(f"Min out:        {summary['suggested_min_output']}")
    print(f"USDC delta:     {summary['usdc_before']} -> {summary['usdc_after']}")
    print(f"WETH balance:   {summary['weth_after']}")
    print(f"Order score:    {summary['score']}")
    print(f"Best score:     {summary['best_score']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the local Minotaur demo end-to-end.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get(
            "LOCAL_TESTNET_API_URL",
            f"http://localhost:{os.environ.get('HOST_API_PORT', '8080')}",
        ),
        help="API base URL",
    )
    parser.add_argument(
        "--relayer-url",
        default=os.environ.get(
            "LOCAL_TESTNET_RELAYER_URL",
            f"http://localhost:{os.environ.get('HOST_RELAYER_PORT', '8091')}",
        ),
        help="Relayer base URL",
    )
    parser.add_argument(
        "--frontend-url",
        default=os.environ.get(
            "LOCAL_TESTNET_FRONTEND_URL",
            f"http://localhost:{os.environ.get('HOST_FRONTEND_PORT', '4000')}",
        ),
        help="Frontend URL to print in the summary",
    )
    parser.add_argument(
        "--eth-rpc-url",
        default=os.environ.get(
            "LOCAL_TESTNET_ETH_RPC_URL",
            f"http://localhost:{os.environ.get('HOST_ANVIL_ETH_PORT', '8545')}",
        ),
        help="Ethereum fork RPC URL",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=int(os.environ.get("LOCAL_TESTNET_CHAIN_ID", "31337")),
        help="Chain ID to use for the demo order",
    )
    parser.add_argument(
        "--app-name",
        default=os.environ.get("LOCAL_TESTNET_APP_NAME", "DexAggregatorApp"),
        help="Seeded app name to locate",
    )
    parser.add_argument(
        "--input-token",
        default=os.environ.get("LOCAL_TESTNET_INPUT_TOKEN", "USDC"),
        help="Input token symbol",
    )
    parser.add_argument(
        "--output-token",
        default=os.environ.get("LOCAL_TESTNET_OUTPUT_TOKEN", "WETH"),
        help="Output token symbol",
    )
    parser.add_argument(
        "--input-amount",
        default=os.environ.get("LOCAL_TESTNET_INPUT_AMOUNT", "1000000"),
        help="Input amount in raw token units",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON summary",
    )
    args = parser.parse_args()

    print("Checking API and relayer health...")
    _wait_for_health(args.api_url, "API")
    _wait_for_health(args.relayer_url, "Relayer")
    _wait_for_blockloop(args.api_url)

    print(f"Locating seeded {args.app_name} on chain {args.chain_id}...")
    app = _wait_for_seeded_app(args.api_url, args.app_name, args.chain_id)

    print("Creating managed wallet...")
    wallet = _create_managed_wallet(args.api_url, args.chain_id)
    wallet_address = str(wallet["address"])

    print(f"Funding wallet {wallet_address}...")
    _fund_wallet(
        args.api_url,
        wallet_address,
        args.chain_id,
        args.input_token,
        args.input_amount,
    )
    before = _get_wallet_balances(args.api_url, wallet_address, args.chain_id)

    print("Preparing and quoting swap...")
    prepared = _prepare_swap(
        args.api_url,
        app["app_id"],
        args.chain_id,
        wallet_address,
        args.input_token,
        args.output_token,
        args.input_amount,
    )
    quote = _wait_for_quote(
        args.api_url,
        app["app_id"],
        args.chain_id,
        prepared.get("resolved_params", {}),
    )

    print("Submitting order...")
    order = _submit_order(
        args.api_url,
        app["app_id"],
        args.chain_id,
        wallet_address,
        quote.get("ready_params", prepared.get("resolved_params", {})),
    )
    order_id = str(order["order_id"])

    print(f"Waiting for fill of order {order_id}...")
    terminal = _wait_for_order_terminal(args.api_url, order_id)
    if terminal.get("status") != "filled":
        raise RuntimeError(f"Demo order finished in unexpected status: {terminal}")

    tx_hash = str(terminal.get("tx_hash", ""))
    if not tx_hash:
        raise RuntimeError(f"Demo order is missing tx_hash: {terminal}")
    _verify_receipt(args.eth_rpc_url, tx_hash, app["contract_address"])

    after = _get_wallet_balances(args.api_url, wallet_address, args.chain_id)
    before_tokens = _token_balances_by_symbol(before)
    after_tokens = _token_balances_by_symbol(after)

    summary = {
        "frontend_url": args.frontend_url,
        "api_url": args.api_url,
        "app_id": app["app_id"],
        "app_address": app["contract_address"],
        "wallet_address": wallet_address,
        "order_id": order_id,
        "tx_hash": tx_hash,
        "route_summary": quote.get("route_summary", ""),
        "estimated_output": quote.get("estimated_output", ""),
        "suggested_min_output": quote.get("suggested_min_output", ""),
        "usdc_before": before_tokens.get(args.input_token.upper(), 0),
        "usdc_after": after_tokens.get(args.input_token.upper(), 0),
        "weth_after": after_tokens.get(args.output_token.upper(), 0),
        "score": terminal.get("score"),
        "best_score": terminal.get("best_score"),
        "status": terminal.get("status"),
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Demo check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
