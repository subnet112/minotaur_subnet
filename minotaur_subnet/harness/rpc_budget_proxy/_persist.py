"""Disk persistence for the RPC caches (fork-cache + block-pin cache).

WHY
===
Both caches hold ONLY provably-immutable data keyed by (chain, block, method,
params). On every container restart — Watchtower/update.sh recreate, crash,
host reboot — the in-memory dict is lost and the proxy cold-starts: the next
wave of simulations re-downloads every touched slot from the archive provider.
That cold re-fetch storm is the dominant *restart-driven* Alchemy cost. Writing
a periodic snapshot and reloading it on startup lets a restarted proxy serve a
warm cache instead.

Safe by construction: cached entries are immutable (state at a fixed historical
block never changes), so a reloaded snapshot can never serve a wrong answer, and
a lost or partial snapshot only costs a re-fetch. Writes are atomic
(temp + ``os.replace``) so a crash mid-write leaves the previous good snapshot
intact.

Off the event loop: ``write_snapshot`` does the JSON encode + disk write and is
meant to run in a worker thread (``asyncio.to_thread``) — the caller takes a
cheap on-loop copy of the live dict (``snapshot_fn``) and hands that copy here,
so a large serialization never freezes the proxy's event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Bump if the on-disk payload shape changes incompatibly — an older/newer
# snapshot is then ignored (cold start) instead of mis-decoded.
SNAPSHOT_VERSION = 1


def load_snapshot(path: str) -> Any | None:
    """Return the decoded snapshot payload, or ``None`` if absent, unreadable,
    corrupt, or written by a different version. Never raises — any problem just
    yields a cold start, which is always correct (immutable data)."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("cache snapshot %s unreadable (%s) — cold start", path, exc)
        return None
    if not isinstance(doc, dict) or doc.get("v") != SNAPSHOT_VERSION:
        logger.warning("cache snapshot %s version mismatch — cold start", path)
        return None
    return doc.get("payload")


def write_snapshot(path: str, payload: Any) -> None:
    """Atomically write ``payload`` to ``path``. Does the JSON encode + disk
    write, so call it in a worker thread. Best-effort: never raises past a
    warning — persistence must never take the proxy down."""
    if not path:
        return
    tmp = ""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        doc = {"v": SNAPSHOT_VERSION, "payload": payload}
        fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".cache-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp, path)  # atomic on POSIX; leaves the old file on failure
        tmp = ""
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("cache snapshot write to %s failed (%s) — skipped", path, exc)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class SnapshotScheduler:
    """Drives periodic + on-shutdown snapshots for one cache.

    The cache supplies ``snapshot_fn`` — called ON the event loop, it must
    return a cheap JSON-able *copy* of the live cache (so the subsequent encode
    can run in a thread without racing further mutations). Inert when ``path``
    is empty, so persistence stays fully opt-in.
    """

    def __init__(self, path: str, interval: int, snapshot_fn: Callable[[], Any]) -> None:
        self.path = (path or "").strip()
        self.interval = max(1, int(interval))
        self._snapshot_fn = snapshot_fn
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                await self.flush()
        except asyncio.CancelledError:  # graceful stop
            pass

    async def flush(self) -> None:
        if not self.enabled:
            return
        try:
            payload = self._snapshot_fn()  # cheap on-loop copy
        except Exception as exc:  # noqa: BLE001 - never let a snapshot kill the loop
            logger.warning("cache snapshot copy failed (%s) — skipped", exc)
            return
        await asyncio.to_thread(write_snapshot, self.path, payload)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()  # final snapshot on shutdown
