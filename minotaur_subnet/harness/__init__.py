"""Benchmarking harness for IntentSolver submissions.

This package handles the communication between the validator (host) and
solver containers:

- protocol: Shared message types for JSON-over-stdin/stdout communication
- runner: Runs inside solver containers, dispatches commands to IntentSolver
- orchestrator: Runs on the host, manages containers and collects results
"""
