"""Self-attested validator identity for peer discovery (api side, port 8080).

Mirrors the validator daemon's GET /identity endpoint (aiohttp, port 9100)
in FastAPI form. The api service exposes this so champion-consensus peer
discovery can verify (evm_address, hotkey, axon_url) bindings the same way
order-consensus discovery does on the validator daemon side.

The endpoint is intentionally registered WITHOUT the /v1 prefix so the URL
matches the validator daemon's convention. Peer-discovery code stays
symmetric across the two ports.

Returns 503 when:
  - No signing key configured (api never reached consensus init)
  - No bittensor hotkey loaded (wallet missing or misconfigured)
  - VALIDATOR_AXON_URL not set in the env
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["identity"])


# Module-level handles, populated by startup.py via setters.
# Match the pattern used by api/routes/apps.py (_js_engine, _simulator).
_signing_key: str = ""
_metagraph_sync: Any = None


def set_signing_key(private_key: str) -> None:
    """Wire the EVM private key used to sign identity attestations.

    Same key that signs order/champion consensus approvals (it's the
    operator's VALIDATOR_PRIVATE_KEY). Empty string disables /identity
    (returns 503).
    """
    global _signing_key
    _signing_key = (private_key or "").strip()


def set_metagraph_sync(metagraph_sync: Any) -> None:
    """Wire the MetagraphSync instance so /identity can read my_hotkey."""
    global _metagraph_sync
    _metagraph_sync = metagraph_sync


@router.get("/identity")
def get_identity() -> dict[str, Any]:
    """Return a freshly signed EIP-712 binding of this validator's identity.

    Format matches consensus.identity.ValidatorIdentity.to_dict():
        {evm_address, hotkey, axon_url, expiry, nonce, signature}

    Each request generates a new signature so the payload is never stale —
    the freshness check on the verifier side compares against `expiry`.
    """
    if not _signing_key:
        raise HTTPException(
            status_code=503,
            detail="Consensus not enabled — no signing key",
        )
    if _metagraph_sync is None or not getattr(_metagraph_sync, "my_hotkey", None):
        raise HTTPException(
            status_code=503,
            detail="No bittensor hotkey configured",
        )
    axon_url = os.environ.get("VALIDATOR_AXON_URL", "").strip()
    if not axon_url:
        raise HTTPException(
            status_code=503,
            detail="VALIDATOR_AXON_URL not configured",
        )

    from minotaur_subnet.consensus.identity import sign_identity

    # Advisory public API base (e.g. https://api.example.com). Lets followers
    # route order-book pulls to a reachable API endpoint distinct from the
    # daemon axon's host:port. Optional — absent → followers fall back to the
    # axon→API port transform.
    api_url = os.environ.get("API_URL", "").strip() or None
    identity = sign_identity(
        _signing_key,
        _metagraph_sync.my_hotkey,
        axon_url,
        api_url=api_url,
    )
    return identity.to_dict()
