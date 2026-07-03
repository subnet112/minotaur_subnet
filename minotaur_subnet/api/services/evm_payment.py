"""EVM (WTAO) deploy-fee payment verifier — the #238 fee on Bittensor EVM.

The deploy fee (0.5 TAO, compensating the miner work to solve a new App) is
paid in **WTAO** on **Bittensor EVM (chain 964)**, not on finney. Rationale:
the developer already controls an EVM wallet (the app ``deployer`` that signs
every management action), so paying on an EVM chain means the SAME wallet pays
— no substrate coldkey, no ``developer_link`` SS58 mapping. Verification is a
standard receipt/log check, not fragile substrate event decoding. WTAO
(``0x9Dc0…29F81``, an 18-decimal WETH9-style wrapper) keeps the fee an ERC-20
so accounting is decoupled from the gas token.

What counts as payment: a direct ERC-20 ``Transfer(from=deployer, to=collector,
value>=fee)`` emitted by the WTAO contract in a confirmed tx, used at most once
(``payment_ref`` = the tx hash, consumed via ``store.consume_payment_ref``).

Amount: the shared fee is expressed in RAO (9 decimals, ``deploy_fee_rao()``);
WTAO is 18 decimals, so ``fee_wei = rao * 10**(18-9)``. The scale is derived
from the token's ``decimals()`` (default 18) so a non-standard token can't
silently under/over-charge.

Config (collection stays OFF until these are set AND
``ENABLE_PUBLIC_DEPLOYMENT=1``):
- ``DEPLOY_FEE_RAIL``            evm | finney   (default evm)
- ``DEPLOY_FEE_PAYMENT_CHAIN_ID`` default 964
- ``DEPLOY_FEE_COLLECTOR_EVM``   the address that receives WTAO fees
- ``DEPLOY_FEE_TOKEN_ADDRESS``   default WTAO on 964
- ``DEPLOY_FEE_MIN_CONFIRMATIONS`` default 6
"""

from __future__ import annotations

import logging
import os
from typing import Any

from eth_hash.auto import keccak

logger = logging.getLogger(__name__)

# Canonical WTAO on Bittensor EVM (chain 964); WETH9-style, 18 decimals.
DEFAULT_WTAO_964 = "0x9Dc08C6e2BF0F1eeD1E00670f80Df39145529F81"
DEFAULT_PAYMENT_CHAIN_ID = 964
DEFAULT_MIN_CONFIRMATIONS = 6
# ERC-20 Transfer(address,address,uint256) topic0.
_TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()


def deploy_fee_rail() -> str:
    """"evm" (WTAO on BT EVM, default) or "finney" (native TAO on substrate)."""
    return os.environ.get("DEPLOY_FEE_RAIL", "evm").strip().lower() or "evm"


def deploy_fee_payment_chain_id() -> int:
    """Chain the deploy fee is PAID on — independent of the deploy TARGET chain
    (one fee per app compensates the solve work, not per targeted chain)."""
    raw = os.environ.get("DEPLOY_FEE_PAYMENT_CHAIN_ID", "").strip()
    try:
        return int(raw) if raw else DEFAULT_PAYMENT_CHAIN_ID
    except ValueError:
        return DEFAULT_PAYMENT_CHAIN_ID


def deploy_fee_collector_evm() -> str:
    """The EVM address that receives WTAO deploy fees
    (``DEPLOY_FEE_COLLECTOR_EVM``). Empty = collection not configured."""
    return os.environ.get("DEPLOY_FEE_COLLECTOR_EVM", "").strip()


def deploy_fee_token_address() -> str:
    """The ERC-20 fee token (``DEPLOY_FEE_TOKEN_ADDRESS``; default WTAO/964)."""
    return os.environ.get("DEPLOY_FEE_TOKEN_ADDRESS", "").strip() or DEFAULT_WTAO_964


