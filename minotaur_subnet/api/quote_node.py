"""Quote-node (trust-leader) mode.

A quote node is a NON-consensus, NON-leader instance that serves reads — chiefly
POST /v1/apps/{id}/quote — by tracking an EXTERNAL lead validator's champion. It
holds no metagraph identity, no hotkey, and no consensus/relayer keys: it simply
polls the configured leader's public champion endpoint, pulls the certified
champion image BY DIGEST, and runs it locally through its own anvil-fork
simulator. It never emits weights and never accepts orders.

Activation is a single env var: ``LEADER_API_URL`` = the lead validator's API
base (e.g. ``https://api.minotaursubnet.com``). When set, startup:

  * treats this node as a FOLLOWER of that URL (not a standalone leader),
  * resolves the leader URL from the env directly (no metagraph),
  * initializes the champion machinery (block loop + epoch manager +
    pull-reconcile + boot-restore) so the existing follower adopt/hot-swap path
    runs against the external leader,

while the operator disables the consensus role the usual way
(``ENABLE_SOLVER_ROUND_COORDINATOR=0`` + ``DISABLE_BENCHMARK_WORKER=1``).

TRUST MODEL: the champion POINTER is trusted (it is the same operator's leader);
the champion CODE is still pulled by content-addressed digest, so its bytes are
cryptographically pinned — trusting the leader never means trusting arbitrary
solver code. The solver still runs sandboxed (isolated-vm / internal network),
because it is untrusted miner code exactly as on the leader.
"""

from __future__ import annotations

import os

# Env var that both enables the mode and supplies the leader's API base.
LEADER_API_URL_ENV = "LEADER_API_URL"


def leader_api_url() -> str | None:
    """The external lead validator's API base for a quote node, or None.

    Trailing slash trimmed so callers can append ``/v1/...`` uniformly.
    """
    raw = os.environ.get(LEADER_API_URL_ENV, "").strip()
    return raw.rstrip("/") or None


def is_quote_node() -> bool:
    """True when this process is a trust-leader quote node (LEADER_API_URL set)."""
    return leader_api_url() is not None


def _sha_suffix(ref: str | None) -> str | None:
    """The ``sha256:…`` digest embedded in an image ref, if any, for a
    representation-independent compare (``<repo>@sha256:D`` vs bare ``sha256:D``)."""
    if not ref:
        return None
    marker = "sha256:"
    idx = ref.find(marker)
    return ref[idx:] if idx != -1 else ref


def champion_status(loaded_image_ref: str | None, active_champion: object | None) -> dict:
    """Compare the LOADED live-solver image against the standing champion record.

    ``active_champion`` is a ChampionSnapshot-like object (``submission_id``,
    ``image_digest``, ``image_id``, ``activated_round_id``) or None. Returns a
    small dict for /health and quote responses. ``synced`` is the safety signal:
    False means the champion record advanced but the running solver did NOT swap
    to it (the silent record-vs-live split) — quotes are then pricing off a stale
    solver even though the champion pointer looks current. ``synced`` is None when
    it can't be determined (no champion yet, or no digest to compare against).
    """
    sub = getattr(active_champion, "submission_id", None) if active_champion else None
    champ_digest = getattr(active_champion, "image_digest", None) if active_champion else None
    champ_image = champ_digest or (getattr(active_champion, "image_id", None) if active_champion else None)

    synced: bool | None
    if not loaded_image_ref or not champ_image:
        # Can't prove sync (fresh node with no champion, or a legacy non-digest
        # image); leave it unknown rather than falsely asserting synced.
        synced = None
    else:
        synced = _sha_suffix(loaded_image_ref) == _sha_suffix(champ_image)

    return {
        "active_submission_id": sub,
        "active_image": champ_image,
        "loaded_image": loaded_image_ref,
        "synced": synced,
    }
