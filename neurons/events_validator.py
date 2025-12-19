"""Validation utilities for aggregator events."""
from __future__ import annotations

import base64
import datetime as dt
import math
from typing import Dict, Any, List, Tuple, Set, Optional

import bittensor as bt

from .exceptions import EventValidationError


def _parse_iso(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _canonical_submission_string(event_type: str, event_id: str, response_ts: str, price: Any, size: Any) -> str:
    return "submission\n{type}\n{id}\n{ts}\n{price}\n{size}".format(
        type=event_type,
        id=event_id,
        ts=response_ts,
        price=price,
        size=size,
    )


def _verify_signature(hotkey: str, message: str, signature_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64)
        keypair = bt.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(message=message.encode("utf-8"), signature=sig))
    except Exception:
        return False


def validate_events(
    raw_events: List[Dict[str, Any]],
    allowed_hotkeys: Set[str],
    logger,
    *,
    default_ttl_ms: Optional[int] = None,
    max_response_latency_ms: Optional[int] = None,
    max_clock_skew_seconds: int = 1,
    min_price: float = 0.0,
    max_price: float = 0.0,
    min_size: float = 0.0,
    max_size: float = 0.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Validate and sanitize events.

    Args:
        raw_events: List of raw event dictionaries from the aggregator.
        allowed_hotkeys: Set of hotkeys currently active on the subnet.
        logger: Logger for warnings.
        default_ttl_ms: Default TTL in milliseconds if not provided per event.
        max_response_latency_ms: Hard cap on latency between request and response.
        max_clock_skew_seconds: Allowable negative latency window.
        min_price/max_price: Bounds for price validation (0 disables upper bound).
        min_size/max_size: Bounds for size validation (0 disables upper bound).

    Returns:
        (validated_events, stats)
    """

    validated: List[Dict[str, Any]] = []
    stats = {
        "total_events": 0,
        "valid_events": 0,
        "valid_submissions": 0,
        "dropped_events": 0,
        "dropped_submissions": 0,
        "signature_failures": 0,
        "unknown_hotkeys": 0,
        "ttl_violations": 0,
        "latency_violations": 0,
        "clock_skew_violations": 0,
        "price_bounds": 0,
        "size_bounds": 0,
    }

    seen_event_ids: Set[str] = set()
    clock_skew_ms = max(0, int(max_clock_skew_seconds * 1000))

    for event in raw_events:
        stats["total_events"] += 1
        try:
            event_type = str(event.get("type"))
            event_id = str(event.get("id"))
            request_ts_raw = str(event.get("request_ts"))
        except Exception:
            stats["dropped_events"] += 1
            continue

        if not event_type or not event_id or event_id in seen_event_ids:
            stats["dropped_events"] += 1
            continue

        try:
            request_ts = _parse_iso(request_ts_raw)
        except Exception:
            logger.warning(f"Invalid request_ts for event {event_id}")
            stats["dropped_events"] += 1
            continue

        constraints = event.get("context", {}).get("constraints", {}) if isinstance(event.get("context"), dict) else {}
        ttl_ms = None
        try:
            ttl_candidate = constraints.get("ttl_ms")
            ttl_ms = int(ttl_candidate) if ttl_candidate is not None else None
        except Exception:
            ttl_ms = None
        if ttl_ms is None:
            ttl_ms = default_ttl_ms
        if max_response_latency_ms is not None and (ttl_ms is None or ttl_ms > max_response_latency_ms):
            ttl_ms = max_response_latency_ms

        submissions = event.get("submissions") or []
        valid_submissions: List[Dict[str, Any]] = []
        seen_hotkeys: Set[str] = set()

        for sub in submissions:
            hotkey = sub.get("hotkey")
            sig = sub.get("signature")
            response_ts_raw = sub.get("response_ts")
            if not isinstance(hotkey, str) or hotkey in seen_hotkeys:
                stats["dropped_submissions"] += 1
                continue
            if allowed_hotkeys and hotkey not in allowed_hotkeys:
                stats["unknown_hotkeys"] += 1
                stats["dropped_submissions"] += 1
                continue
            if not isinstance(response_ts_raw, str):
                stats["dropped_submissions"] += 1
                continue

            try:
                response_ts = _parse_iso(response_ts_raw)
            except Exception:
                stats["dropped_submissions"] += 1
                continue

            delta_ms = (response_ts - request_ts).total_seconds() * 1000.0
            if delta_ms < -clock_skew_ms:
                stats["clock_skew_violations"] += 1
                stats["dropped_submissions"] += 1
                continue
            if delta_ms < 0:
                # within skew allowance
                delta_ms = 0.0

            if ttl_ms is not None and delta_ms > ttl_ms:
                stats["ttl_violations"] += 1
                stats["dropped_submissions"] += 1
                continue
            if max_response_latency_ms is not None and delta_ms > max_response_latency_ms:
                stats["latency_violations"] += 1
                stats["dropped_submissions"] += 1
                continue

            price = sub.get("price")
            size = sub.get("size")
            try:
                price_val = float(price)
                if not math.isfinite(price_val):
                    raise ValueError
            except Exception:
                stats["price_bounds"] += 1
                stats["dropped_submissions"] += 1
                continue
            if price_val <= 0 or (min_price > 0 and price_val < min_price) or (max_price > 0 and price_val > max_price):
                stats["price_bounds"] += 1
                stats["dropped_submissions"] += 1
                continue

            try:
                size_val = float(size)
                if not math.isfinite(size_val):
                    raise ValueError
            except Exception:
                stats["size_bounds"] += 1
                stats["dropped_submissions"] += 1
                continue
            if size_val <= 0 or (min_size > 0 and size_val < min_size) or (max_size > 0 and size_val > max_size):
                stats["size_bounds"] += 1
                stats["dropped_submissions"] += 1
                continue

            if not isinstance(sig, str):
                stats["dropped_submissions"] += 1
                continue

            message = _canonical_submission_string(event_type, event_id, response_ts_raw, price, size)
            if not _verify_signature(hotkey, message, sig):
                stats["signature_failures"] += 1
                stats["dropped_submissions"] += 1
                continue

            seen_hotkeys.add(hotkey)
            valid_submissions.append(sub)

        if not valid_submissions:
            stats["dropped_events"] += 1
            continue

        event_copy = dict(event)
        event_copy["submissions"] = valid_submissions
        validated.append(event_copy)
        seen_event_ids.add(event_id)
        stats["valid_events"] += 1
        stats["valid_submissions"] += len(valid_submissions)

    return validated, stats


