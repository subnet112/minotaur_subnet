#!/usr/bin/env python3
"""
Standalone solver harness (no aggregator required).

This script simulates the key HTTP interactions the aggregator/miner perform
against a solver:
  - GET /health
  - GET /tokens   (used by the miner in this repo during registration)
  - POST /quotes  (used by the aggregator)

It then validates the response shape and a few important invariants that the
aggregator commonly enforces:
  - quotes[] structure + nested details fields
  - settlement.executionPlan structure (optional, but required by default here)
  - interactionsHash matches a canonical hash of the execution plan
  - callValue matches the sum of interaction.value across all interactions

Usage:
  python -m tests.solver_harness --solver-url http://localhost:8000
  python tests/solver_harness.py --solver-url http://localhost:8000 --chain-id 8453 --input-symbol WETH --output-symbol USDC
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from eth_utils import keccak, to_canonical_address
except Exception:  # pragma: no cover
    keccak = None  # type: ignore
    to_canonical_address = None  # type: ignore


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _is_hex_address(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if not value.startswith("0x"):
        return False
    if len(value) != 42:
        return False
    try:
        int(value[2:], 16)
        return True
    except Exception:
        return False


def _parse_int(value: Any) -> int:
    """Parse decimal string/int or 0x-prefixed hex string into int."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("0x"):
            return int(v, 16)
        return int(v, 10)
    return int(value)


def create_interop_address(chain_id: int, eth_address: str) -> str:
    """Create an ERC-7930 interoperable address in hex format (same as neurons/solver*.py)."""
    if eth_address.startswith("0x"):
        eth_address = eth_address[2:]
    address_bytes = bytes.fromhex(eth_address)
    if len(address_bytes) != 20:
        raise ValueError(f"Invalid Ethereum address length: {len(address_bytes)} (expected 20)")
    chain_id_bytes = int(chain_id).to_bytes(8, "big")
    first_nonzero = next((i for i, b in enumerate(chain_id_bytes) if b != 0), 7)
    chain_reference = chain_id_bytes[first_nonzero:]
    result = bytes(
        [
            0x01,  # Version
            0x00,
            0x00,  # ChainType (EIP-155)
            len(chain_reference),  # ChainReferenceLength
            0x14,  # AddressLength (20 bytes for Ethereum)
        ]
    )
    result += chain_reference
    result += address_bytes
    return "0x" + result.hex()


def _compute_interactions_hash(execution_plan: Dict[str, Any]) -> str:
    """Compute canonical keccak256 hash of the execution plan (matches neurons/solver*.py)."""
    if keccak is None or to_canonical_address is None:
        raise RuntimeError(
            "Missing dependency: eth_utils. Install requirements.txt dependencies before running this harness."
        )

    encoded = bytearray()
    for interaction in (
        execution_plan.get("preInteractions", [])
        + execution_plan.get("interactions", [])
        + execution_plan.get("postInteractions", [])
    ):
        target = interaction.get("target", "")
        if target:
            encoded.extend(to_canonical_address(target))
        else:
            encoded.extend(bytes(20))

        value_int = _parse_int(interaction.get("value", "0"))
        encoded.extend(int(value_int).to_bytes(32, byteorder="big"))

        call_data_hex = interaction.get("callData", "0x")
        if isinstance(call_data_hex, str) and call_data_hex.startswith("0x"):
            call_data_hex = call_data_hex[2:]
        call_data_bytes = bytes.fromhex(call_data_hex) if call_data_hex else b""
        encoded.extend(keccak(call_data_bytes))

    return "0x" + keccak(bytes(encoded)).hex()


def _sum_call_value(execution_plan: Dict[str, Any]) -> int:
    total = 0
    for interaction in (
        execution_plan.get("preInteractions", [])
        + execution_plan.get("interactions", [])
        + execution_plan.get("postInteractions", [])
    ):
        total += _parse_int(interaction.get("value", "0"))
    return total


@dataclass(frozen=True)
class Token:
    chain_id: int
    address: str
    symbol: str
    decimals: int


def _pick_token_by_symbol(tokens: List[Token], symbol: str) -> Optional[Token]:
    want = symbol.strip().lower()
    for t in tokens:
        if t.symbol.strip().lower() == want:
            return t
    return None


def _parse_tokens_payload(tokens_payload: Dict[str, Any]) -> List[Token]:
    networks = tokens_payload.get("networks", {})
    out: List[Token] = []
    if not isinstance(networks, dict):
        return out

    for chain_key, network in networks.items():
        if not isinstance(network, dict):
            continue
        chain_id = network.get("chain_id")
        if chain_id is None:
            try:
                chain_id = int(chain_key)
            except Exception:
                continue
        tokens = network.get("tokens", [])
        if not isinstance(tokens, list):
            continue
        for t in tokens:
            if not isinstance(t, dict):
                continue
            address = t.get("address")
            symbol = t.get("symbol")
            decimals = t.get("decimals", 18)
            if not address or not symbol:
                continue
            out.append(Token(int(chain_id), str(address), str(symbol), int(decimals)))
    return out


