"""Validator champion resolver — reads the champion from the CO-LOCATED API.

KEYSTONE of the state-consolidation refactor (docs/architecture/state-consolidation.md,
Phase 2). The validator process calls set_weights, but the AUTHORITY on "who is the
champion" is the co-located API process (it ran the benchmark/consensus and adopted).
Historically the validator kept its OWN file-backed RoundStore copy and read that
(validator/main.py _local_champion_hotkey) — a second copy that drifts to None/stale and
silently burns (bug #446). This resolver makes the validator READ the API's single source
of truth over HTTP (GET /v1/solver/champion), which returned the correct champion the
whole time, with a bounded last-known-good MEMO so a transient API restart never flips a
standing champion to 100% burn.

ANTI-FREE-RIDE: champion_api_url MUST be the operator's OWN co-located API (default
http://api:8080), the same node that did the consensus work — NEVER the public leader and
NEVER chain. This preserves the per-validator-independence invariant (an operator cannot
free-ride by copying someone else's published answer). Mirrors the existing LEADER_API_URL
security posture.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class ChampionResolver:
    """Resolve the current champion hotkey from the co-located API, with a TTL memo.

    No wall-clock is read inside: ``resolve(now)`` takes a monotonic clock value from the
    caller, so the memo/TTL logic is deterministic and unit-testable. ``_fetch`` is the
    single network seam (override it in tests).
    """

    def __init__(
        self,
        champion_api_url: str,
        *,
        request_timeout: float = 5.0,
        memo_ttl_seconds: float = 2600.0,  # ~2 epochs (1300s each) — survives an API restart
    ) -> None:
        self._url = (champion_api_url or "").rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._memo_ttl = memo_ttl_seconds
        self._memo_hotkey: str | None = None
        self._memo_at: float | None = None  # monotonic seconds of last DEFINITIVE resolve

    @property
    def configured(self) -> bool:
        return bool(self._url)

    async def _fetch(self) -> str | None:
        """GET the champion hotkey from the co-located API, filtered to a real miner.

        Returns the hotkey, or None when the API DEFINITIVELY reports no champion. Raises
        on any transport/HTTP error (so resolve() can fall back to the memo). The only
        network seam — overridden in tests.
        """
        from minotaur_subnet.weight_policy import is_real_miner_hotkey

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(f"{self._url}/v1/solver/champion") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GET /v1/solver/champion -> {resp.status}")
                data = await resp.json()
        hotkey = str((data or {}).get("hotkey") or "").strip()
        return hotkey if is_real_miner_hotkey(hotkey) else None

    async def resolve(self, now: float) -> tuple[str | None, str]:
        """Return ``(hotkey_or_None, source)`` where source ∈ {'api', 'memo', 'none'}.

        A successful API read (even a definitive "no champion") refreshes the memo. A
        transient failure returns the last-known-good champion if still within TTL, else
        ('none') — so the validator NEVER silently burns a standing champion through a
        brief API restart, and NEVER serves an arbitrarily stale answer.
        """
        if not self._url:
            return (None, "none")
        try:
            hotkey = await self._fetch()
            if hotkey is not None:
                # Only memoize a REAL champion. Never overwrite a good last-known-good with
                # a None: a transient "no champion" (e.g. the API's genesis default during
                # its store-load window) must not poison the memo into a sticky burn. A
                # definitive None is still returned in real time as (None, 'api') so the
                # caller can act on it; it just doesn't erase the memo.
                self._memo_hotkey = hotkey
                self._memo_at = now
            return (hotkey, "api")
        except Exception as exc:  # noqa: BLE001 — transient API/transport error
            if self._memo_at is not None and (now - self._memo_at) <= self._memo_ttl:
                logger.warning(
                    "champion API read failed (%s); using last-known-good (age %.0fs)",
                    exc, now - self._memo_at,
                )
                return (self._memo_hotkey, "memo")
            logger.warning("champion API read failed (%s); no fresh memo -> no champion", exc)
            return (None, "none")
