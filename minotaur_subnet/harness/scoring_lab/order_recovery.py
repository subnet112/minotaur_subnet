"""Phase 5b prototype — recover the Stage-2 filled-order corpus from CHAIN.

A fresh validator (e.g. a former follower promoted to leader) has no local order
history. But every filled intent — for ANY app — goes through the generic
``AppIntentBase.executeIntent`` and emits
``IntentExecuted(orderId, submittedBy, score, planHash, gasUsed)``. This module
rebuilds the corpus from chain:

    IntentExecuted logs -> fetch each tx -> decode executeIntent calldata
    -> recover the IntentOrder (params, selector, deadline, perpetual terms)
    -> canonical, byte-deterministic order record (PII stripped) -> corpus hash

The corpus IS the chain: no validator can fabricate fills, and the record is
byte-identical across nodes, so the benchmark pack hash converges. This is the
GENERIC, app-independent anchor — NOT the DexAggregator-specific ``SwapExecuted``.
The raw ``intentParams`` is interpreted into a re-runnable scenario downstream via
the app manifest; for the corpus hash only the generic fields matter.

Ports to: a production indexer feeding Stage-2 sampling + the sealed Report
(plan Phase 5b). Single-chain (Base) keeps the surface small.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Generic AppIntentBase surface (contracts/src/interfaces/IAppIntentBase.sol).
_ORDER_COMPONENTS = [
    {"name": "orderId", "type": "bytes32"},
    {"name": "app", "type": "address"},
    {"name": "intentSelector", "type": "bytes4"},
    {"name": "intentParams", "type": "bytes"},
    {"name": "submittedBy", "type": "address"},
    {"name": "chainId", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "perpetual", "type": "bool"},
    {"name": "maxExecutions", "type": "uint256"},
    {"name": "cooldown", "type": "uint256"},
]
_CALL_COMPONENTS = [
    {"name": "target", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "callData", "type": "bytes"},
]
_PLAN_COMPONENTS = [
    {"name": "calls", "type": "tuple[]", "components": _CALL_COMPONENTS},
    {"name": "deadline", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "metadata", "type": "bytes"},
]
APPINTENT_ABI = [
    {"type": "function", "name": "executeIntent", "stateMutability": "payable", "outputs": [],
     "inputs": [
         {"name": "order", "type": "tuple", "components": _ORDER_COMPONENTS},
         {"name": "plan", "type": "tuple", "components": _PLAN_COMPONENTS},
         {"name": "userSignature", "type": "bytes"},
         {"name": "validatorSignatures", "type": "bytes[]"},
     ]},
    {"type": "event", "name": "IntentExecuted", "anonymous": False, "inputs": [
        {"name": "orderId", "type": "bytes32", "indexed": True},
        {"name": "submittedBy", "type": "address", "indexed": True},
        {"name": "score", "type": "uint256", "indexed": False},
        {"name": "planHash", "type": "bytes32", "indexed": False},
        {"name": "gasUsed", "type": "uint256", "indexed": False},
    ]},
]

_ORDER_KEYS = [c["name"] for c in _ORDER_COMPONENTS]


def _hexstr(b: Any) -> str:
    """Lowercase 0x-hex for bytes/addresses — one frozen casing so the hash is stable."""
    if isinstance(b, (bytes, bytearray)):
        return "0x" + bytes(b).hex()
    s = str(b)
    return s.lower() if s.startswith("0x") else s


def _normalize_order(order: Any) -> dict:
    """web3 may return a decoded tuple as a dict (named components) or a positional
    tuple — normalize to a dict keyed by component name."""
    if isinstance(order, dict):
        return order
    return dict(zip(_ORDER_KEYS, order))


def canonical_order(order: Any, *, score: int, block_number: int, tx_hash: Any,
                    chain_id: int) -> dict:
    """Byte-deterministic, PII-stripped filled-order record. ``submittedBy`` + the
    signatures are omitted (PII + not needed for Stage 2 — and chain omits exactly
    this in the event/calldata-stripped view). Ints are stringified so the JSON is
    big-int-safe and casing-stable."""
    o = _normalize_order(order)
    return {
        "order_id": _hexstr(o["orderId"]),
        "app_id": _hexstr(o["app"]),                 # the app contract address
        "intent_selector": _hexstr(o["intentSelector"]),
        "intent_params_hex": _hexstr(o["intentParams"]),
        "chain_id": int(chain_id),
        "deadline": str(int(o["deadline"])),
        "user_nonce": str(int(o["nonce"])),
        "perpetual": bool(o["perpetual"]),
        "max_executions": str(int(o["maxExecutions"])),
        "cooldown": str(int(o["cooldown"])),
        "status": "filled",
        "on_chain_score": int(score),
        "block_number": int(block_number),
        "tx_hash": _hexstr(tx_hash),
    }


def corpus_hash(records: list[dict]) -> str:
    """sha256 over the canonical corpus, sorted by (block_number, order_id) so the
    order of discovery doesn't matter — every honest node converges on one hash."""
    ordered = sorted(records, key=lambda r: (r["block_number"], r["order_id"]))
    blob = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def recover_corpus(w3, app_address: str, from_block: int, to_block: int | str = "latest",
                   chunk: int = 2000) -> list[dict]:
    """Scan ``IntentExecuted`` from ``app_address``, decode each tx's executeIntent
    calldata, return the canonical filled-order records. Paged to respect RPC
    log-range caps (halves the window on a range error)."""
    contract = w3.eth.contract(address=w3.to_checksum_address(app_address), abi=APPINTENT_ABI)
    chain_id = w3.eth.chain_id
    head = w3.eth.block_number if to_block == "latest" else int(to_block)
    records: list[dict] = []
    start = int(from_block)
    while start <= head:
        end = min(start + chunk - 1, head)
        try:
            evts = contract.events.IntentExecuted().get_logs(from_block=start, to_block=end)
        except Exception:
            if chunk > 1:
                chunk //= 2
                continue
            raise
        for e in evts:
            tx = w3.eth.get_transaction(e["transactionHash"])
            try:
                _func, params = contract.decode_function_input(tx["input"])
                order = params["order"]
            except Exception:
                continue  # not an executeIntent call (e.g. a multi-leg executeLeg) — skip
            records.append(canonical_order(
                order, score=e["args"]["score"], block_number=e["blockNumber"],
                tx_hash=e["transactionHash"], chain_id=chain_id))
        start = end + 1
    return records
