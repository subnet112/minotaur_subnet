"""Calldata + revert-data decoders for actionable simulator error messages.

Without these, a failed swap looks like ``target=0x2626... value=0`` —
useless. With them, the strategy author sees ``fn=exactInputSingle
reason="Too little received"`` and can fix it in one iteration instead
of burning ten LLM cycles guessing.

Selectors are kept inline rather than fetched from 4byte.directory so
the simulator works offline and the same mapping ships with every
deploy. Add new entries when a real revert in production produces an
unknown selector.
"""

from __future__ import annotations

import logging
from typing import Any

from eth_abi import decode as _abi_decode

logger = logging.getLogger(__name__)


# ── Revert ABI encodings ───────────────────────────────────────────────

_ERROR_SELECTOR = bytes.fromhex("08c379a0")    # Error(string)
_PANIC_SELECTOR = bytes.fromhex("4e487b71")    # Panic(uint256)

# Solidity Panic(uint256) codes — from the language reference.
_PANIC_REASONS: dict[int, str] = {
    0x00: "GENERIC_PANIC",
    0x01: "ASSERT_FAIL",
    0x11: "ARITHMETIC_OVERFLOW_OR_UNDERFLOW",
    0x12: "DIVISION_OR_MODULO_BY_ZERO",
    0x21: "ENUM_CONVERSION_OUT_OF_RANGE",
    0x22: "STORAGE_BYTE_ARRAY_INCORRECTLY_ENCODED",
    0x31: "POP_FROM_EMPTY_ARRAY",
    0x32: "ARRAY_OUT_OF_BOUNDS_ACCESS",
    0x41: "ALLOCATION_TOO_LARGE_OR_OUT_OF_MEMORY",
    0x51: "CALL_TO_UNINITIALIZED_INTERNAL_FUNCTION",
}

# Custom error selectors we've seen in production. Keys are 4-byte
# selectors; values are human-readable names.
_CUSTOM_ERRORS: dict[bytes, str] = {
    # Generic
    bytes.fromhex("f4d678b8"): "InsufficientBalance()",
    bytes.fromhex("8b063d73"): "NotAuthorized()",
    # SafeERC20 (OpenZeppelin)
    bytes.fromhex("486aa307"): "STF (SafeERC20: transferFrom failed)",
    bytes.fromhex("a9059cbb"): "ST  (SafeERC20: transfer failed)",
    # Uniswap V3 SwapRouter
    bytes.fromhex("85e8db05"): "Too little received (V3 SwapRouter)",
    bytes.fromhex("c9f52c71"): "Too much requested (V3 SwapRouter)",
    bytes.fromhex("f1ab7b71"): "STF (V3 SwapRouter SafeTransferFrom)",
    # Universal Router / Permit2
    bytes.fromhex("675cae38"): "TransactionDeadlinePassed",
    bytes.fromhex("3f6cc768"): "InsufficientToken (Permit2)",
    # WETH9
    bytes.fromhex("3e3f8f73"): "WETH: insufficient",
}

# Function selectors → human names. Kept tight; add as needed.
_FUNCTION_NAMES: dict[bytes, str] = {
    # ERC-20
    bytes.fromhex("095ea7b3"): "approve(address,uint256)",
    bytes.fromhex("23b872dd"): "transferFrom(address,address,uint256)",
    bytes.fromhex("a9059cbb"): "transfer(address,uint256)",
    bytes.fromhex("70a08231"): "balanceOf(address)",
    bytes.fromhex("dd62ed3e"): "allowance(address,address)",
    # WETH
    bytes.fromhex("d0e30db0"): "deposit() [WETH wrap]",
    bytes.fromhex("2e1a7d4d"): "withdraw(uint256) [WETH unwrap]",
    # Uniswap V3 SwapRouter / SwapRouter02
    bytes.fromhex("04e45aaf"): "exactInputSingle(...)",
    bytes.fromhex("c04b8d59"): "exactInput(...)",
    bytes.fromhex("5023b4df"): "exactOutputSingle(...)",
    bytes.fromhex("09b81346"): "exactOutput(...)",
    bytes.fromhex("ac9650d8"): "multicall(bytes[])",
    bytes.fromhex("5ae401dc"): "multicall(uint256,bytes[]) [w/ deadline]",
    bytes.fromhex("12210e8a"): "refundETH()",
    bytes.fromhex("49404b7c"): "unwrapWETH9(uint256,address)",
    bytes.fromhex("df2ab5bb"): "sweepToken(address,uint256,address)",
    # Uniswap V2 / Aerodrome / Velodrome routers
    bytes.fromhex("18cbafe5"): "swapExactTokensForETH(...)",
    bytes.fromhex("38ed1739"): "swapExactTokensForTokens(...)",
    bytes.fromhex("8803dbee"): "swapTokensForExactTokens(...)",
    bytes.fromhex("7ff36ab5"): "swapExactETHForTokens(...)",
    bytes.fromhex("4a25d94a"): "swapTokensForExactETH(...)",
    bytes.fromhex("fb3bdb41"): "swapETHForExactTokens(...)",
    # Uniswap V3 Quoter
    bytes.fromhex("c6a5026a"): "quoteExactInputSingle(...) [V1]",
    bytes.fromhex("f7729d43"): "quoteExactInputSingle(...) [V2]",
    bytes.fromhex("cdca1753"): "quoteExactInput(bytes,uint256)",
    # AppIntentBase (our own)
    bytes.fromhex("51e02c64"): "scoreIntent(...)",
}


