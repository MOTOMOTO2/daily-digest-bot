"""The local record: SQLite.

Five tables carry everything the bot knows:

``items``
    Every item ever seen, with the fingerprint that makes it unique. An item
    with ``digest_id IS NULL`` has been collected but not yet sent.
``digests``
    One row per day, unique on ``digest_date``. That constraint is what
    enforces "one digest a day" even if the bot is invoked every five minutes.
``runs``
    One row per invocation, with the exit status.
``source_results``
    Per-source outcome for each run, which is how consecutive failures and
    per-source health are derived.
``events``
    Warnings and errors mirrored out of the log so the dashboard can show them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .models import Item, SourceOutcome, StoredItem, iso, parse_iso, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL UNIQUE,
    title_key      TEXT,
    source         TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT,
    canonical_url  TEXT,
    summary        TEXT,
    author         TEXT,
    published_at   TEXT,
    first_seen_at  TEXT NOT NULL,
    raw            TEXT,
    digest_id      INTEGER REFERENCES digests(id) ON DELETE SET NULL,
    sent_at        TEXT,
    -- Set when an item aged past bot.lookback_hours before any digest went
    -- out. Keeps the pending queue from growing without bound while the
    -- fingerprint stays on record forever.
    expired_at     TEXT
);
CREATE INDEX IF NOT EXISTS items_title_key    ON items(title_key);
CREATE INDEX IF NOT EXISTS items_pending      ON items(digest_id) WHERE digest_id IS NULL;
CREATE INDEX IF NOT EXISTS items_source       ON items(source);
CREATE INDEX IF NOT EXISTS items_first_seen   ON items(first_seen_at);

CREATE TABLE IF NOT EXISTS digests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date  TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
    item_count   INTEGER NOT NULL DEFAULT 0,
    channel      TEXT,
    title        TEXT,
    body         TEXT,
    created_at   TEXT NOT NULL,
    sent_at      TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS digests_status ON digests(status);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    trigger        TEXT,
    items_fetched  INTEGER NOT NULL DEFAULT 0,
    items_new      INTEGER NOT NULL DEFAULT 0,
    digest_id      INTEGER REFERENCES digests(id) ON DELETE SET NULL,
    sent           INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS source_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    kind           TEXT,
    status         TEXT NOT NULL,
    items_fetched  INTEGER NOT NULL DEFAULT 0,
    items_new      INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    error_type     TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_results_source ON source_results(source, created_at);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    source   TEXT,
    event    TEXT,
    message  TEXT NOT NULL,
    detail   TEXT,
    run_id   INTEGER
);
CREATE INDEX IF NOT EXISTS events_ts    ON events(ts);
CREATE INDEX IF NOT EXISTS events_level ON events(level);
"""


class StoreError(Exception):
    """Raised when the local record cannot be opened or migrated."""


class StoreConflict(StoreError):
    """A uniqueness rule refused a write, e.g. a second digest for the same day.

    SQLite reports this as ``sqlite3.IntegrityError``; DynamoDB as a cancelled
    transaction, which :class:`digestbot.dynamo.DynamoStore` raises as this.
    """


def _row_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
    return sqlite3.Row(cursor, row)


