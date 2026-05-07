"""Solver implementations package.

DEX-specific solver implementations (BaselineSwapSolver, SwapIntentProcessor,
TWAP, Rebalance, etc.) have been moved to the solver repo where miners can
modify them directly. The SDK now only provides the abstract interfaces:

- IntentSolver (sdk/intent_solver.py)
- Strategy (sdk/strategy.py)
- IntentProcessor (sdk/intent_processor.py)

See https://github.com/subnet112/minotaur-solver for the reference
implementations.
"""
