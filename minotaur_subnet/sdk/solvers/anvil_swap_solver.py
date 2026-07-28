"""Anvil swap solver — generates real swap calldata for E2E testing.

Produces ExecutionPlans targeting a TestSwapRouter deployed on Anvil.
The router.swapExact(outputToken, outputAmount, recipient) call mints
output tokens to the recipient, simulating a real swap.

Usage:
    solver = AnvilSwapSolver()
    solver.initialize({
        "router_address": "0x...",
        "weth_address": "0x...",
        "usdc_address": "0x...",
    })
    plan = solver.generate_plan(app, state, snapshot)
"""

from __future__ import annotations

import time
from typing import Any

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak

from minotaur_subnet.shared.types import (
    AppIntentDefinition,
    ExecutionPlan,
    Interaction,
    IntentState,
)
from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
from minotaur_subnet.v3.manifest import IntentManifest, _intent_field_default


def _state_params(state: IntentState) -> dict[str, Any]:
    typed = getattr(state, "typed_context", None)
    if typed is not None:
        raw = getattr(typed, "raw_params", None)
        if isinstance(raw, dict):
            return raw
    return state.raw_params_view()



# ── Swap param normalisation (moved here from v3/manifest.py) ───────────────
#
# These interpret SWAP params — input_token/tokenIn/token_in, amountPerBuy,
# min_output_amount, fee tiers, permit fields. That is this app's vocabulary,
# not the platform's, so it belongs with the swap solver rather than in a
# module every app's manifest goes through. _intent_field_default stays in
# v3/manifest (it reads a declared default for ANY field, of any app).

def canonical_swap_receiver_field(
    manifest: IntentManifest | None,
    *,
    intent_name: str = "swap",
) -> str:
    """Return the manifest-preferred receiver-like field name for swap intents."""
    if manifest is None:
        return "receiver"
    intent = manifest.get_intent(intent_name)
    if intent is None:
        return "receiver"
    field_names = {field.name for field in intent.params}
    if "receiver" in field_names:
        return "receiver"
    if "recipient" in field_names:
        return "recipient"
    return "receiver"


def _resolve_swap_receiver(
    params: dict[str, Any],
    *,
    canonical_field: str,
    receiver_default: str,
) -> str:
    if canonical_field == "recipient":
        return (
            params.get("recipient")
            or params.get("receiver")
            or receiver_default
        )
    return (
        params.get("receiver")
        or params.get("recipient")
        or receiver_default
    )


def normalize_swap_intent_params(
    params: dict[str, Any],
    *,
    manifest: IntentManifest | None = None,
    intent_name: str = "swap",
    receiver_default: str = "",
    slippage_bps: int | None = None,
) -> dict[str, Any]:
    """Normalize swap params to one runtime shape using manifest hints when present.

    Supports aliases from DCA-style params (tokenIn/tokenOut/amountPerBuy) and
    yield-style params (asset/amount) so the baseline solver can handle multiple
    app types without separate strategies.
    """
    input_token = (
        params.get("input_token", "")
        or params.get("tokenIn", "")
        or params.get("token_in", "")
    )
    output_token = (
        params.get("output_token", "")
        or params.get("tokenOut", "")
        or params.get("token_out", "")
    )

    input_amount_raw = (
        params.get("input_amount", 0)
        or params.get("amountPerBuy", 0)
        or params.get("amount_per_buy", 0)
        or params.get("amount", 0)
    )
    input_amount = int(input_amount_raw or 0)

    min_output_raw = (
        params.get("min_output_amount")
        or params.get("output_amount")
        or params.get("minAmountOut")
        or params.get("min_amount_out")
    )
    if min_output_raw not in (None, ""):
        min_output_amount = int(min_output_raw)
    elif slippage_bps is not None and input_amount > 0:
        min_output_amount = input_amount * (10000 - slippage_bps) // 10000
    else:
        min_output_amount = 0

    receiver_field = canonical_swap_receiver_field(manifest, intent_name=intent_name)
    receiver = _resolve_swap_receiver(
        params,
        canonical_field=receiver_field,
        receiver_default=receiver_default,
    )

    fee_tier = params.get(
        "fee_tier",
        _intent_field_default(manifest, intent_name, "fee_tier", 3000),
    )
    permit_deadline = params.get(
        "permit_deadline",
        _intent_field_default(manifest, intent_name, "permit_deadline", 0),
    )
    permit_v = params.get(
        "permit_v",
        _intent_field_default(manifest, intent_name, "permit_v", 0),
    )
    permit_r = params.get(
        "permit_r",
        _intent_field_default(manifest, intent_name, "permit_r", "0x" + "00" * 32),
    )
    permit_s = params.get(
        "permit_s",
        _intent_field_default(manifest, intent_name, "permit_s", "0x" + "00" * 32),
    )

    return {
        "input_token": input_token,
        "output_token": output_token,
        "input_amount": input_amount,
        "min_output_amount": min_output_amount,
        "receiver": receiver,
        "receiver_field": receiver_field,
        "fee_tier": int(fee_tier or 3000),
        "permit_deadline": int(permit_deadline or 0),
        "permit_v": int(permit_v or 0),
        "permit_r": permit_r,
        "permit_s": permit_s,
    }


class AnvilSwapSolver(IntentSolver):
    """Solver for testing against Anvil-deployed contracts.

    Generates real swapExact() calldata targeting the TestSwapRouter.
    """

    def __init__(self) -> None:
        self.router = ""
        self.weth = ""
        self.usdc = ""

    def initialize(self, config: dict[str, Any]) -> None:
        self.router = config.get("router_address", "")
        self.weth = config.get("weth_address", "")
        self.usdc = config.get("usdc_address", "")

    def generate_plan(
        self,
        intent: AppIntentDefinition,
        state: IntentState,
        snapshot: MarketSnapshot,
    ) -> ExecutionPlan:
        """Build a real swapExact() call targeting the test router."""
        params = normalize_swap_intent_params(
            _state_params(state),
            receiver_default=state.contract_address or state.owner,
        )
        output_token = params.get("output_token", self.usdc) or self.usdc
        output_amount = params.get("min_output_amount", 0) or 1_800_000_000
        # DexAggregatorApp measures gained output on the app contract, then
        # forwards tokens to the final receiver itself after fee accounting.
        recipient = state.contract_address or state.owner

        # Encode swapExact(address outputToken, uint256 outputAmount, address recipient)
        selector = keccak(b"swapExact(address,uint256,address)")[:4]
        args = abi_encode(
            ["address", "uint256", "address"],
            [output_token, output_amount, recipient],
        )
        calldata = "0x" + (selector + args).hex()

        return ExecutionPlan(
            intent_id=intent.app_id,
            interactions=[
                Interaction(
                    target=self.router,
                    value="0",
                    call_data=calldata,
                    chain_id=state.chain_id,
                ),
            ],
            deadline=int(time.time()) + 300,
            nonce=state.nonce,
            metadata={"solver": "anvil-swap-solver"},
        )

    def metadata(self) -> SolverMetadata:
        return SolverMetadata(
            name="anvil-swap-solver",
            version="1.0.0",
            author="test",
            description="Test solver for Anvil E2E testing",
            supported_chains=[31337],
            supported_intent_types=["swap"],
        )
