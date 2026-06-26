"""Finney (native TAO) deploy-fee payment verifier.

Confirms that the deploy fee was actually paid on Bittensor mainnet (finney):
a finalized ``Balances.Transfer`` from the app's **linked coldkey** (the
``payer_ss58`` recorded by ``developer_link``) to the configured deploy-fee
**collector**, of at least the required RAO, used at most once.

Design: the *policy* (payer match, collector match, amount, finality,
consume-once) lives in :class:`FinneyPaymentVerifier` and is fully unit-tested
with a fake reader. The *substrate decoding* — the one part that needs a live
node — is isolated behind :class:`SubstrateTransferReader`. The shipped
:class:`SubstrateInterfaceTransferReader` is written against substrate-interface's
documented event shape but is **NOT yet validated against a live finney node**;
validate it with an integration test before turning collection on (i.e. before
setting ``ENABLE_PUBLIC_DEPLOYMENT=1`` with a collector configured). Until then
the gate keeps this code inert.

``payment_ref`` format: ``"{block_hash}:{extrinsic_index}"`` — pins the exact
transfer so consume-once is per-payment, not per-block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


def deploy_fee_collector_ss58() -> str:
    """The SS58 coldkey that receives deploy fees (``DEPLOY_FEE_COLLECTOR_SS58``)."""
    return os.environ.get("DEPLOY_FEE_COLLECTOR_SS58", "").strip()


def finney_url() -> str:
    """The finney RPC endpoint to read transfers from."""
    return (
        os.environ.get("FINNEY_URL", "").strip()
        or os.environ.get("SUBTENSOR_URL", "").strip()
        or "wss://entrypoint-finney.opentensor.ai:443"
    )


@dataclass(frozen=True)
class TransferRecord:
    """A decoded ``Balances.Transfer`` the verifier reasons about."""

    from_ss58: str
    to_ss58: str
    amount_rao: int
    finalized: bool


class SubstrateTransferReader(Protocol):
    """Resolves a ``payment_ref`` to its on-chain transfer. The one seam that
    talks to finney — kept narrow so the verifier policy is testable without a
    node."""

    def find_transfer(self, *, payment_ref: str) -> TransferRecord | None:
        """Return the transfer at ``payment_ref``, or ``None`` if not found."""
        ...


class FinneyPaymentVerifier:
    """Verifies a native-TAO deploy-fee payment against the app's linked coldkey.

    Implements the ``deploy_payment.PaymentVerifier`` protocol. Reads the
    ``payer_ss58`` link via ``store``, confirms the on-chain transfer through the
    reader, and consumes the payment once on success.
    """

    def __init__(self, reader: SubstrateTransferReader | None = None) -> None:
        self._reader = reader

    def _get_reader(self) -> SubstrateTransferReader:
        if self._reader is None:
            self._reader = SubstrateInterfaceTransferReader()
        return self._reader

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
        payer = (store.get_payer_ss58(app_id) or "").strip()
        if not payer:
            return False, (
                "no SS58 coldkey linked to this app's deployer; link one first "
                "(POST /apps/{id}/link-ss58)"
            )
        collector = deploy_fee_collector_ss58()
        if not collector:
            return False, "deploy-fee collector (DEPLOY_FEE_COLLECTOR_SS58) is not configured"

        try:
            record = self._get_reader().find_transfer(payment_ref=payment_ref)
        except Exception as exc:  # pragma: no cover - live-node failure path
            return False, f"finney transfer lookup failed: {exc}"
        if record is None:
            return False, "no transfer found at payment_ref"

        if not record.finalized:
            return False, "payment is not yet finalized on finney"
        if record.from_ss58 != payer:
            return False, "payment is not from the app's linked coldkey"
        if record.to_ss58 != collector:
            return False, "payment was not sent to the deploy-fee collector"
        if int(record.amount_rao) < int(amount_rao):
            return False, (
                f"payment {record.amount_rao} RAO is below the required {amount_rao} RAO"
            )

        # Consume only after every check passes — one payment, one deploy.
        spent, serr = store.consume_payment_ref(payment_ref, app_id)
        if not spent:
            return False, serr
        return True, ""


class SubstrateInterfaceTransferReader:
    """Reads a ``Balances.Transfer`` from finney via substrate-interface.

    WARNING: written against substrate-interface's documented event shape but
    NOT yet validated against a live finney node. The event attribute layout
    (list ``[from, to, amount]`` vs dict ``{from, to, amount}``) and the
    finality check may need adjustment for the deployed node/SDK version. Cover
    this with a live integration test before turning collection on.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or finney_url()

    def _connect(self) -> Any:
        from substrateinterface import SubstrateInterface  # lazy: heavy import

        return SubstrateInterface(url=self._url)

    @staticmethod
    def _parse_transfer(attributes: Any) -> tuple[str, str, int] | None:
        """Extract ``(from, to, amount)`` from a Transfer event's attributes."""
        if isinstance(attributes, dict):
            frm = attributes.get("from") or attributes.get("who")
            to = attributes.get("to") or attributes.get("dest")
            amount = attributes.get("amount") or attributes.get("value")
        elif isinstance(attributes, (list, tuple)) and len(attributes) >= 3:
            frm, to, amount = attributes[0], attributes[1], attributes[2]
        else:
            return None
        if frm is None or to is None or amount is None:
            return None
        return str(frm), str(to), int(amount)

    def _is_finalized(self, substrate: Any, block_hash: str) -> bool:
        try:
            target = substrate.get_block_number(block_hash)
            final_head = substrate.get_chain_finalised_head()
            final_num = substrate.get_block_number(final_head)
            return target is not None and final_num is not None and target <= final_num
        except Exception:  # pragma: no cover - conservative: unconfirmed → not final
            return False

    def find_transfer(self, *, payment_ref: str) -> TransferRecord | None:
        block_hash, _, idx_str = (payment_ref or "").partition(":")
        if not block_hash or not idx_str:
            return None  # require "{block_hash}:{extrinsic_index}"
        try:
            ext_idx = int(idx_str)
        except ValueError:
            return None

        substrate = self._connect()
        finalized = self._is_finalized(substrate, block_hash)
        events = substrate.get_events(block_hash)
        for event in events:
            value = getattr(event, "value", event)
            if not isinstance(value, dict):
                continue
            if value.get("extrinsic_idx") != ext_idx:
                continue
            if value.get("module_id") != "Balances" or value.get("event_id") != "Transfer":
                continue
            parsed = self._parse_transfer(value.get("attributes"))
            if parsed is None:
                continue
            frm, to, amount = parsed
            return TransferRecord(
                from_ss58=frm, to_ss58=to, amount_rao=amount, finalized=finalized,
            )
        return None
