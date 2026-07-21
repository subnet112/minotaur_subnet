"""Denylist of compromised signer/deployer addresses and banned app_ids.

Why this exists
---------------
App-management routes (``PUT /apps/{id}/scoring``, ``/solidity``, the lifecycle
actions) authorize a caller by an EIP-712 wallet signature from an *allowed
signer* — the app's own ``deployer`` ∪ ``APP_ADMIN_SIGNERS`` — and NOT
(anymore) by the shared admin key. So anyone holding an allowed-signer private
key can rewrite an app's scoring JS against the public API.

On 2026-07-18 two operator EOAs leaked from a shared 1Password vault and were
reused by an external attacker (2026-07-20, IP 149.88.110.53) to re-sign a
credential-exfil scoring payload:

  * ``0x63AeEF52…`` — the "MinoDeployer" deploy key (it created the subnet's
    own ValidatorRegistry/AppRegistry); it was the ``deployer`` of
    ``app_da6c96b84c60`` (DexAggregatorApp), whose scoring JS was weaponized
    into a ``process.env`` credential-exfil payload.
  * ``0xD4cF78…``  — the old owner key, rotated out to ``0x7dC301…`` on
    2026-07-18; reused to re-arm a sibling app.

These keys must never again authorize an app action or be recorded as an app
``deployer``, and the hard-deleted malicious ``app_id`` must never be
re-created. The set is hardcoded so the protection cannot be lost by a missing
env var; ``SIGNER_DENYLIST`` / ``APP_ID_DENYLIST`` (comma-separated) extend it.
"""

from __future__ import annotations

import os

# Lowercased EOAs that may NEVER sign an app action or be recorded as a deployer.
_COMPROMISED_SIGNERS: frozenset[str] = frozenset({
    "0x63aeef526406be8d1af89023422a455b4d8e130b",  # MinoDeployer — 1Password breach 2026-07-18
    "0xd4cf78059243faed77350f2dd7e73d5300465d70",  # old owner 0xD4 — rotated out + compromised 2026-07-18
})

# app_ids that may NEVER be (re-)created or persisted — hard-deleted malicious apps.
_BANNED_APP_IDS: frozenset[str] = frozenset({
    "app_da6c96b84c60",  # DexAggregatorApp — credential-exfil scoring payload (purged 2026-07-21)
})


def _split_env(name: str) -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()}


def denylisted_signers() -> set[str]:
    """Lowercased addresses barred from signing app actions / being a deployer."""
    return set(_COMPROMISED_SIGNERS) | _split_env("SIGNER_DENYLIST")


def is_signer_denylisted(addr: str | None) -> bool:
    """True if ``addr`` (any case) is a compromised/denylisted signer."""
    return (addr or "").strip().lower() in denylisted_signers()


def banned_app_ids() -> set[str]:
    """Lowercased app_ids that must never be (re-)created or persisted."""
    return set(_BANNED_APP_IDS) | _split_env("APP_ID_DENYLIST")


def is_app_id_banned(app_id: str | None) -> bool:
    """True if ``app_id`` (any case) is a hard-deleted/banned app."""
    return (app_id or "").strip().lower() in banned_app_ids()
