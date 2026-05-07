"""Backward-compatibility shim for function selector constants.

The encoding functions (encode_approve, encode_exact_input_single, etc.)
have been moved to the solver repo where miners own them. The platform
uses sdk/selectors.py for the constants it needs.

This file re-exports selectors so existing imports don't break.
"""

# Re-export selectors from the canonical location.
from minotaur_subnet.sdk.selectors import (  # noqa: F401
    APPROVE_SELECTOR,
    EXACT_INPUT_SINGLE_SELECTOR_V1,
    EXACT_INPUT_SINGLE_SELECTOR_V2,
    EXACT_INPUT_SINGLE_SELECTOR,
    EXACT_INPUT_SELECTOR,
    SWAP_ROUTER_V2_CHAINS,
)
