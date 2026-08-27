"""A native-sentinel OUTPUT resolves on the DESTINATION chain, not the source.

``_resolve_token_params`` took ONE ``chain_id`` and applied it to both tokens.
For a cross-chain order asking to deliver native on chain 964 that produced two
compounding errors:

1. the output resolved to the SOURCE chain's wrapped native (WETH on Ethereum)
   — a different asset on a different chain, and
2. ``token_chains["output_token"]`` recorded the source chain too, so
   ``input_chain == output_chain`` and the cross-chain branch never fired: the
   order was routed as single-chain.

The sentinel carries no chain of its own, so an explicit ``dest_chain_id`` is
the only available signal for the output side.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.orders import _resolve_token_params  # noqa: E402

_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_WTAO_964 = "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81"


def test_native_output_resolves_on_destination_chain():
    out = _resolve_token_params(
        {"input_token": _USDC_ETH, "output_token": _NATIVE, "dest_chain_id": "964"},
        1,
    )
    assert out["output_token"].lower() == _WTAO_964.lower()
    assert out["output_token"].lower() != _WETH_ETH.lower()


def test_native_output_keeps_cross_chain_detectable():
    """The second half: routing must still see this as cross-chain."""
    out = _resolve_token_params(
        {"input_token": _USDC_ETH, "output_token": _NATIVE, "dest_chain_id": "964"},
        1,
    )
    assert out.get("output_chain_id") == 964 or str(out.get("dest_chain_id")) == "964"
    assert int(out.get("input_chain_id", 1)) == 1


def test_single_chain_native_output_unchanged():
    """No dest_chain_id => the order's own chain, exactly as before."""
    out = _resolve_token_params({"input_token": _USDC_ETH, "output_token": _NATIVE}, 1)
    assert out["output_token"].lower() == _WETH_ETH.lower()
    assert out.get("dest_chain_id") is None


def test_native_input_still_uses_order_chain():
    """Only the OUTPUT side takes dest_chain_id; the input is on the order chain."""
    out = _resolve_token_params(
        {"input_token": _NATIVE, "output_token": _USDC_ETH, "dest_chain_id": "964"},
        1,
    )
    assert out["input_token"].lower() == _WETH_ETH.lower()


def test_garbage_dest_chain_falls_back_rather_than_raising():
    out = _resolve_token_params(
        {"input_token": _USDC_ETH, "output_token": _NATIVE, "dest_chain_id": "not-a-chain"},
        1,
    )
    assert out["output_token"].lower() == _WETH_ETH.lower()
