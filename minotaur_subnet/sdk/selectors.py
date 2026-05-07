"""Well-known EVM function selectors used by the platform for plan assessment.

These are protocol-level constants that the platform needs for safety
classification (v3/classifier.py). They are NOT solver-specific — solvers
may use any DEX or protocol. The platform just needs to recognize common
interaction patterns for policy enforcement.

Solvers that need encoding helpers (encode_approve, encode_exact_input_single,
etc.) should use their own local implementations — those functions are
DEX-specific and belong in the solver repo, not the platform SDK.
"""

# ERC-20 approve(address,uint256)
APPROVE_SELECTOR = bytes.fromhex("095ea7b3")

# Uniswap V3 SwapRouter V1 — exactInputSingle with deadline
EXACT_INPUT_SINGLE_SELECTOR_V1 = bytes.fromhex("414bf389")

# Uniswap V3 SwapRouter V2 (SwapRouter02) — exactInputSingle WITHOUT deadline
EXACT_INPUT_SINGLE_SELECTOR_V2 = bytes.fromhex("04e45aaf")

# Default alias
EXACT_INPUT_SINGLE_SELECTOR = EXACT_INPUT_SINGLE_SELECTOR_V1

# Uniswap V3 SwapRouter.exactInput(ExactInputParams)
EXACT_INPUT_SELECTOR = bytes.fromhex("c04b8d59")

# Chains that use SwapRouter02 (V2 encoding, no deadline param)
SWAP_ROUTER_V2_CHAINS = {8453, 10, 42161}