class Store:
    """Thin, explicit data-access layer over SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(
                str(self.path), timeout=30, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database at {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def migrate(self) -> None:
        try:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot initialise schema in {self.path}: {exc}") from exc

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # items / deduplication
    # ------------------------------------------------------------------

    def seen_fingerprints(self, fingerprints: Sequence[str]) -> set[str]:
        if not fingerprints:
            return set()
        found: set[str] = set()
        # Chunked to stay under SQLite's variable limit on large batches.
        for start in range(0, len(fingerprints), 500):
            chunk = fingerprints[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT fingerprint FROM items WHERE fingerprint IN ({placeholders})", chunk
            ).fetchall()
            found.update(row["fingerprint"] for row in rows)
        return found

    def seen_title_keys(self, title_keys: Sequence[str]) -> set[str]:
        keys = [key for key in title_keys if key]
        if not keys:
            return set()
        found: set[str] = set()
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT title_key FROM items WHERE title_key IN ({placeholders})", chunk
            ).fetchall()
            found.update(row["title_key"] for row in rows if row["title_key"])
        return found

    def add_item(self, item: Item, fingerprint: str) -> int | None:
        """Insert an item. Returns its row id, or None if already recorded.

        The UNIQUE constraint on ``fingerprint`` is the real deduplication
        guarantee: even with two bots racing, the same item lands once.
        """
        cursor = self.conn.execute(
            """
            INSERT INTO items (
                fingerprint, title_key, source, title, url, canonical_url,
                summary, author, published_at, first_seen_at, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO NOTHING
            """,
            (
                fingerprint,
                item.title_key or None,
                item.source,
                item.title,
                item.url,
                item.canonical or None,
                item.summary,
                item.author,
                iso(item.published_at) if item.published_at else None,
                iso(utcnow()),
                json.dumps(item.raw, default=str) if item.raw else None,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid or 0)

    def pending_items(self, *, limit: int | None = None, since: datetime | None = None) -> list[StoredItem]:
        """Collected items that have not been part of any digest yet."""
        sql = ["SELECT * FROM items WHERE digest_id IS NULL AND expired_at IS NULL"]
        params: list[Any] = []
        if since is not None:
            # Keep an item if it is recent, or if it carries no date at all.
            sql.append("AND (published_at IS NULL OR published_at >= ?)")
            params.append(iso(since))
        sql.append("ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC")
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(limit)
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [StoredItem.from_row(row) for row in rows]

    def count_pending(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE digest_id IS NULL AND expired_at IS NULL"
        ).fetchone()
        return int(row["n"])

    def expire_stale_pending(self, cutoff: datetime) -> int:
        """Retire pending items published before ``cutoff``.

        They were recorded (so they never come back through deduplication) but
        are too old to be worth posting.
        """
        cursor = self.conn.execute(
            """
            UPDATE items SET expired_at = ?
             WHERE digest_id IS NULL AND expired_at IS NULL
               AND published_at IS NOT NULL AND published_at < ?
            """,
            (iso(utcnow()), iso(cutoff)),
        )
        return cursor.rowcount or 0

    def items_for_digest(self, digest_id: int) -> list[StoredItem]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE digest_id = ? "
            "ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC",
            (digest_id,),
        ).fetchall()
        return [StoredItem.from_row(row) for row in rows]

    def attach_items_to_digest(self, item_ids: Iterable[int], digest_id: int) -> int:
        ids = list(item_ids)
        if not ids:
            return 0
        cursor = self.conn.executemany(
            "UPDATE items SET digest_id = ? WHERE id = ? AND digest_id IS NULL",
            [(digest_id, item_id) for item_id in ids],
        )
        return cursor.rowcount or 0

    def mark_items_sent(self, digest_id: int, sent_at: datetime) -> int:
        cursor = self.conn.execute(
            "UPDATE items SET sent_at = ? WHERE digest_id = ? AND sent_at IS NULL",
            (iso(sent_at), digest_id),
        )
        return cursor.rowcount or 0

    def release_items(self, digest_id: int) -> int:
        """Detach items from a digest so a failed digest's items are not lost."""
        cursor = self.conn.execute(
            "UPDATE items SET digest_id = NULL, sent_at = NULL, expired_at = NULL "
            "WHERE digest_id = ? AND sent_at IS NULL",
            (digest_id,),
        )
        return cursor.rowcount or 0

    def total_items(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # digests
    # ------------------------------------------------------------------

    def get_digest_for_date(self, digest_date: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM digests WHERE digest_date = ?", (digest_date,)
        ).fetchone()

    def get_digest(self, digest_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()

    def create_digest(self, *, digest_date: str, channel: str, title: str) -> int:
        """Claim today's digest slot. Raises if the day is already claimed."""
        cursor = self.conn.execute(
            """
            INSERT INTO digests (digest_date, status, channel, title, created_at)
            VALUES (?, 'pending', ?, ?, ?)
            """,
            (digest_date, channel, title, iso(utcnow())),
        )
        return int(cursor.lastrowid or 0)

    def update_digest_body(self, digest_id: int, *, body: str, item_count: int, title: str) -> None:
        self.conn.execute(
            "UPDATE digests SET body = ?, item_count = ?, title = ? WHERE id = ?",
            (body, item_count, title, digest_id),
        )

    def mark_digest_sent(self, digest_id: int, *, sent_at: datetime | None = None) -> None:
        moment = sent_at or utcnow()
        self.conn.execute(
            """
            UPDATE digests
               SET status = 'sent', sent_at = ?, attempts = attempts + 1, last_error = NULL
             WHERE id = ?
            """,
            (iso(moment), digest_id),
        )
        self.mark_items_sent(digest_id, moment)

    def mark_digest_failed(self, digest_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE digests SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:2000], digest_id),
        )

    def mark_digest_skipped(self, digest_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE digests SET status = 'skipped', last_error = ? WHERE id = ?",
            (reason[:2000], digest_id),
        )

    def recent_digests(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM digests ORDER BY digest_date DESC LIMIT ?", (limit,)
        ).fetchall()

    def last_sent_digest(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM digests WHERE status = 'sent' ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()

    def undelivered_digests(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM digests WHERE status IN ('pending', 'failed') ORDER BY digest_date ASC"
        ).fetchall()

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    def start_run(self, trigger: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs (started_at, status, trigger) VALUES (?, 'running', ?)",
            (iso(utcnow()), trigger),
        )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        items_fetched: int = 0,
        items_new: int = 0,
        digest_id: int | None = None,
        sent: bool = False,
        error: str | None = None,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, items_fetched = ?, items_new = ?,
                   digest_id = ?, sent = ?, error = ?, notes = ?
             WHERE id = ?
            """,
            (
                iso(utcnow()),
                status,
                items_fetched,
                items_new,
                digest_id,
                1 if sent else 0,
                error[:2000] if error else None,
                notes,
                run_id,
            ),
        )

    def record_source_result(self, run_id: int, outcome: SourceOutcome) -> None:
        self.conn.execute(
            """
            INSERT INTO source_results (
                run_id, source, kind, status, items_fetched, items_new,
                duration_ms, error, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                outcome.name,
                outcome.kind,
                outcome.status,
                outcome.items_fetched,
                outcome.items_new,
                outcome.duration_ms,
                outcome.error[:2000] if outcome.error else None,
                outcome.error_type,
                iso(utcnow()),
            ),
        )

    def recent_runs(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE status != 'running' ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()

    def source_results_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_results WHERE run_id = ? ORDER BY source", (run_id,)
        ).fetchall()

    def consecutive_failures(self, source: str, *, window: int = 20) -> int:
        """How many times in a row this source has failed, most recent first."""
        rows = self.conn.execute(
            "SELECT status FROM source_results WHERE source = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (source, window),
        ).fetchall()
        streak = 0
        for row in rows:
            if row["status"] == "ok":
                break
            streak += 1
        return streak

    def source_stats(self, source: str) -> dict[str, Any]:
        latest = self.conn.execute(
            "SELECT * FROM source_results WHERE source = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (source,),
        ).fetchone()
        last_ok = self.conn.execute(
            "SELECT created_at FROM source_results WHERE source = ? AND status = 'ok' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (source,),
        ).fetchone()
        totals = self.conn.execute(
            """
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(items_new), 0) AS items_new,
                   COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_runs
              FROM source_results WHERE source = ?
            """,
            (source,),
        ).fetchone()
        contributed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE source = ? AND sent_at IS NOT NULL",
            (source,),
        ).fetchone()
        return {
            "source": source,
            "last_status": latest["status"] if latest else None,
            "last_checked_at": latest["created_at"] if latest else None,
            "last_error": latest["error"] if latest else None,
            "last_error_type": latest["error_type"] if latest else None,
            "last_duration_ms": latest["duration_ms"] if latest else None,
            "last_success_at": last_ok["created_at"] if last_ok else None,
            "consecutive_failures": self.consecutive_failures(source),
            "checks": int(totals["runs"]) if totals else 0,
            "successful_checks": int(totals["ok_runs"]) if totals else 0,
            "items_new_total": int(totals["items_new"]) if totals else 0,
            "items_sent_total": int(contributed["n"]) if contributed else 0,
        }

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        level: str,
        message: str,
        source: str | None = None,
        event: str | None = None,
        run_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, level, source, event, message, detail, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso(utcnow()),
                level,
                source,
                event,
                message[:4000],
                json.dumps(detail, default=str) if detail else None,
                run_id,
            ),
        )

    def recent_events(
        self, *, limit: int = 100, levels: Sequence[str] | None = None
    ) -> list[sqlite3.Row]:
        sql = ["SELECT * FROM events"]
        params: list[Any] = []
        if levels:
            placeholders = ",".join("?" * len(levels))
            sql.append(f"WHERE level IN ({placeholders})")
            params.extend(level.upper() for level in levels)
        sql.append("ORDER BY ts DESC, id DESC LIMIT ?")
        params.append(limit)
        return self.conn.execute(" ".join(sql), params).fetchall()

    def count_events_since(self, since: datetime, *, levels: Sequence[str] = ("ERROR", "CRITICAL")) -> int:
        placeholders = ",".join("?" * len(levels))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE ts >= ? AND level IN ({placeholders})",
            [iso(since), *[level.upper() for level in levels]],
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    def prune(self, *, days: int) -> dict[str, int]:
        """Drop bulky history while keeping every fingerprint forever.

        Fingerprints are the deduplication memory, so they are never deleted;
        only raw payloads, old events and old run rows go.
        """
        cutoff = iso(utcnow() - timedelta(days=days))
        raw = self.conn.execute(
            "UPDATE items SET raw = NULL WHERE raw IS NOT NULL AND first_seen_at < ?", (cutoff,)
        ).rowcount
        events = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        runs = self.conn.execute(
            "DELETE FROM runs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,)
        ).rowcount
        self.conn.execute("VACUUM")
        return {
            "raw_payloads_cleared": max(raw, 0),
            "events_deleted": max(events, 0),
            "runs_deleted": max(runs, 0),
        }

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, params: Sequence[Any] = ()) -> int:
            row = self.conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        return {
            "database": str(self.path),
            "items_total": scalar("SELECT COUNT(*) FROM items"),
            "items_pending": scalar(
                "SELECT COUNT(*) FROM items WHERE digest_id IS NULL AND expired_at IS NULL"
            ),
            "items_expired": scalar("SELECT COUNT(*) FROM items WHERE expired_at IS NOT NULL"),
            "items_sent": scalar("SELECT COUNT(*) FROM items WHERE sent_at IS NOT NULL"),
            "digests_total": scalar("SELECT COUNT(*) FROM digests"),
            "digests_sent": scalar("SELECT COUNT(*) FROM digests WHERE status = 'sent'"),
            "digests_failed": scalar("SELECT COUNT(*) FROM digests WHERE status = 'failed'"),
            "runs_total": scalar("SELECT COUNT(*) FROM runs"),
            "events_total": scalar("SELECT COUNT(*) FROM events"),
        }


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    detail = data.get("detail")
    if isinstance(detail, str):
        try:
            data["detail"] = json.loads(detail)
        except json.JSONDecodeError:
            pass
    return data


def open_store(location: str | Path) -> Store:
    """Open the record named by ``[paths] database``.

    ``dynamodb://<table>`` opens the DynamoDB store (see dynamo.py); anything
    else is a SQLite file path. boto3 is only imported when DynamoDB is used.
    """
    text = str(location)
    if text.startswith("dynamodb://"):
        from .dynamo import DynamoStore

        return DynamoStore.from_url(text)  # type: ignore[return-value]
    if text.lower().startswith("dynamodb:"):
        # Otherwise "dynamodb:/table" would quietly become a SQLite file in a
        # folder called "dynamodb:".
        raise StoreError(f"{text!r} is not a DynamoDB URL; write it as dynamodb://<table>")
    return Store(location)


__all__ = ["Store", "StoreError", "StoreConflict", "open_store", "row_to_dict", "parse_iso", "SCHEMA_VERSION"]
