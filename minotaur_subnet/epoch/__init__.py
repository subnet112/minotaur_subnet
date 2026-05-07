"""Epoch management — solver lifecycle and champion adoption."""

from .clock import SolverRoundEpochClock
from .manager import EpochManager

__all__ = ["EpochManager", "SolverRoundEpochClock"]
