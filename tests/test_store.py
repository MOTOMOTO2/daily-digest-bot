from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from digestbot.models import Item, SourceOutcome, utcnow
from digestbot.store import Store, StoreConflict


def item(title: str, url: str | None = None, **kwargs) -> Item:
    return Item(source=kwargs.pop("source", "feed"), title=title, url=url, **kwargs)


def test_add_item_is_idempotent_on_fingerprint(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    assert store.add_item(one, one.fingerprint(["url"])) is not None
    # Same fingerprint, second attempt: recorded once.
    assert store.add_item(one, one.fingerprint(["url"])) is None
    assert store.total_items() == 1


@pytest.mark.sqlite_only
def test_fingerprint_unique_constraint_is_the_authority(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    store.add_item(one, "fixed-fingerprint")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO items (fingerprint, source, title, first_seen_at) VALUES (?, ?, ?, ?)",
            ("fixed-fingerprint", "feed", "another", "2025-01-01T00:00:00+00:00"),
        )


def test_seen_fingerprints_and_title_keys(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    store.add_item(one, one.fingerprint(["url"]))
    assert store.seen_fingerprints([one.fingerprint(["url"]), "nope"]) == {one.fingerprint(["url"])}
    assert store.seen_title_keys([one.title_key]) == {one.title_key}
    assert store.seen_fingerprints([]) == set()
    assert store.seen_title_keys([""]) == set()


def test_seen_fingerprints_handles_large_batches(store: Store) -> None:
    """More than the 500-item chunk size, to exercise the chunking path."""
    fingerprints = []
    for index in range(1200):
        one = item(f"post {index}", f"https://example.com/{index}")
        fingerprint = one.fingerprint(["url"])
        store.add_item(one, fingerprint)
        fingerprints.append(fingerprint)
    assert len(store.seen_fingerprints(fingerprints)) == 1200


def test_pending_items_excludes_sent_and_expired(store: Store) -> None:
    for index in range(3):
        one = item(f"post {index}", f"https://example.com/{index}")
        store.add_item(one, one.fingerprint(["url"]))
    assert store.count_pending() == 3

    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    first = store.pending_items()[0]
    store.attach_items_to_digest([first.id], digest_id)
    assert store.count_pending() == 2

    store.mark_digest_sent(digest_id)
    assert store.items_for_digest(digest_id)[0].sent_at is not None
    assert store.count_pending() == 2


def test_pending_items_respects_since(store: Store) -> None:
    old = item("old", "https://example.com/old", published_at=utcnow() - timedelta(days=10))
    new = item("new", "https://example.com/new", published_at=utcnow())
    undated = item("undated", "https://example.com/undated")
    for one in (old, new, undated):
        store.add_item(one, one.fingerprint(["url"]))

    titles = {item.title for item in store.pending_items(since=utcnow() - timedelta(days=1))}
    # Undated items are kept: dropping them would silently lose sources that
    # publish no timestamps at all.
    assert titles == {"new", "undated"}


def test_expire_stale_pending(store: Store) -> None:
    old = item("old", "https://example.com/old", published_at=utcnow() - timedelta(days=10))
    new = item("new", "https://example.com/new", published_at=utcnow())
    for one in (old, new):
        store.add_item(one, one.fingerprint(["url"]))

    assert store.expire_stale_pending(utcnow() - timedelta(days=1)) == 1
    assert [one.title for one in store.pending_items()] == ["new"]
    # Expiring keeps the fingerprint, so the item never comes back.
    assert store.seen_fingerprints([old.fingerprint(["url"])])
    assert store.stats()["items_expired"] == 1


def test_one_digest_per_day_is_enforced_by_the_schema(store: Store) -> None:
    store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    with pytest.raises((sqlite3.IntegrityError, StoreConflict)):
        store.create_digest(digest_date="2025-10-08", channel="#c", title="T")


def test_failed_digest_releases_items_back_to_pending(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    store.add_item(one, one.fingerprint(["url"]))
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.attach_items_to_digest([store.pending_items()[0].id], digest_id)
    assert store.count_pending() == 0

    store.mark_digest_failed(digest_id, "webhook returned HTTP 500")
    assert store.release_items(digest_id) == 1
    assert store.count_pending() == 1

    row = store.get_digest(digest_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "500" in row["last_error"]


def test_release_does_not_unsend_a_delivered_item(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    store.add_item(one, one.fingerprint(["url"]))
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.attach_items_to_digest([store.pending_items()[0].id], digest_id)
    store.mark_digest_sent(digest_id)

    assert store.release_items(digest_id) == 0
    assert store.count_pending() == 0


def test_undelivered_digests_and_last_sent(store: Store) -> None:
    pending_id = store.create_digest(digest_date="2025-10-06", channel="#c", title="T")
    failed_id = store.create_digest(digest_date="2025-10-07", channel="#c", title="T")
    store.mark_digest_failed(failed_id, "boom")
    sent_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.mark_digest_sent(sent_id)

    dates = [row["digest_date"] for row in store.undelivered_digests()]
    assert dates == ["2025-10-06", "2025-10-07"]
    assert store.last_sent_digest()["id"] == sent_id
    assert pending_id != failed_id


def test_consecutive_failures_counts_only_the_current_streak(store: Store) -> None:
    run_id = store.start_run("test")
    for status in ("error", "ok", "error", "error"):
        store.record_source_result(
            run_id, SourceOutcome(name="feed", kind="rss", status=status, error="x")
        )
    assert store.consecutive_failures("feed") == 2

    store.record_source_result(run_id, SourceOutcome(name="feed", kind="rss", status="ok"))
    assert store.consecutive_failures("feed") == 0
    assert store.consecutive_failures("never-seen") == 0


def test_source_stats(store: Store) -> None:
    run_id = store.start_run("test")
    store.record_source_result(
        run_id, SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=5, items_new=3)
    )
    store.record_source_result(
        run_id,
        SourceOutcome(name="feed", kind="rss", status="error", error="HTTP 500", error_type="http_500"),
    )
    stats = store.source_stats("feed")
    assert stats["last_status"] == "error"
    assert stats["last_error"] == "HTTP 500"
    assert stats["consecutive_failures"] == 1
    assert stats["checks"] == 2
    assert stats["successful_checks"] == 1
    assert stats["items_new_total"] == 3


def test_runs_lifecycle(store: Store) -> None:
    run_id = store.start_run("cron")
    assert store.last_run() is None  # still running
    store.finish_run(run_id, status="ok", items_fetched=4, items_new=2, sent=True)
    row = store.last_run()
    assert row["status"] == "ok" and row["sent"] == 1 and row["trigger"] == "cron"


def test_events_are_recorded_and_counted(store: Store) -> None:
    store.record_event(level="ERROR", message="source broke", source="feed", event="collect.failed")
    store.record_event(level="INFO", message="all good", event="run.start")
    assert len(store.recent_events()) == 2
    assert len(store.recent_events(levels=["ERROR"])) == 1
    assert store.count_events_since(utcnow() - timedelta(hours=1)) == 1
    assert store.count_events_since(utcnow() + timedelta(hours=1)) == 0


@pytest.mark.sqlite_only
def test_prune_keeps_fingerprints(store: Store) -> None:
    one = item("A post", "https://example.com/a", raw={"big": "payload"})
    store.add_item(one, one.fingerprint(["url"]))
    store.conn.execute(
        "UPDATE items SET first_seen_at = ?", ("2020-01-01T00:00:00+00:00",)
    )
    store.record_event(level="ERROR", message="old")
    store.conn.execute("UPDATE events SET ts = ?", ("2020-01-01T00:00:00+00:00",))

    result = store.prune(days=30)
    assert result["raw_payloads_cleared"] == 1
    assert result["events_deleted"] == 1
    # The fingerprint survives, so deduplication memory is intact.
    assert store.seen_fingerprints([one.fingerprint(["url"])])
    assert store.total_items() == 1


def test_store_reopen_is_safe(tmp_path) -> None:
    path = tmp_path / "nested" / "digest.db"
    first = Store(path)
    one = item("A post", "https://example.com/a")
    first.add_item(one, one.fingerprint(["url"]))
    first.close()

    second = Store(path)
    assert second.total_items() == 1
    second.close()


@pytest.mark.sqlite_only
def test_transaction_rolls_back(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            one = item("A post", "https://example.com/a")
            store.add_item(one, one.fingerprint(["url"]))
            raise RuntimeError("abort")
    assert store.total_items() == 0


def test_mark_digest_sent_stamps_items(store: Store) -> None:
    one = item("A post", "https://example.com/a")
    store.add_item(one, one.fingerprint(["url"]))
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.attach_items_to_digest([store.pending_items()[0].id], digest_id)
    when = datetime(2025, 10, 8, 12, 0, tzinfo=timezone.utc)
    store.mark_digest_sent(digest_id, sent_at=when)

    stored = store.items_for_digest(digest_id)[0]
    assert stored.sent_at == when
    assert store.stats()["items_sent"] == 1