def deploy_fee_min_confirmations() -> int:
    raw = os.environ.get("DEPLOY_FEE_MIN_CONFIRMATIONS", "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_MIN_CONFIRMATIONS
    except ValueError:
        return DEFAULT_MIN_CONFIRMATIONS


def _topic_addr(topic: Any) -> str:
    """Last 20 bytes of a 32-byte log topic → lowercased 0x address."""
    h = topic.hex() if hasattr(topic, "hex") else str(topic)
    h = h[2:] if h.startswith("0x") else h
    return "0x" + h[-40:].lower()


class EvmDeployFeeVerifier:
    """Confirms a WTAO deploy-fee payment on the configured EVM chain.

    Implements the ``deploy_payment.PaymentVerifier`` protocol. ``get_web3`` is
    injectable for tests; production uses ``blockchain.chains.get_web3``.
    """

    def __init__(self, get_web3: Any = None) -> None:
        self._get_web3 = get_web3

    def _w3(self, chain_id: int) -> Any:
        if self._get_web3 is not None:
            return self._get_web3(chain_id)
        from minotaur_subnet.blockchain.chains import get_web3

        return get_web3(chain_id)

    def _token_decimals(self, w3: Any, token: str) -> int:
        try:
            out = w3.eth.call({"to": token, "data": "0x" + keccak(b"decimals()")[:4].hex()})
            d = int.from_bytes(bytes(out)[:32], "big")
            return d if 0 < d <= 36 else 18
        except Exception:
            return 18

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
        """Return ``(ok, error)``. ``chain_id`` here is the PAYMENT chain (the
        caller passes ``deploy_fee_payment_chain_id()``), not the deploy target.

        Confirms: the tx succeeded and has enough confirmations, and it carries
        a WTAO ``Transfer`` from the app's ``deployer`` to the configured
        collector for at least the fee — then consumes the ref once."""
        collector = deploy_fee_collector_evm().lower()
        if not collector:
            return False, "deploy-fee collector not configured (DEPLOY_FEE_COLLECTOR_EVM)"
        token = deploy_fee_token_address().lower()
        deployer_l = (deployer or "").strip().lower()
        if not deployer_l:
            return False, "app has no deployer identity"

        try:
            w3 = self._w3(int(chain_id))
        except Exception as exc:
            return False, f"cannot reach payment chain {chain_id}: {exc}"

        try:
            receipt = w3.eth.get_transaction_receipt(payment_ref)
        except Exception:
            receipt = None
        if not receipt:
            return False, f"payment tx not found on chain {chain_id}: {payment_ref}"
        if int(receipt.get("status", 0)) != 1:
            return False, "payment tx reverted"

        # Confirmations.
        try:
            head = int(w3.eth.block_number)
            conf = head - int(receipt["blockNumber"]) + 1
        except Exception:
            conf = 0
        need = deploy_fee_min_confirmations()
        if conf < need:
            return False, f"payment not yet confirmed ({conf}/{need} confirmations)"

        # Amount: RAO (9 dp) → token wei via the token's actual decimals.
        decimals = self._token_decimals(w3, token)
        fee_wei = int(amount_rao) * (10 ** max(0, decimals - 9))

        # Find a WTAO Transfer(from=deployer, to=collector, value>=fee) log.
        matched = False
        for log in receipt.get("logs", []) or []:
            laddr = (log.get("address") or "")
            laddr = laddr.lower() if isinstance(laddr, str) else str(laddr).lower()
            topics = log.get("topics") or []
            if laddr != token or len(topics) < 3:
                continue
            t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
            if (t0 if t0.startswith("0x") else "0x" + t0).lower() != _TRANSFER_TOPIC:
                continue
            if _topic_addr(topics[1]) != deployer_l or _topic_addr(topics[2]) != collector:
                continue
            data = log.get("data")
            raw = bytes.fromhex(data[2:]) if isinstance(data, str) else bytes(data or b"")
            value = int.from_bytes(raw[:32], "big") if raw else 0
            if value >= fee_wei:
                matched = True
                break
        if not matched:
            return False, (
                f"no WTAO transfer of >= {fee_wei} wei from {deployer_l[:10]}… "
                f"to the collector in tx {payment_ref}"
            )

        # Consume-once: one payment authorizes one deploy.
        spent, serr = store.consume_payment_ref(payment_ref, app_id)
        if not spent:
            return False, serr
        return True, ""
