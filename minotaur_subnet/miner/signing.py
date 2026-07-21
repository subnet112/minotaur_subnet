"""Signing helpers for the miner client's authenticated API calls.

Two distinct schemes are in play, both verified against the SN112 metagraph:

* **Submission signing** — the message ``{pr_number}:{head_sha}:{round_id}``
  signed and sent in the request body. That lives with the submit path
  (``minotaur_subnet.miner.agent.loop._sign_submission`` and
  ``minotaur_subnet.miner.main``).
* **Gated read/scoring endpoints** — ``POST /v1/apps/{id}/score`` and
  ``POST /v1/orders/{id}/dry-run`` — authenticated with per-request
  ``X-Bittensor-*`` headers: a substrate signature over the canonical message
  ``f"{METHOD} {PATH} {TIMESTAMP}"``, verified by
  ``_require_admin_or_signed_miner`` on the server (timestamp must be within
  ±300s of now). **This module builds those headers.**

Wallet resolution mirrors ``_sign_submission``: ``--wallet-name`` /
``MINER_WALLET_NAME`` (falling back to ``MINER_HOTKEY``), optional
``--hotkey-name`` / ``MINER_HOTKEY_NAME``, and ``BT_WALLET_PATH``.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def build_canonical_message(method: str, path: str, timestamp: int) -> str:
    """The exact string the server reconstructs and verifies.

    Format ``f"{METHOD} {PATH} {TIMESTAMP}"`` — whitespace-sensitive. ``path``
    must be the request path only: no scheme/host and no query string, e.g.
    ``/v1/apps/app_123/score``. It must equal the server's ``request.url.path``.
    """
    return f"{method.upper()} {path} {timestamp}"


def _resolve_hotkey_keypair(
    wallet_name: str | None = None,
    hotkey_name: str | None = None,
    wallet_path: str | None = None,
):
    """Load the miner's hotkey keypair from a local bittensor wallet."""
    from bittensor_wallet import Wallet as BtWallet

    name = (
        wallet_name
        or os.environ.get("MINER_WALLET_NAME")
        or os.environ.get("MINER_HOTKEY")
        or ""
    ).strip()
    hk = (hotkey_name or os.environ.get("MINER_HOTKEY_NAME") or "").strip()
    path = (
        wallet_path
        or os.environ.get("BT_WALLET_PATH")
        or os.path.join(os.path.expanduser("~"), ".bittensor", "wallets")
    )
    if not name:
        raise ValueError(
            "no wallet name — pass --wallet-name or set MINER_WALLET_NAME"
        )
    wallet = (
        BtWallet(name=name, hotkey=hk, path=path)
        if hk
        else BtWallet(name=name, path=path)
    )
    return wallet.get_hotkey()


def signed_headers(
    method: str,
    path: str,
    *,
    wallet_name: str | None = None,
    hotkey_name: str | None = None,
    wallet_path: str | None = None,
    timestamp: int | None = None,
    required: bool = True,
) -> dict[str, str]:
    """Build ``X-Bittensor-*`` auth headers for a gated endpoint call.

    ``required=True`` (the CLI default): raise if no usable wallet is
    available, so a miner who asked to sign gets a clear error rather than a
    silent 401.

    ``required=False`` (the agent's self-test default): return ``{}`` — an
    UNSIGNED request, which a local testnet accepts and the leader rejects —
    logging a warning, mirroring the submission-signing fallback. Callers pass
    ``headers=signed_headers(..., required=False) or None``.
    """
    ts = int(timestamp if timestamp is not None else time.time())
    try:
        keypair = _resolve_hotkey_keypair(wallet_name, hotkey_name, wallet_path)
        message = build_canonical_message(method, path, ts)
        sig = keypair.sign(message)
        sig_bytes = sig if isinstance(sig, (bytes, bytearray)) else bytes(sig)
        return {
            "X-Bittensor-Hotkey": keypair.ss58_address,
            "X-Bittensor-Signature": "0x" + sig_bytes.hex(),
            "X-Bittensor-Timestamp": str(ts),
        }
    except Exception as exc:
        if required:
            raise
        logger.warning(
            "Gated call to %s %s is UNSIGNED (no usable wallet: %s). Works on "
            "a local testnet; the leader will 401. Set MINER_WALLET_NAME / "
            "MINER_HOTKEY_NAME / BT_WALLET_PATH.",
            method, path, exc,
        )
        return {}
