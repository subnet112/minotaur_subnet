"""Protocol fee policy — validator-controlled, the single source of truth.

The Minotaur protocol fee is computed HERE, in validator/daemon code, NOT in
the miner-controlled solver. The solver only produces an execution plan; we
measure that plan's gas via a real simulation and price the fee ourselves, so
miners cannot influence the fee (they cannot understate the gas of their own
plan — the same plan is simulated and executed).

Formula — identical on every chain; only the per-chain *values* differ::

    fee = min(cap, max(floor[chain], gas_used * gas_price_wei * (1 + margin)))

- ``floor[chain]`` — per-chain revenue floor in native-token wei (~$0.10
  equivalent), governance-tuned. Native-denominated, no oracle. On the chains
  we run (Base, BT EVM) gas is tiny, so the floor dominates and the fee is
  effectively a flat governance number there.
- ``margin`` — the universal tip over measured gas (default 50%). It is the
  ONLY buffer against gas-price drift between fee-lock and execution, because
  the relayer cannot refuse a quorum-approved order.
- ``cap`` — the on-chain ``maxPlatformFeeWei``; the fee is clamped down to it so
  the on-chain ``_clampFee`` can never revert on an over-cap fee.

Lifecycle:
  1. quote  — :func:`protocol_fee_wei` from a real sim's ``gas_used`` → locked
     into the user-signed order (the fee is a *computational param*; the plan
     is NOT pinned).
  2. consensus — each validator re-simulates and calls :func:`certify_fee`
     against its *own* measured gas before signing. This is where the
     never-lose-money guarantee lives (upstream of the relayer).
  3. relay — the relayer caps its gas bid at :func:`max_gas_price_wei` so a
     post-certification spike makes the tx pend rather than overpay.

One formula, one certification gate, one signature semantics — no per-mode
(USER/APP) and no per-chain branching anywhere.
"""

from __future__ import annotations

import os

from minotaur_subnet.chains import registry

# ---------------------------------------------------------------------------
# Per-chain revenue floor in native-token wei (~$0.10-equivalent).
#
# Governance-tuned, native-denominated (no price oracle); the per-chain values
# live in the chain registry (registry.fee_floor_wei). Re-tune at RUNTIME via env
# PROTOCOL_FEE_FLOOR_WEI_<chain_id> as token prices drift. Each value MUST lie
# within that chain's on-chain [minPlatformFeeWei, maxPlatformFeeWei] clamp.
#
# Defaults below assume ETH≈$3000, TAO≈$300 — adjust as needed; on Base/BT EVM
# this floor dominates the gas term and therefore IS the fee in practice.
# ---------------------------------------------------------------------------
# Universal tip over measured gas cost. 50% per the agreed design.
_DEFAULT_MARGIN_BPS = 5000

# Gas the executeIntent *wrapper* adds on top of the plan's own swap
# interactions: EIP-712 signature verification, ephemeral CREATE2 proxy
# deploy, protocol-fee settlement, on-chain output verification, token
# delivery. A plan-only simulation measures the swap calls but not this
# wrapper, so the quote adds this constant to the measured swap gas. It is
# app-agnostic (AppIntentBase framework overhead, same for every app) and
# daemon-controlled (NOT solver code), so it cannot be gamed by miners.
# Override via env PROTOCOL_FEE_FRAMEWORK_GAS.
_DEFAULT_FRAMEWORK_GAS = 400_000


def framework_overhead_gas() -> int:
    """executeIntent wrapper gas to add to a plan-only sim's measured gas."""
    override = _env_int("PROTOCOL_FEE_FRAMEWORK_GAS")
    return override if override is not None else _DEFAULT_FRAMEWORK_GAS

# On-chain clamp guardrails. These mirror the contract's constructor args
# (deployer.py: MIN_PLATFORM_FEE_WEI / MAX_PLATFORM_FEE_WEI). The cert refuses
# any fee outside [min, max] so the on-chain _clampFee can never revert.
_FALLBACK_MIN_FEE_WEI = 0
_FALLBACK_MAX_FEE_WEI = 10 ** 17  # 0.1 ETH


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def floor_wei(chain_id: int) -> int:
    """Per-chain revenue floor in native-token wei."""
    override = _env_int(f"PROTOCOL_FEE_FLOOR_WEI_{chain_id}")
    if override is not None:
        return override
    return registry.fee_floor_wei(chain_id)


def margin_bps(chain_id: int) -> int:
    """Tip over measured gas cost, in basis points (default 50%)."""
    per_chain = _env_int(f"PROTOCOL_FEE_MARGIN_BPS_{chain_id}")
    if per_chain is not None:
        return per_chain
    glob = _env_int("PROTOCOL_FEE_MARGIN_BPS")
    if glob is not None:
        return glob
    return _DEFAULT_MARGIN_BPS


def fee_min_wei(chain_id: int) -> int:
    """On-chain minPlatformFeeWei guardrail (lower clamp bound)."""
    per_chain = _env_int(f"MIN_PLATFORM_FEE_WEI_{chain_id}")
    if per_chain is not None:
        return per_chain
    glob = _env_int("MIN_PLATFORM_FEE_WEI")
    if glob is not None:
        return glob
    return _FALLBACK_MIN_FEE_WEI


