"""Validation utilities for events and weights data.

Provides helper functions to validate data received from the aggregator
before processing and weight computation.
"""
from __future__ import annotations

import math
from typing import Dict, Set, Optional, Any
import bittensor as bt


def validate_events_response(
    events: Any,
    allowed_hotkeys: Optional[Set[str]] = None
) -> bool:
    """Validate events response structure from aggregator.
    
    Args:
        events: Events list from aggregator
        allowed_hotkeys: Optional set of allowed hotkey addresses
        
    Returns:
        True if validation passes, False otherwise
    """
    if not isinstance(events, list):
        bt.logging.warning("Events response is not a list", prefix="VALIDATION")
        return False
    
    if len(events) == 0:
        bt.logging.debug("Empty events list (may be valid for quiet periods)", prefix="VALIDATION")
        return True
    
    # Validate a sample of events (first few)
    sample_size = min(5, len(events))
    for i, event in enumerate(events[:sample_size]):
        if not isinstance(event, dict):
            bt.logging.warning(f"Event {i} is not a dictionary", prefix="VALIDATION")
            return False
        
        # Check required fields
        if "type" not in event or "id" not in event:
            bt.logging.warning(f"Event {i} missing required fields", prefix="VALIDATION")
            return False
        
        # Validate submissions if present
        submissions = event.get("submissions", [])
        if submissions and not isinstance(submissions, list):
            bt.logging.warning(f"Event {i} submissions not a list", prefix="VALIDATION")
            return False
        
        # If checking hotkeys, validate they're in allowed set
        if allowed_hotkeys and submissions:
            for sub in submissions:
                hotkey = sub.get("hotkey")
                if hotkey and hotkey not in allowed_hotkeys:
                    bt.logging.debug(
                        f"Event {i} contains unknown hotkey: {hotkey[:8]}...",
                        prefix="VALIDATION"
                    )
    
    return True


def validate_weights_dict(
    weights_mapping: Dict[str, float],
    allowed_hotkeys: Set[str],
    tolerance: float = 0.01
) -> bool:
    """Validate computed weights before emission.
    
    Args:
        weights_mapping: Dict mapping hotkey to weight value
        allowed_hotkeys: Set of hotkeys currently active on subnet
        tolerance: Tolerance for normalization check (default 1%)
        
    Returns:
        True if validation passes, False otherwise
    """
    if not weights_mapping:
        bt.logging.warning("Empty weights mapping", prefix="VALIDATION")
        return False
    
    # Check all hotkeys are allowed
    unknown_hotkeys = set(weights_mapping.keys()) - allowed_hotkeys
    if unknown_hotkeys:
        bt.logging.warning(
            f"Weights contain {len(unknown_hotkeys)} unknown hotkeys",
            prefix="VALIDATION"
        )
        # Filter out unknown hotkeys
        for hotkey in unknown_hotkeys:
            bt.logging.debug(f"Removing unknown hotkey: {hotkey[:8]}...", prefix="VALIDATION")
            del weights_mapping[hotkey]
        
        if not weights_mapping:
            bt.logging.warning("No valid weights after filtering", prefix="VALIDATION")
            return False
    
    # Validate weight values
    for hotkey, weight in list(weights_mapping.items()):
        # Type check
        if not isinstance(weight, (int, float)):
            bt.logging.warning(f"Invalid weight type for {hotkey[:8]}...: {type(weight)}", prefix="VALIDATION")
            return False
        
        # Bounds check
        if not (0 <= weight <= 1):
            bt.logging.warning(f"Weight out of bounds for {hotkey[:8]}...: {weight}", prefix="VALIDATION")
            return False
        
        # Finite check
        if not math.isfinite(weight):
            bt.logging.warning(f"Non-finite weight for {hotkey[:8]}...", prefix="VALIDATION")
            return False
    
    # Check normalization
    total = sum(weights_mapping.values())
    if total <= 0:
        bt.logging.warning("All weights are zero", prefix="VALIDATION")
        return False
    
    if not (1.0 - tolerance <= total <= 1.0 + tolerance):
        bt.logging.warning(
            f"Weights not normalized: sum={total:.6f}",
            prefix="VALIDATION",
            suffix=f"tolerance={tolerance}"
        )
        # Auto-normalize if off by a small amount
        if abs(total - 1.0) < 0.1:
            bt.logging.info("Auto-normalizing weights", prefix="VALIDATION")
            for hotkey in weights_mapping:
                weights_mapping[hotkey] /= total
            return True
        return False
    
    bt.logging.debug(
        f"Weights validation passed: {len(weights_mapping)} miners, sum={total:.6f}",
        prefix="VALIDATION"
    )
    return True


def sanitize_hotkey(hotkey: Any) -> Optional[str]:
    """Sanitize and validate hotkey format.
    
    Args:
        hotkey: Hotkey string to validate
        
    Returns:
        Sanitized hotkey string or None if invalid
    """
    if not isinstance(hotkey, str):
        return None
    
    hotkey = hotkey.strip()
    
    # Basic SS58 format check (Bittensor addresses)
    if len(hotkey) < 40 or len(hotkey) > 50:
        return None
    
    # Check for valid base58 characters
    valid_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not all(c in valid_chars for c in hotkey):
        return None
    
    return hotkey


def validate_metrics_dict(metrics: Dict[str, Dict[str, float]]) -> bool:
    """Validate per-hotkey metrics dictionary.
    
    Args:
        metrics: Dict mapping hotkey to metrics dict
        
    Returns:
        True if validation passes, False otherwise
    """
    if not isinstance(metrics, dict):
        bt.logging.warning("Metrics is not a dictionary", prefix="VALIDATION")
        return False
    
    if not metrics:
        bt.logging.warning("Empty metrics dictionary", prefix="VALIDATION")
        return False
    
    # Validate structure
    for hotkey, hotkey_metrics in metrics.items():
        if not isinstance(hotkey_metrics, dict):
            bt.logging.warning(f"Metrics for {hotkey[:8]}... not a dict", prefix="VALIDATION")
            return False
        
        # Check for expected metric fields
        expected_fields = {"participations", "wins", "filled_notional", "p95_latency_ms", "reverts"}
        present_fields = set(hotkey_metrics.keys())
        
        if not present_fields.intersection(expected_fields):
            bt.logging.warning(
                f"Metrics for {hotkey[:8]}... missing expected fields",
                prefix="VALIDATION"
            )
            return False
        
        # Validate metric values are numeric
        for field, value in hotkey_metrics.items():
            if not isinstance(value, (int, float)):
                bt.logging.warning(
                    f"Invalid metric value for {hotkey[:8]}.../{field}: {type(value)}",
                    prefix="VALIDATION"
                )
                return False
            
            if not math.isfinite(value):
                bt.logging.warning(
                    f"Non-finite metric for {hotkey[:8]}.../{field}",
                    prefix="VALIDATION"
                )
                return False
    
    return True