def decode_call(calldata: bytes | str) -> str:
    """Identify the function being called from raw calldata.

    Returns a human-readable label like ``"exactInputSingle(...)"`` or
    ``"selector=0xdeadbeef"`` when unknown. Empty calldata → ``"<empty>"``.
    """
    if isinstance(calldata, str):
        s = calldata[2:] if calldata.startswith("0x") else calldata
        try:
            calldata = bytes.fromhex(s)
        except ValueError:
            return f"<unparseable: {calldata[:18]}...>"
    if not calldata or len(calldata) < 4:
        return "<empty>"
    selector = calldata[:4]
    return _FUNCTION_NAMES.get(selector, f"selector=0x{selector.hex()}")


def decode_revert_data(data: bytes | str) -> str:
    """Decode an EVM revert payload into a human string.

    Recognises:
      - ``Error(string)`` (Solidity ``require(x, "msg")`` and ``revert("msg")``)
      - ``Panic(uint256)`` (built-in panics: overflow, divide-by-zero, etc.)
      - Known custom errors (4-byte selector lookup, see ``_CUSTOM_ERRORS``)
      - Unknown custom errors → reports selector + first 32 bytes of body
    """
    if isinstance(data, str):
        s = data[2:] if data.startswith("0x") else data
        try:
            data = bytes.fromhex(s)
        except ValueError:
            return f"unparseable revert: {data[:18]}"
    if not data:
        return "(empty revert)"
    if len(data) < 4:
        return f"raw 0x{data.hex()}"
    selector = data[:4]
    body = data[4:]
    if selector == _ERROR_SELECTOR:
        try:
            (msg,) = _abi_decode(["string"], body)
            return f'Error("{msg}")'
        except Exception:
            return "Error(undecodable)"
    if selector == _PANIC_SELECTOR:
        try:
            (code,) = _abi_decode(["uint256"], body)
            label = _PANIC_REASONS.get(code, "unknown")
            return f"Panic(0x{code:02x}): {label}"
        except Exception:
            return "Panic(undecodable)"
    name = _CUSTOM_ERRORS.get(selector)
    if name:
        return f"{name}"
    body_preview = body[:32].hex() if body else ""
    suffix = f", body 0x{body_preview}" if body_preview else ""
    return f"CustomError 0x{selector.hex()}{suffix}"


def extract_revert_via_trace(w3: Any, tx_hash: Any) -> str:
    """Pull the revert reason from a failed transaction via Anvil's
    ``debug_traceTransaction``.

    Anvil supports debug_traceTransaction natively. The trace contains
    a top-level ``returnValue`` field on revert which holds the raw
    EVM revert bytes. We decode them with :func:`decode_revert_data`.

    Returns "" if the trace can't be retrieved or doesn't contain a
    revert payload (e.g. out-of-gas with no revert data).
    """
    if hasattr(tx_hash, "hex"):
        tx_hash_str = tx_hash.hex()
    else:
        tx_hash_str = str(tx_hash)
    if not tx_hash_str.startswith("0x"):
        tx_hash_str = "0x" + tx_hash_str
    try:
        resp = w3.provider.make_request("debug_traceTransaction", [tx_hash_str, {}])
    except Exception as exc:
        logger.debug("debug_traceTransaction failed: %s", exc)
        return ""
    result = resp.get("result") if isinstance(resp, dict) else None
    if not result:
        return ""
    return_value = result.get("returnValue") or result.get("output") or ""
    if not return_value:
        return ""
    if isinstance(return_value, str) and not return_value.startswith("0x"):
        return_value = "0x" + return_value
    return decode_revert_data(return_value)
