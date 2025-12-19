"""Custom exception hierarchy for validator operations.

Provides specific exception types for different failure modes to enable
better error handling and debugging.
"""
from __future__ import annotations


class ValidatorException(Exception):
    """Base exception for all validator errors."""
    pass


class ConfigurationException(ValidatorException):
    """Configuration validation error."""
    pass


class AggregatorException(ValidatorException):
    """Aggregator communication error."""
    pass


class EventValidationError(ValidatorException):
    """Event validation error."""
    pass


class WeightEmissionException(ValidatorException):
    """Weight emission error."""
    pass


class MetagraphSyncException(ValidatorException):
    """Metagraph synchronization error."""
    pass


class WindowPlannerError(ValidatorException):
    """Window planner error (epoch timing, block queries)."""
    pass

