"""Deploy-fee payment authorization (builds on developer_auth + #238).

#238 added the deploy-fee QUOTE + config + the hard gate that keeps public
deployment closed until collection is live. This module adds the *authorization*
layer that produces the ``fee_paid`` the gate consumes: a developer proves they
own the app (an EIP-712 ``pay_deploy_fee`` signature from the app's ``deployer``,
reusing the ``developer_auth`` primitive) and that the fee was actually paid
on-chain.

What is built here: the EIP-712 authorization binding
``(action=pay_deploy_fee, app_id, payment_ref, chain_id, amount)``, the
single-use nonce consume (shared with the other developer actions, so a nonce
can't be replayed across actions), and the plumbing into ``deploy_app_intent``.

The on-chain payment check itself lives behind :class:`PaymentVerifier`; the
rail is always finney (native TAO) — see ``finney_payment``. The structural #238
block holds until ``ENABLE_PUBLIC_DEPLOYMENT=1`` *and* the verifier's config
(collector + the app's linked coldkey) is in place. Fee routing is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from eth_hash.auto import keccak


@dataclass(frozen=True)
class DeployFeePayment:
    """A developer's claim that the deploy fee was paid for an app.

    ``payment_ref`` is the on-chain payment reference (e.g. a tx hash) the
    verifier resolves; ``signature`` is the deployer's EIP-712 ``pay_deploy_fee``
    authorization binding it to the app, chain, and amount.
    """

    payment_ref: str
    nonce: int
    deadline: int
    signature: str


class PaymentVerifier(Protocol):
    """Confirms an on-chain deploy-fee payment. Implementations are pluggable so
    the EVM (wTAO) and finney (native TAO) rails can be added without touching
    the authorization flow."""

    def verify(
        self,
        *,
        store: Any,
        app_id: str,
        deployer: str,
        payment_ref: str,
        chain_id: int,
        amount_rao: int,
    ) -> tuple[bool, str]:
        """Return ``(ok, error)``: did the app's payer pay at least
        ``amount_rao``, referenced by ``payment_ref``, to the deploy-fee
        collector on ``chain_id``? Implementations own consume-once of the
        payment (``store.consume_payment_ref``) so one payment authorizes one
        deploy; ``store`` also resolves the rail's payer identity (e.g. the
        linked SS58 coldkey)."""
        ...


def get_payment_verifier() -> PaymentVerifier:
    """The deploy-fee payment verifier, selected by ``DEPLOY_FEE_RAIL``:

    - ``evm`` (default): WTAO on Bittensor EVM (chain 964). The developer's
      OWN EVM wallet pays — no substrate coldkey / ``developer_link`` needed.
    - ``finney``: native TAO on Bittensor mainnet (needs the app's SS58 link).

    Safe by default regardless: each verifier refuses unless its collector is
    configured, and ``verify_deploy_fee_payment`` never calls it at all unless
    ``ENABLE_PUBLIC_DEPLOYMENT=1``. So collection stays closed (#238) until the
    rail is deliberately configured and the gate opened.
    """
    from minotaur_subnet.api.services.evm_payment import deploy_fee_rail

    if deploy_fee_rail() == "finney":
        from minotaur_subnet.api.services.finney_payment import FinneyPaymentVerifier

        return FinneyPaymentVerifier()
    from minotaur_subnet.api.services.evm_payment import EvmDeployFeeVerifier

    return EvmDeployFeeVerifier()


def deploy_fee_params_hash(payment_ref: str, chain_id: int, amount_rao: int) -> bytes:
    """bytes32 binding of the payment params the deployer signs over.

    Binding the amount means a signature is valid only for the exact fee in
    force when it was signed; binding the chain means a payment on one chain
    can't authorize a deploy on another.
    """
    return keccak(f"{payment_ref}|{int(chain_id)}|{int(amount_rao)}".encode())


def verify_deploy_fee_payment(
    store: Any,
    definition: Any,
    *,
    payment: DeployFeePayment,
    verifier: PaymentVerifier | None = None,
    now: int | None = None,
) -> tuple[bool, str]:
    """Authorize a deploy-fee payment for ``definition`` (an app).

    Returns ``(fee_paid, error)``. ``fee_paid`` is True only when ALL hold:
    public deployment is enabled, the app has a ``deployer``, the deployer's
    EIP-712 ``pay_deploy_fee`` signature is valid + fresh and binds
    ``(app_id, payment_ref, payment_chain, amount)``, and the on-chain payment
    is confirmed by the verifier. The single-use nonce is consumed once, only
    on full success — so a failed or disabled verification never burns a nonce.

    The fee binds the **payment chain** (``DEPLOY_FEE_PAYMENT_CHAIN_ID``, 964),
    NOT the deploy target chain: the 0.5 TAO compensates solving the app (one
    fee), so it is paid once on BT EVM regardless of how many chains the app
    targets. ``deploy_app_intent`` records it and skips re-charging per chain.
    """
    from minotaur_subnet.deployment.deploy_fee import (
        deploy_fee_rao,
        public_deployment_enabled,
    )
    from minotaur_subnet.api.services import developer_auth
    from minotaur_subnet.api.services.evm_payment import deploy_fee_payment_chain_id

    # Structural #238 gate first, before consuming anything: while collection is
    # off the answer is always "not live", and no nonce is spent.
    if not public_deployment_enabled():
        return False, "deploy-fee collection is not live (ENABLE_PUBLIC_DEPLOYMENT off)"

    deployer = (getattr(definition, "deployer", "") or "").strip()
    if not deployer:
        return False, "app has no deployer; a paid deploy requires a deployer identity"
    if payment is None or not payment.payment_ref:
        return False, "payment_ref is required"
    if not payment.signature:
        return False, "pay_deploy_fee signature is required"

    payment_chain = deploy_fee_payment_chain_id()
    amount_rao = deploy_fee_rao()
    params_hash = deploy_fee_params_hash(payment.payment_ref, payment_chain, amount_rao)
    ok, err = developer_auth.verify_developer_auth(
        expected_deployer=deployer,
        action=developer_auth.ACTION_PAY_DEPLOY_FEE,
        app_id=definition.app_id,
        params_hash=params_hash,
        nonce=payment.nonce,
        deadline=payment.deadline,
        signature=payment.signature,
        now=now,
    )
    if not ok:
        return False, err

    active = verifier or get_payment_verifier()
    paid, perr = active.verify(
        store=store,
        app_id=definition.app_id,
        deployer=deployer,
        payment_ref=payment.payment_ref,
        chain_id=payment_chain,
        amount_rao=amount_rao,
    )
    if not paid:
        return False, perr

    # Consume only after the signature AND the payment both check out.
    consumed, cerr = store.consume_developer_nonce(
        definition.app_id, deployer.lower(), payment.nonce,
    )
    if not consumed:
        return False, cerr
    return True, ""