def fee_cap_wei(chain_id: int) -> int:
    """On-chain maxPlatformFeeWei guardrail (upper clamp bound)."""
    per_chain = _env_int(f"MAX_PLATFORM_FEE_WEI_{chain_id}")
    if per_chain is not None:
        return per_chain
    glob = _env_int("MAX_PLATFORM_FEE_WEI")
    if glob is not None:
        return glob
    return _FALLBACK_MAX_FEE_WEI


def protocol_fee_wei(
    chain_id: int,
    gas_used: int,
    gas_price_wei: int,
    *,
    floor: int | None = None,
    margin: int | None = None,
) -> int:
    """Compute the binding protocol fee in native-token wei.

    ``fee = min(cap, max(floor, gas_used * gas_price_wei * (1 + margin)))``.
    """
    floor_v = floor if floor is not None else floor_wei(chain_id)
    margin_v = margin if margin is not None else margin_bps(chain_id)
    gas_cost = max(0, int(gas_used)) * max(0, int(gas_price_wei))
    with_margin = gas_cost + gas_cost * margin_v // 10000
    fee = max(floor_v, with_margin)
    # Never exceed the on-chain cap — an over-cap fee would revert in
    # _clampFee and burn the relayer's gas for nothing.
    return min(fee, fee_cap_wei(chain_id))


def certify_fee(
    chain_id: int,
    locked_fee_wei: int,
    gas_used: int,
    gas_price_wei: int,
) -> tuple[bool, str]:
    """Certify a locked fee against freshly measured gas. Returns (ok, reason).

    The never-lose-money + no-revert gate, run by every validator before it
    signs. Three conditions:
      * locked fee within the on-chain [min, max] clamp (else executeIntent
        reverts and the relayer eats the gas), and
      * locked fee covers the freshly measured gas cost (else the relayer
        submits at a loss / the tx can never be priced to include).
    Bare gas coverage is the hard floor; the margin baked into the locked fee
    is extra headroom, and the relayer's gas-price cap absorbs the
    certify→inclusion window.
    """
    locked = int(locked_fee_wei)
    gas_cost = max(0, int(gas_used)) * max(0, int(gas_price_wei))
    lo = fee_min_wei(chain_id)
    hi = fee_cap_wei(chain_id)
    if locked < lo:
        return False, f"fee {locked} below on-chain min {lo}"
    if locked > hi:
        return False, f"fee {locked} above on-chain max {hi}"
    if locked < gas_cost:
        return False, f"fee {locked} does not cover measured gas cost {gas_cost}"
    return True, ""


# Fallback gas prices in wei, used ONLY when the live RPC query fails. Deliberately
# conservative (≈ each chain's typical baseline); the per-chain values live in the
# chain registry (registry.fallback_gas_price_wei). Daemon-side twin of the solver's
# table — the fee math must not depend on miner code.


def _live_gas_rpc_url(chain_id: int) -> str:
    """RPC for the gas-price read — the operator's LIVE upstream, never the sim
    fork. The leader's quote and every follower's fee certification MUST price
    gas from the SAME live source: anvil reports a higher gas price than live
    Base, so a follower reading the fork rejects the (correctly floor-priced)
    fee as not-covering-gas (FEE_NOT_CERTIFIED). Mirrors the consensus caches.
    Falls back to the plain chain RPC for local/dev where "live" IS the node.
    """
    return registry.gas_rpc(chain_id)


# Cache Web3 instances by RPC URL so the gas-price read doesn't reconnect each call.
_GAS_W3_CACHE: dict = {}


def _gas_w3(url: str):
    w3 = _GAS_W3_CACHE.get(url)
    if w3 is None:
        from minotaur_subnet.blockchain.web3_retry import build_retrying_web3
        w3 = build_retrying_web3(url)
        _GAS_W3_CACHE[url] = w3
    return w3


def current_gas_price_wei(chain_id: int) -> int:
    """Live gas price for a chain, with a conservative per-chain fallback.

    Used by the quote (to price the fee) and by certification (to re-check
    coverage). MUST read from the LIVE chain (operator's upstream), not the sim
    fork — otherwise the leader (live) and followers (fork) price gas
    differently and followers reject the fee (FEE_NOT_CERTIFIED). Queried
    daemon-side via web3 — never from solver code.
    """
    url = _live_gas_rpc_url(chain_id)
    try:
        if url:
            price = int(_gas_w3(url).eth.gas_price)
            if price > 0:
                return price
        else:
            # No upstream configured (local/dev) — fall back to the chain RPC.
            from minotaur_subnet.blockchain.chains import get_web3
            w3 = get_web3(chain_id)
            if w3 is not None:
                price = int(w3.eth.gas_price)
                if price > 0:
                    return price
    except Exception:
        pass
    return registry.fallback_gas_price_wei(chain_id)


def max_gas_price_wei(locked_fee_wei: int, gas_units: int) -> int:
    """Cap the relayer's gas bid so total spend can't exceed the locked fee.

    ``max_price = locked_fee // gas_units``. Bidding at most this price means
    the relayer pays at most ``gas_units * max_price ≤ locked_fee``. A
    post-certification gas spike above this makes the tx pend (it still gets
    submitted — no refusal) rather than execute at a loss. Returns 0 for a
    non-positive gas estimate (caller should treat 0 as "no cap available").
    """
    g = int(gas_units)
    if g <= 0:
        return 0
    return int(locked_fee_wei) // g