def _http_json(method: str, url: str, *, json_body: Optional[dict] = None, timeout: int = 10) -> Tuple[int, Any]:
    resp = requests.request(method, url, json=json_body, timeout=timeout)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone solver harness (no aggregator required).")
    parser.add_argument("--solver-url", default="http://localhost:8000", help="Solver base URL (e.g., http://localhost:8000)")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout seconds")
    parser.add_argument("--chain-id", type=int, default=None, help="Chain ID to test (defaults to first network in /tokens)")
    parser.add_argument("--input-symbol", default=None, help="Input token symbol to use (e.g., WETH)")
    parser.add_argument("--output-symbol", default=None, help="Output token symbol to use (e.g., USDC)")
    parser.add_argument("--input-eth-address", default=None, help="Override input token ETH address (0x...)")
    parser.add_argument("--output-eth-address", default=None, help="Override output token ETH address (0x...)")
    parser.add_argument("--input-amount", default="1000000", help="Input amount (wei, decimal string)")
    parser.add_argument("--user-eth-address", default="0x000000000000000000000000000000000000dEaD", help="User ETH address (0x...)")
    parser.add_argument(
        "--allow-missing-settlement",
        action="store_true",
        help="Allow quotes without settlement (warn only). Default behavior is to require settlement.",
    )
    parser.add_argument("--print-json", action="store_true", help="Print raw JSON responses for debugging")

    args = parser.parse_args(argv)
    base = args.solver_url.rstrip("/")

    failures: List[str] = []
    warnings: List[str] = []
    require_settlement = not bool(args.allow_missing_settlement)

    # 1) /health
    code, health = _http_json("GET", f"{base}/health", timeout=args.timeout)
    if code != 200:
        failures.append(f"GET /health expected 200, got {code}: {health}")
    elif not isinstance(health, dict):
        failures.append(f"GET /health expected JSON object, got: {type(health)}")
    else:
        status = health.get("status")
        if status not in (None, "healthy"):
            warnings.append(f"/health returned status={status!r} (expected 'healthy' or omitted)")

    # 2) /tokens
    code, tokens_payload = _http_json("GET", f"{base}/tokens", timeout=args.timeout)
    tokens: List[Token] = []
    if code != 200:
        failures.append(f"GET /tokens expected 200, got {code}: {tokens_payload}")
    elif not isinstance(tokens_payload, dict):
        failures.append(f"GET /tokens expected JSON object, got: {type(tokens_payload)}")
    else:
        tokens = _parse_tokens_payload(tokens_payload)
        if not tokens:
            warnings.append("No tokens parsed from /tokens response (networks/tokens missing or empty)")

    # Choose chain and tokens
    chain_id = args.chain_id
    if chain_id is None:
        chain_id = tokens[0].chain_id if tokens else 1

    input_eth = args.input_eth_address
    output_eth = args.output_eth_address

    if input_eth is None or output_eth is None:
        candidates = [t for t in tokens if t.chain_id == chain_id] if tokens else []
        if args.input_symbol:
            t = _pick_token_by_symbol(candidates, args.input_symbol)
            if t:
                input_eth = t.address
        if args.output_symbol:
            t = _pick_token_by_symbol(candidates, args.output_symbol)
            if t:
                output_eth = t.address

        # Heuristic defaults: WETH/USDC if present, otherwise first two tokens.
        if candidates and (input_eth is None or output_eth is None):
            if input_eth is None:
                t = _pick_token_by_symbol(candidates, "WETH") or candidates[0]
                input_eth = t.address
            if output_eth is None:
                t = _pick_token_by_symbol(candidates, "USDC")
                if t and t.address.lower() != (input_eth or "").lower():
                    output_eth = t.address
                else:
                    output_eth = candidates[1].address if len(candidates) > 1 else candidates[0].address

    if not input_eth or not output_eth:
        failures.append(
            "Unable to select input/output token. Provide --input-eth-address/--output-eth-address or ensure /tokens includes tokens."
        )
        # Still print partial info if requested
        if args.print_json:
            print(json.dumps({"health": health, "tokens": tokens_payload}, indent=2))
        _emit_report(base, health, tokens_payload, None, None, failures, warnings, args.print_json)
        return 2

    if not _is_hex_address(input_eth) or not _is_hex_address(output_eth):
        failures.append(
            f"Input/output ETH address must be 0x-prefixed 20-byte hex. input={input_eth!r}, output={output_eth!r}"
        )

    user_eth = args.user_eth_address
    if not _is_hex_address(user_eth):
        failures.append(f"--user-eth-address must be a 0x-prefixed 20-byte hex address, got {user_eth!r}")

    # 3) /quotes
    user_interop = create_interop_address(chain_id, user_eth)
    input_interop = create_interop_address(chain_id, input_eth)
    output_interop = create_interop_address(chain_id, output_eth)

    quote_req = {
        "user": user_interop,
        "availableInputs": [{"asset": input_interop, "amount": str(args.input_amount), "user": user_interop}],
        "requestedOutputs": [{"asset": output_interop, "minAmount": "0", "receiver": user_interop}],
    }

    code, quotes_payload = _http_json("POST", f"{base}/quotes", json_body=quote_req, timeout=args.timeout)
    if code != 200:
        failures.append(f"POST /quotes expected 200, got {code}: {quotes_payload}")
        _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
        return 2
    if not isinstance(quotes_payload, dict):
        failures.append(f"POST /quotes expected JSON object, got: {type(quotes_payload)}")
        _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
        return 2

    quotes = quotes_payload.get("quotes")
    if not isinstance(quotes, list):
        failures.append("POST /quotes response missing 'quotes' array")
        _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
        return 2

    if not quotes:
        warnings.append("Solver returned 0 quotes. This can be OK (unsupported pair / no liquidity), but aggregator won't select you.")
        _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
        return 0

    # Validate first quote (developers usually iterate here; extend as needed).
    quote0 = quotes[0]
    if not isinstance(quote0, dict):
        failures.append("quotes[0] must be an object")
        _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
        return 2

    for k in ("quoteId", "provider", "details"):
        if k not in quote0:
            failures.append(f"quotes[0] missing required field: {k}")

    details = quote0.get("details")
    if isinstance(details, dict):
        ai = details.get("availableInputs")
        ro = details.get("requestedOutputs")
        if not isinstance(ai, list) or not ai:
            failures.append("quotes[0].details.availableInputs must be a non-empty array")
        if not isinstance(ro, list) or not ro:
            failures.append("quotes[0].details.requestedOutputs must be a non-empty array")
    else:
        failures.append("quotes[0].details must be an object")

    settlement = quote0.get("settlement")
    if settlement is None:
        msg = "quotes[0].settlement is missing"
        if require_settlement:
            failures.append(msg)
        else:
            warnings.append(msg + " (allowed by --allow-missing-settlement)")
    elif not isinstance(settlement, dict):
        failures.append("quotes[0].settlement must be an object")
    else:
        exec_plan = settlement.get("executionPlan")
        if not isinstance(exec_plan, dict):
            failures.append("quotes[0].settlement.executionPlan must be an object")
        else:
            # Validate arrays exist
            for arr_key in ("preInteractions", "interactions", "postInteractions"):
                arr = exec_plan.get(arr_key)
                if not isinstance(arr, list):
                    failures.append(f"executionPlan.{arr_key} must be an array")

            # Validate interactionsHash
            interactions_hash = settlement.get("interactionsHash")
            if isinstance(interactions_hash, str) and interactions_hash.startswith("0x"):
                try:
                    computed = _compute_interactions_hash(exec_plan)
                    if computed.lower() != interactions_hash.lower():
                        failures.append(
                            f"interactionsHash mismatch: computed={computed} response={interactions_hash}"
                        )
                except Exception as exc:
                    failures.append(f"Failed to compute interactionsHash: {exc}")
            else:
                failures.append("quotes[0].settlement.interactionsHash must be a 0x-prefixed hex string")

            # Validate callValue
            call_value_raw = settlement.get("callValue", "0")
            try:
                call_value_int = _parse_int(call_value_raw)
                sum_values = _sum_call_value(exec_plan)
                if call_value_int != sum_values:
                    failures.append(f"callValue mismatch: callValue={call_value_int} sum(values)={sum_values}")
            except Exception as exc:
                failures.append(f"Failed to validate callValue: {exc}")

    _emit_report(base, health, tokens_payload, quote_req, quotes_payload, failures, warnings, args.print_json)
    return 0 if not failures else 2


def _emit_report(
    base: str,
    health: Any,
    tokens_payload: Any,
    quote_req: Optional[dict],
    quotes_payload: Any,
    failures: List[str],
    warnings: List[str],
    print_json: bool,
) -> None:
    print(f"Solver: {base}")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"- {f}")
    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print(f"- {w}")

    if print_json:
        print("\nRAW JSON:")
        print(json.dumps({"health": health, "tokens": tokens_payload, "quoteRequest": quote_req, "quotes": quotes_payload}, indent=2))

    if failures:
        _eprint("\nResult: FAIL")
    else:
        print("\nResult: OK")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


