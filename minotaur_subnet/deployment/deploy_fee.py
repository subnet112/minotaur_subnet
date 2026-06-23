"""App DEPLOYMENT fee + the public-deployment gate (#238).

Distinct from the EXECUTION-time protocol fee (``fee_policy.py``): this is a
one-time fee charged when an App is DEPLOYED, compensating the miner work to solve
a newly-deployed App (new ground miners have to solve). Today deployment is
admin-only and free (the relayer fronts gas); this module adds the fee QUOTE +
config + the HARD GATE that keeps public/3rd-party deployment closed until the
on-chain fee COLLECTION is wired.

Scope (#238, per the product decision): quote + config + gate here. The actual
collection + routing (developer pays the fee -> miner-compensation / subnet) is
deliberately NOT here yet — it needs the public-deploy API (Phase 6 "Third-Party
Apps") + a payment mechanism. ``require_deployment_authorized`` blocks public
deploys until then, so the fee can never be silently bypassed when that API lands.
"""

from __future__ import annotations

import os

# Default deploy fee in TAO. Compensates the miner work to solve a new App.
# Governance-tunable: ``DEPLOY_FEE_TAO`` env now; on-chain governance later.
DEFAULT_DEPLOY_FEE_TAO = 0.5
RAO_PER_TAO = 1_000_000_000  # 1 TAO = 1e9 RAO

# Coarse upper-bound gas to deploy an App's contract (AppIntentBase + the app),
# used only for the quote. The real deploy uses the actual tx gas. Override via
# ``DEPLOY_GAS_ESTIMATE``.
DEFAULT_DEPLOY_GAS_ESTIMATE = 3_000_000

_PUBLIC_DEPLOY_ON = frozenset({"1", "true", "yes", "on"})


def deploy_fee_tao() -> float:
    """The App deployment fee in TAO (default 0.5; ``DEPLOY_FEE_TAO`` override)."""
    raw = os.environ.get("DEPLOY_FEE_TAO")
    if raw is None:
        return DEFAULT_DEPLOY_FEE_TAO
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DEPLOY_FEE_TAO


def deploy_fee_rao() -> int:
    """The deployment fee in RAO (1 TAO = 1e9 RAO) for integer on-chain math."""
    return int(round(deploy_fee_tao() * RAO_PER_TAO))


def deploy_gas_estimate() -> int:
    """Estimated gas to deploy an App's contract (``DEPLOY_GAS_ESTIMATE`` override)."""
    raw = os.environ.get("DEPLOY_GAS_ESTIMATE")
    if raw is None:
        return DEFAULT_DEPLOY_GAS_ESTIMATE
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_DEPLOY_GAS_ESTIMATE


def public_deployment_enabled() -> bool:
    """Whether PUBLIC (non-admin / 3rd-party) App deployment is enabled. **DEFAULT OFF.**

    The hard gate of #238: this MUST stay off until the deploy-fee collection is
    live — otherwise opening the public App API would let anyone deploy for free,
    burning relayer gas and skipping the miner-work compensation. Enable
    (``ENABLE_PUBLIC_DEPLOYMENT=1``) only once collection + routing are wired.
    """
    return os.environ.get("ENABLE_PUBLIC_DEPLOYMENT", "").strip().lower() in _PUBLIC_DEPLOY_ON


def quote_deployment(
    chains: list[int],
    gas_price_wei_by_chain: dict[int, int] | None = None,
) -> dict:
    """Deployment quote: estimated gas per targeted chain + the deploy fee (#238).

    ``gas_price_wei_by_chain`` (optional, injected by the caller from live RPC):
    chains with a price get a ``gas_cost_wei``; others get the gas estimate only.
    Gas is fronted by the relayer today — the cost is informational until public
    deployment + fee collection are live.
    """
    prices = gas_price_wei_by_chain or {}
    est = deploy_gas_estimate()
    gas: dict[str, dict] = {}
    for c in chains:
        entry: dict = {"estimated_gas": est}
        gp = prices.get(c)
        if gp is not None:
            entry["gas_price_wei"] = int(gp)
            entry["gas_cost_wei"] = est * int(gp)
        gas[str(c)] = entry
    return {
        "gas": gas,
        "deploy_fee_tao": deploy_fee_tao(),
        "deploy_fee_rao": deploy_fee_rao(),
        "fee_collection_enabled": public_deployment_enabled(),
        "note": (
            "Deploy fee compensates the miner work to solve the new App. Gas is "
            "fronted by the relayer today; the fee is NOT yet collected — public "
            "deployment is gated off until collection is live (#238)."
        ),
    }


class DeploymentFeeRequired(Exception):
    """A deployment was refused by the #238 public-deployment / fee gate."""


def require_deployment_authorized(*, is_admin: bool, fee_paid: bool = False) -> None:
    """Hard gate (#238). Admin deploys pass (free, as today). A PUBLIC/3rd-party
    deploy is REFUSED unless public deployment is enabled AND the deploy fee was
    paid. Collection is not wired yet, so ``fee_paid`` is always False for public
    callers — public deployment is structurally blocked until collection lands.
    """
    if is_admin:
        return
    if not public_deployment_enabled():
        raise DeploymentFeeRequired(
            "Public/3rd-party App deployment is disabled — it cannot be enabled "
            "until the deploy-fee collection (#238) is live."
        )
    if not fee_paid:
        raise DeploymentFeeRequired(
            f"App deployment requires the {deploy_fee_tao()} TAO deploy fee to be "
            "paid before deploying."
        )
