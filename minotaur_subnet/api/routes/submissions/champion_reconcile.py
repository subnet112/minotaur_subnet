"""Follower-side champion pull-reconcile: self-heal missed lifecycle broadcasts.

Round lifecycle sync is push-based and single-shot: the leader fans out
close/certify/activate once (``broadcast_json`` — no retry, no queue), so a
follower that is unreachable for even a few seconds silently desyncs for that
round: its round FSM wedges mid-state, it keeps emitting the no-champion
fallback, and recovery needs an operator to fire the re-attest lever
(observed fleet-wide 2026-07-02, rounds e29716562/e29716599).

This loop is the PULL twin of that lever. Each follower periodically compares
the leader's standing champion (public ``GET /v1/solver/champion``) with its
own; on divergence it fetches the leader's ``/v1/solver/champion/sync-bundle``
— the same three force payloads the re-attest broadcasts — and applies them
through the SAME local sync functions the push handlers call. The trust model
is unchanged: the certify step still cryptographically verifies the
certificate approvals, and adoption still routes through the q1-trust gate.
Convergence no longer depends on any single message arriving.

Mirrors the OrderSync / ValidatorAppCatalogSync follower-pull pattern
(metagraph leader resolution + is_follower gate, injected for testability).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class ChampionPullReconcile:
    def __init__(
        self,
        *,
        leader_api_url: Callable[[], str | None],
        is_follower: Callable[[], bool],
        interval: float = 300.0,
        api_key: str = "",
        http_get_json: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._leader_api_url = leader_api_url  # () -> current leader's API base, or None
        self._is_follower = is_follower        # () -> True only when this node is a follower
        self._interval = interval
        self._api_key = api_key                # shared internal round key for the bundle GET
        self._http_get_json = http_get_json or self._default_http_get_json

    async def run_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:  # never let the loop die
                # %r, not %s: connection-level exceptions (TimeoutError, ...) have
                # an empty str(), which would log a blank line and hide the cause.
                logger.warning("Champion reconcile loop error: %r", exc)
            await asyncio.sleep(self._interval)

    async def reconcile_once(self) -> bool:
        """Compare champions with the leader; heal on divergence.

        Returns True when a heal was applied, False on no-op (in sync, not a
        follower, no leader resolved, or leader has no champion yet).
        """
        if not self._is_follower():
            return False
        base = (self._leader_api_url() or "").rstrip("/")
        if not base:
            return False

        leader = await self._http_get_json(f"{base}/v1/solver/champion")
        leader_sub = leader.get("submission_id")
        leader_rid = leader.get("activated_round_id")
        if not leader_sub or not leader_rid:
            return False  # leader has no standing champion — nothing to converge on

        from .state import get_round_store

        local = get_round_store().get_active_champion()
        if (
            local is not None
            and getattr(local, "submission_id", None) == leader_sub
            and getattr(local, "activated_round_id", None) == leader_rid
        ):
            return False  # in sync

        logger.warning(
            "[champion-reconcile] local champion %s@%s != leader %s@%s — pulling sync bundle",
            getattr(local, "submission_id", None),
            getattr(local, "activated_round_id", None),
            leader_sub,
            leader_rid,
        )
        headers = (
            {"x-solver-round-internal-key": self._api_key} if self._api_key else None
        )
        bundle = await self._http_get_json(
            f"{base}/v1/solver/champion/sync-bundle", headers=headers,
        )
        # Apply through the SAME functions the push handlers call — module-level
        # references (not from-imports) so the certify path's approval
        # verification, force gating, and q1-trust adoption all apply verbatim.
        from . import champion_consensus, round_manager
        from .models import ActivateRoundRequest, CertifyRoundRequest, CloseRoundRequest

        round_manager._sync_close_solver_round_state(
            CloseRoundRequest(**bundle["close"])
        )
        await champion_consensus._sync_certified_round_state(
            CertifyRoundRequest(**bundle["certify"])
        )
        await round_manager._activate_solver_round_state(
            ActivateRoundRequest(**bundle["activate"])
        )
        logger.info(
            "[champion-reconcile] healed onto leader champion %s (round %s)",
            leader_sub, leader_rid,
        )
        return True

    @staticmethod
    async def _default_http_get_json(
        url: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data if isinstance(data, dict) else {}
