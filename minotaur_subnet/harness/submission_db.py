"""Per-record SQLite persistence backend for :class:`SubmissionStore`.

Why: the store re-serialized the WHOLE ~44MB submissions.json on every mutation
(json/orjson encode holds the GIL → a per-write loop stall). The live store is
~13.5k records but only ~59 are in-flight; 99.6% are terminal. This backend makes
each write an O(1) per-row UPSERT instead of O(store).

Design (see the store for the read side — it keeps the in-memory dict as the SOLE
read source, so all reads / the benchmark pack context stay byte-identical):
- TWO tables so the ``benchmark_details`` retention strip is an O(1)
  ``DELETE FROM submission_details`` rather than an O(store) rewrite of every
  stripped row:
    submissions(submission_id PK, data)          -- to_dict() MINUS benchmark_details
    submission_details(submission_id PK, details) -- benchmark_details only
  A stripped record simply has no details row; ``load_all`` LEFT JOINs, so a
  missing details row reloads as ``benchmark_details=None`` — byte-identical to
  the current stripped state (and it keeps the in-memory details bound).
- WAL + synchronous=NORMAL + busy_timeout: NORMAL is fully crash-safe against a
  PROCESS crash (api kill / OOM / update-restart — the common case; committed txns
  live in the WAL and are recovered on reopen). Only an OS crash / power loss can
  lose the WAL tail (the last few tiny writes), which is rare and re-benchmark
  recoverable; FULL would fsync every commit for needless latency on per-record
  writes. busy_timeout lets a second process (Phase 2 benchmark worker) wait for
  the write lock instead of erroring. Per-row UPSERT means two writers touching
  different rows never clobber each other (unlike the old whole-file replace) —
  the two-writer lost-update class is structurally impossible.

Rollback: the crash-safe DB (not the frozen submissions.json) is the authoritative
recovery source. To downgrade to a pre-SQLite build, export the DB to JSON FIRST
via ``python -m minotaur_subnet.harness.submission_db export <db> <json>`` (works
even after an OOM/SIGKILL, since the DB survives), then start the old build.
- One connection per store instance; the store serializes all access with its
  in-process RLock, so ``check_same_thread=False`` is safe.

Cross-process READ visibility (a 2nd process's writes reflected in this process's
in-memory dict) is intentionally NOT handled here — it is deferred to Phase 2 via a
monotonic ``updated_seq`` column + tombstones + a read-only connection. Today the
store is single-writer (only the api constructs it), so it isn't needed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from minotaur_subnet.harness import fastjson

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    data          BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_details (
    submission_id TEXT PRIMARY KEY REFERENCES submissions(submission_id) ON DELETE CASCADE,
    details       BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_MIGRATED_KEY = "migrated_from_json"


class SubmissionDB:
    """SQLite persistence for the submission store (per-record writes)."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout backs busy handling for any statement that can't be served the
        # write lock immediately (a concurrent writer process).
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # WAL + NORMAL: fully crash-safe against a PROCESS crash (api kill / OOM /
        # update-restart — the common case; committed txns live in the WAL and are
        # recovered on reopen). Only an OS crash / power loss can lose the WAL tail
        # (the last few tiny writes), which is rare and recoverable (re-benchmark).
        # FULL would fsync every commit — needless latency for per-record writes.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        got = (mode[0] if mode else "").lower()
        if got != "wal":
            # WAL needs mmap + a -shm file; a network FS (NFS/CIFS) can refuse it.
            # /data is a local docker volume in prod, so this is a loud warning, not
            # a fatal — the store still works with whatever journal mode we got.
            logger.warning(
                "[submission-db] journal_mode=%s (wanted wal) for %s — "
                "check the /data volume type", got, db_path,
            )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── migration ────────────────────────────────────────────────────────

    def is_migrated(self) -> bool:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (_MIGRATED_KEY,)
        ).fetchone()
        return row is not None

    def _mark_migrated(self) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", (_MIGRATED_KEY,)
        )

    def migrate_from_json(self, json_path: Path) -> int:
        """One-time import of an existing submissions.json into the DB.

        Idempotent + atomic: the bulk insert AND the migrated flag land in ONE
        transaction, so a crash mid-import rolls back (table empty, flag unset)
        and the next boot retries. Returns the number of records imported (0 if
        already migrated / no JSON / DB already populated). The JSON file is left
        in place (rollback + audit)."""
        if self.is_migrated():
            return 0
        have = self._conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        if have > 0:
            # DB already has rows (e.g. a prior partial run that wasn't flagged) —
            # don't double-import; just flag it.
            self._mark_migrated()
            self._conn.commit()
            return 0
        if not json_path.exists():
            self._mark_migrated()
            self._conn.commit()
            return 0
        try:
            raw = json_path.read_bytes()
        except OSError as exc:
            # Transient IO blip at the one migration boot. Do NOT mark migrated —
            # leave the flag unset so the NEXT boot retries and the blip self-heals
            # (a permanent empty store on a leader is the #430 burn class).
            logger.error(
                "[submission-db] migration read failed (%s); NOT marking migrated "
                "— will retry next boot", exc,
            )
            raise
        try:
            data = fastjson.loads(raw)
        except Exception as exc:  # noqa: BLE001 — genuinely corrupt JSON
            # Fail LOUD (like require_durable_state): starting a consensus-critical
            # validator with an EMPTY submission store must not be silent. Leave the
            # flag unset + raise so an operator repairs the retained submissions.json
            # (or deliberately clears the DB) rather than the validator burning.
            logger.error("[submission-db] submissions.json is corrupt (%s) — refusing "
                         "to start empty; leaving it for repair", exc)
            raise
        n = 0
        try:
            with self._conn:  # BEGIN…COMMIT (or ROLLBACK on exception)
                for sid, record in (data or {}).items():
                    if not isinstance(record, dict):
                        continue
                    details = record.get("benchmark_details")
                    body = {k: v for k, v in record.items() if k != "benchmark_details"}
                    self._conn.execute(
                        "INSERT OR REPLACE INTO submissions(submission_id, data) VALUES(?, ?)",
                        (sid, fastjson.dumps(body)),
                    )
                    if details:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO submission_details(submission_id, details) VALUES(?, ?)",
                            (sid, fastjson.dumps(details)),
                        )
                    n += 1
                self._mark_migrated()
        except Exception as exc:  # noqa: BLE001
            logger.error("[submission-db] migration failed, rolled back: %s", exc)
            raise
        logger.info("[submission-db] migrated %d records from %s", n, json_path)
        return n

    # ── writes (per record) ──────────────────────────────────────────────

    @staticmethod
    def _split(record: dict) -> tuple[bytes, bytes | None]:
        """Serialize a to_dict() record into (data-without-details, details-or-None)."""
        details = record.get("benchmark_details")
        body = {k: v for k, v in record.items() if k != "benchmark_details"}
        det_blob = fastjson.dumps(details) if details else None
        return fastjson.dumps(body), det_blob

    def write_records(
        self,
        records: Iterable[tuple[str, dict]],
        strip_ids: Iterable[str] = (),
    ) -> None:
        """Write the given record(s) AND drop the retention-strip's details rows
        in ONE transaction, so the stripped state is durable together with the
        write (matching the old whole-file os.replace all-or-nothing semantics).

        Each record is a data UPSERT + a details UPSERT-or-DELETE; strip_ids are
        the submission_ids whose benchmark_details the in-memory retention pass
        just nulled (their DB details rows are DELETEd here). All-or-nothing:
        a crash between statements rolls the whole batch back rather than leaking
        a details row that would re-inflate on reload."""
        try:
            with self._conn:
                for submission_id, record in records:
                    data, det = self._split(record)
                    self._conn.execute(
                        "INSERT INTO submissions(submission_id, data) VALUES(?, ?) "
                        "ON CONFLICT(submission_id) DO UPDATE SET data=excluded.data",
                        (submission_id, data),
                    )
                    if det is not None:
                        self._conn.execute(
                            "INSERT INTO submission_details(submission_id, details) VALUES(?, ?) "
                            "ON CONFLICT(submission_id) DO UPDATE SET details=excluded.details",
                            (submission_id, det),
                        )
                    else:
                        self._conn.execute(
                            "DELETE FROM submission_details WHERE submission_id=?",
                            (submission_id,),
                        )
                for sid in strip_ids:
                    self._conn.execute(
                        "DELETE FROM submission_details WHERE submission_id=?", (sid,)
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[submission-db] batch write failed: %s", exc)

    # ── read (startup / reload only; hot reads use the in-memory dict) ────

    def load_all(self) -> Iterator[tuple[str, dict]]:
        """Yield (submission_id, full-record-dict) for every row, re-attaching
        benchmark_details from the side table (LEFT JOIN → missing = None)."""
        cur = self._conn.execute(
            "SELECT s.submission_id, s.data, d.details "
            "FROM submissions s LEFT JOIN submission_details d "
            "USING(submission_id)"
        )
        for sid, data, det in cur:
            record = fastjson.loads(data)
            record["benchmark_details"] = fastjson.loads(det) if det is not None else None
            yield sid, record

    def export_to_json(self, json_path: Path) -> int:
        """Reconstruct a whole-store submissions.json from the DB — the crash-safe
        recovery source. Run this BEFORE deliberately downgrading to a pre-SQLite
        build (it works even after an OOM/SIGKILL, unlike the graceful-shutdown
        snapshot). Returns the record count. The exported JSON reflects the
        current retention-bounded state (stripped records have benchmark_details
        null), which is exactly what the store holds."""
        data = {sid: record for sid, record in self.load_all()}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = json_path.with_name(f".{json_path.name}.export.tmp")
        tmp.write_bytes(fastjson.dumps(data))
        os.replace(tmp, json_path)
        return len(data)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def _main() -> None:
    import sys

    if len(sys.argv) != 4 or sys.argv[1] != "export":
        print(
            "usage: python -m minotaur_subnet.harness.submission_db export "
            "<db_path> <json_path>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    db = SubmissionDB(Path(sys.argv[2]))
    try:
        n = db.export_to_json(Path(sys.argv[3]))
    finally:
        db.close()
    print(f"exported {n} records to {sys.argv[3]}")


if __name__ == "__main__":
    _main()
