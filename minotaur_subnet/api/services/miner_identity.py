"""Bind a miner's GitHub account to their Bittensor hotkey (gist-proven).

A submission references a PR on the canonical solver repo, opened from the miner's
fork. To stop one miner submitting another miner's PR/code under their own hotkey,
the validator checks the PR's FORK OWNER against a registered GitHub-account↔hotkey
binding. This records that binding only when BOTH sides attest, with ZERO artifacts
in the solver repo:

  * GitHub-account control: the proof is a PUBLIC GIST. Only the owning account can
    host a gist under it, so the gist owner login — read from GitHub's authoritative
    response, never self-declared — proves control of that GitHub account.
  * hotkey control + consent: the gist content is a substrate signature by the hotkey
    over ``MinotaurGithubLink:{github_login}:{hotkey}`` — the hotkey holder consents
    to binding THIS github account (and no other).

Requiring both directions stops either side claiming the other: a copier can't host
a gist under someone else's account, and can't forge that account's hotkey signature.
The binding is persisted in the SQLite store so it survives restarts.

Mirrors api/services/developer_link (the EVM-deployer↔coldkey link).
"""

from __future__ import annotations

import json
import time
from typing import Any

from minotaur_subnet.api.routes.submissions.github_pr import (
    PRResolutionError,
    resolve_gist,
)
from minotaur_subnet.api.services.developer_link import verify_ss58_signature


def identity_message(github_login: str, hotkey: str) -> str:
    """Canonical message the hotkey signs to consent to binding a GitHub account.

    Binds the (lower-cased) GitHub login and the hotkey, so a signature can't be
    replayed to claim a different github account.
    """
    login = (github_login or "").strip().lower()
    return f"MinotaurGithubLink:{login}:{(hotkey or '').strip()}"


def link_miner_identity(
    store: Any,
    gist_id: str,
    *,
    resolve: Any = resolve_gist,
    now: float | None = None,
) -> tuple[bool, str, dict[str, str]]:
    """Verify a gist proof and persist the GitHub-account↔hotkey binding.

    The gist (owned by the GitHub account being linked) must contain JSON
    ``{"hotkey": "5G..", "signature": "0x.."}`` where ``signature`` is a substrate
    signature by ``hotkey`` over ``identity_message(gist_owner, hotkey)``. The gist
    OWNER (authoritative, from GitHub) is the account that gets bound — the caller
    cannot bind an account whose gist they don't own. Returns
    ``(ok, error, {github_login, hotkey})``.
    """
    try:
        owner_login, content = resolve(gist_id)
    except PRResolutionError as exc:
        return False, str(exc), {}

    try:
        doc = json.loads(content)
    except (ValueError, TypeError):
        return False, 'gist content must be JSON {"hotkey": "..", "signature": ".."}', {}
    if not isinstance(doc, dict):
        return False, "gist content must be a JSON object", {}

    hotkey = str(doc.get("hotkey") or "").strip()
    signature = str(doc.get("signature") or "").strip()
    if not hotkey or not signature:
        return False, "gist must contain non-empty 'hotkey' and 'signature'", {}

    ok, err = verify_ss58_signature(
        hotkey, identity_message(owner_login, hotkey), signature,
    )
    if not ok:
        return False, f"hotkey did not sign this GitHub binding: {err}", {}

    store.set_miner_identity(
        owner_login, hotkey,
        proof_ref=gist_id,
        linked_at=time.time() if now is None else float(now),
    )
    return True, "", {"github_login": owner_login.lower(), "hotkey": hotkey}
