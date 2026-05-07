"""Agentic solver development loop.

This package implements the miner-side agent that:
1. Discovers active apps from the validator
2. Monitors per-app scores
3. Uses an LLM to generate/improve per-app strategies
4. Tests strategies locally before submission
5. Bundles strategies into a RoutingSolver and submits to the validator
"""
