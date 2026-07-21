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

Cross-process READ visibility (Phase 2 — a 2nd process's writes reflected in this
process's in-memory dict) IS handled here now, via a monotonic ``updated_seq``
column + a ``writer`` tag + a dedicated read connection:
- every write stamps ``updated_seq = MAX(updated_seq)+1`` and the writing store's
  ``writer`` id, inside an explicit ``BEGIN IMMEDIATE`` (see ``_immediate``) so the
  MAX read and the UPSERT that depends on it are one atomic write transaction;
- a reader pulls only ``updated_seq > watermark AND writer <> me`` — its OWN rows
  are filtered IN SQL, so a single-process node never re-parses its own writes;
- ``max_seq()`` is the cheap O(log n) gate: unchanged ⇒ the reader returns without
  touching a row. A single-writer node therefore behaves exactly as before.
NO tombstones are needed: records are only ever inserted/updated — nothing deletes
a row from ``submissions`` (the retention strip only drops ``submission_details``
rows, and each process bounds its own memory independently).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from minotaur_subnet.harness import fastjson

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    data          BLOB NOT NULL,
    updated_seq   INTEGER NOT NULL DEFAULT 0,
    writer        TEXT
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
# NOTE: the updated_seq index is created by _migrate_schema, NOT here. On a
# pre-Phase-2 DB (the leader's, from #794) the CREATE TABLE above no-ops against
# the OLD column set, so indexing updated_seq before the ALTER adds it fails with
# "no such column" — i.e. it would crash the leader at boot.

_MIGRATED_KEY = "migrated_from_json"


class SubmissionDB:
    """SQLite persistence for the submission store (per-record writes)."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout backs busy handling for any statement that can't be served the
        # write lock immediately (a concurrent writer process).
        # isolation_level=None disables the legacy implicit-BEGIN machinery: every
        # write transaction is opened EXPLICITLY via _immediate() (BEGIN IMMEDIATE).
        # Required for correctness, not style — see _immediate.
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10.0, isolation_level=None
        )
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
        self._migrate_schema()
        self._conn.commit()
        # Identifies THIS store instance's writes so a reader can filter its own
        # rows out of the cross-process pull (in SQL). Per-instance uuid, not the
        # pid: pids recycle across container restarts, and a stale pid could make a
        # peer's rows look like our own → silently skipped → stale in-memory state.
        self._writer_id = uuid.uuid4().hex[:12]
        # Dedicated connection for the cross-process pull. The store's reads are
        # LOCK-FREE and run on the event-loop thread while the offload writer thread
        # may be mid-transaction on self._conn — sharing one sqlite3 connection
        # across those two is not safe. This second connection is used for SELECTs
        # only (max_seq / load_since), serialized by its own lock so it never waits
        # on the write path (which would re-introduce the loop stall Phase 1 fixed).
        self._read_conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
        self._read_conn.execute("PRAGMA busy_timeout=10000")
        self._read_lock = threading.Lock()

    @property
    def writer_id(self) -> str:
        """Opaque id tagging rows written by THIS store instance."""
        return self._writer_id

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """Run the body in an explicit ``BEGIN IMMEDIATE`` write transaction.

        This is load-bearing for cross-process correctness, NOT a style choice.
        Python's legacy isolation mode only emits an implicit BEGIN before the
        first INSERT/UPDATE/DELETE — a ``SELECT`` inside ``with conn:`` therefore
        runs in AUTOCOMMIT, holding no write lock. ``write_records`` reads
        ``MAX(updated_seq)`` and then writes rows derived from it, so under the
        legacy mode two processes could both read the same MAX and stamp the SAME
        updated_seq. A peer's watermark then skips past one of them → that row is
        NEVER pulled → permanently stale in-memory state (a benchmark result the
        coordinator never sees).

        Guarded by test_write_records_stamps_unique_seqs_without_the_store_flock,
        which drives SubmissionDB DIRECTLY: the store's flock serializes its own
        writers and would MASK a regression here, so the store-level two-writer
        test cannot see it.

        IMMEDIATE (not deferred) because a deferred txn that reads first and writes
        later must UPGRADE its lock, which raises SQLITE_BUSY_SNAPSHOT if a peer
        wrote in between — an error busy_timeout does NOT retry. IMMEDIATE takes
        the write lock up front, so contention is handled by busy_timeout instead.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                # SQLite already auto-rolled-back (SQLITE_FULL / SQLITE_IOERR /
                # interrupt), so ROLLBACK raises "no transaction is active" — which
                # would REPLACE the real error (disk full) with a confusing one.
                # The legacy `with conn:` form checked sqlite3_get_autocommit() and
                # skipped the rollback; hand-rolling it means handling this here.
                pass
            raise
        else:
            self._conn.execute("COMMIT")

    def _migrate_schema(self) -> None:
        """Additively bring a pre-Phase-2 DB up to the current schema.

        The leader's DB already exists (created by #794), so the Phase-2 columns
        must be ALTERed in rather than only declared in CREATE TABLE. Existing rows
        get updated_seq=0 / writer=NULL, which is correct: a fresh reader's
        watermark starts at max_seq() right after it hydrates every row, so seq-0
        rows are never re-pulled."""
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(submissions)")
        }
        for name, decl in (
            ("updated_seq", "INTEGER NOT NULL DEFAULT 0"),
            ("writer", "TEXT"),
        ):
            if name not in cols:
                try:
                    self._conn.execute(
                        f"ALTER TABLE submissions ADD COLUMN {name} {decl}"
                    )
                except sqlite3.OperationalError as exc:
                    # check-then-act race: the api and the worker boot together and
                    # both see the column missing, so the loser's ALTER fails. The
                    # column exists either way — that is the state we wanted.
                    if "duplicate column" not in str(exc).lower():
                        raise
                    logger.info(
                        "[submission-db] schema: submissions.%s added by a peer", name
                    )
                else:
                    logger.info("[submission-db] schema: added submissions.%s", name)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_submissions_updated_seq "
            "ON submissions(updated_seq)"
        )

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
            # Explicit BEGIN IMMEDIATE: the connection is in autocommit
            # (isolation_level=None), where `with self._conn:` would open NO
            # transaction at all and silently drop this import's all-or-nothing
            # guarantee (a crash mid-import would leave a half-populated store).
            with self._immediate():
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
        a details row that would re-inflate on reload.

        Each row is stamped with a monotonic ``updated_seq`` (MAX+1) and this
        instance's ``writer`` id, so a second process can incrementally pull just
        the rows it hasn't seen (see load_since). The MAX+1 read-then-write is
        race-free ONLY because _immediate() holds the write lock across both — see
        _immediate for why the legacy ``with self._conn:`` form is not enough."""
        try:
            with self._immediate():
                seq = self._conn.execute(
                    "SELECT COALESCE(MAX(updated_seq), 0) FROM submissions"
                ).fetchone()[0]
                for submission_id, record in records:
                    data, det = self._split(record)
                    seq += 1
                    self._conn.execute(
                        "INSERT INTO submissions(submission_id, data, updated_seq, writer) "
                        "VALUES(?, ?, ?, ?) "
                        "ON CONFLICT(submission_id) DO UPDATE SET data=excluded.data, "
                        "updated_seq=excluded.updated_seq, writer=excluded.writer",
                        (submission_id, data, seq, self._writer_id),
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

    def delete_records(self, ids: Iterable[str]) -> None:
        """Hard-DELETE the given submission rows (and their details via the FK).

        SAFE ONLY at load/retention pruning, before this store serves reads or a
        peer syncs: the incremental pull (load_since) is append-only with NO
        tombstones, so a delete would not propagate to an already-running peer.
        Pruned rows are old + terminal (never re-updated), and every process
        re-hydrates the bounded DB on the coordinated restart. Deleting low-seq
        rows never lowers ``max_seq`` (the retained rows keep the high seqs), so
        no reader's watermark is disturbed.
        """
        ids = [str(i) for i in ids]
        if not ids:
            return
        try:
            with self._immediate():
                self._conn.executemany(
                    "DELETE FROM submission_details WHERE submission_id=?",
                    [(i,) for i in ids],
                )
                self._conn.executemany(
                    "DELETE FROM submissions WHERE submission_id=?",
                    [(i,) for i in ids],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[submission-db] retention delete failed: %s", exc)

    # ── cross-process incremental pull (Phase 2) ─────────────────────────

    def max_seq(self) -> int:
        """Highest ``updated_seq`` in the DB — the cheap (indexed, O(log n)) gate
        for the reader: unchanged since its watermark ⇒ nothing to pull, so a
        single-writer node never advances past this call. Returns the caller's
        fallback (0) if the query fails, which simply means 'no pull this tick'."""
        with self._read_lock:
            try:
                row = self._read_conn.execute(
                    "SELECT COALESCE(MAX(updated_seq), 0) FROM submissions"
                ).fetchone()
                return int(row[0]) if row else 0
            except Exception as exc:  # noqa: BLE001 — a read blip must never break a read
                logger.warning("[submission-db] max_seq failed: %s", exc)
                return 0

    def load_since(self, seq: int) -> list[tuple[str, dict, int]] | None:
        """Rows written by ANOTHER store instance after ``seq``, oldest first.

        Our OWN rows are excluded IN SQL (``writer <> me``) — they are already in
        this process's memory, so re-parsing them would be pure waste on every read
        of a single-writer node. Returns (submission_id, full record, updated_seq).
        Materialized (not a generator) so the connection isn't held across the
        caller's in-memory merge.

        Returns ``None`` — NOT ``[]`` — if the pull fails. The two are opposite
        instructions to the caller: ``[]`` means "no peer rows, safe to advance the
        watermark", while a failure means "unknown, do NOT advance". Conflating them
        made a transient read blip advance the watermark past rows that were never
        applied → permanent silent loss."""
        with self._read_lock:
            try:
                rows = self._read_conn.execute(
                    "SELECT s.submission_id, s.data, d.details, s.updated_seq "
                    "FROM submissions s LEFT JOIN submission_details d "
                    "USING(submission_id) "
                    "WHERE s.updated_seq > ? AND (s.writer IS NULL OR s.writer <> ?) "
                    "ORDER BY s.updated_seq",
                    (seq, self._writer_id),
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[submission-db] load_since failed: %s", exc)
                return None
        # Decode OUTSIDE the read lock (and outside the DB): a row that fails to
        # decode is corruption, not a transient blip, and must not be reported as
        # "no rows" either — the caller stalls loudly rather than skipping it.
        try:
            out: list[tuple[str, dict, int]] = []
            for sid, data, det, row_seq in rows:
                record = fastjson.loads(data)
                record["benchmark_details"] = (
                    fastjson.loads(det) if det is not None else None
                )
                out.append((sid, record, int(row_seq)))
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("[submission-db] load_since decode failed: %s", exc)
            return None

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
        for conn in (self._conn, getattr(self, "_read_conn", None)):
            if conn is None:
                continue
            try:
                conn.close()
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
