from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from digestbot.config import Config
from digestbot.logging_setup import setup_logging
from digestbot.pipeline import Pipeline
from digestbot.store import Store
from tests.factories import API_FIXTURE, HTML_FIXTURE, RSS_FIXTURE, make_config

FEED_URL = "https://rss.example.com/feed.xml"
API_URL = "https://api.example.com/releases"
PAGE_URL = "https://scrape.example.com/blog"


def mock_all_sources_ok() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))


def pipeline_for(config: Config) -> tuple[Pipeline, Store]:
    """Wire up logging exactly the way the CLI does, so log assertions are real."""
    setup_logging(log_file=config.paths.log_file, level="DEBUG", console=False)
    store = Store(config.paths.database)
    return Pipeline(config, store), store


# ----------------------------------------------------------------------
# the happy path
# ----------------------------------------------------------------------


@respx.mock
def test_run_collects_from_all_three_source_kinds_and_delivers(config: Config) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")

        assert result.status == "ok"
        assert result.sent is True
        # 2 RSS + 2 API + 2 scraped
        assert result.collection.items_fetched == 6
        assert result.collection.items_new == 6
        assert result.digest is not None and result.digest.item_count == 6

        # Delivered to the file channel.
        written = list(Path(config.delivery.path).glob("*.md"))
        assert len(written) == 1
        body = written[0].read_text(encoding="utf-8")
        assert "First post" in body
        assert "widget-2.0 released" in body
        assert "Alpha announcement" in body
        assert "3/3 sources healthy" in body

        # The local record agrees.
        digest = store.get_digest(result.digest_id)
        assert digest["status"] == "sent"
        assert digest["item_count"] == 6
        assert store.count_pending() == 0
        assert store.stats()["items_sent"] == 6
    finally:
        store.close()


@respx.mock
def test_digest_groups_items_by_source(config: Config) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        body = result.digest.body
        assert "### feed" in body and "### api" in body and "### page" in body
    finally:
        store.close()


# ----------------------------------------------------------------------
# deduplication
# ----------------------------------------------------------------------


@respx.mock
def test_second_run_sends_nothing_new(config: Config) -> None:
    """The same sources, polled twice: no item is reported twice."""
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        first = pipeline.run(trigger="test")
        assert first.collection.items_new == 6

        second = pipeline.run(trigger="test", force=True)
        assert second.collection.items_fetched == 6
        assert second.collection.items_new == 0
        assert second.collection.items_duplicate == 6
        # Forced re-send reuses the same day's digest row and the same items.
        assert second.digest_id == first.digest_id
        assert second.digest.item_count == 6
        assert store.total_items() == 6
    finally:
        store.close()


@respx.mock
def test_same_story_from_two_sources_is_deduplicated_by_title(config: Config) -> None:
    duplicate_feed = RSS_FIXTURE.replace(
        "https://example.com/first?utm_source=rss", "https://mirror.example.net/first"
    ).replace("example-1", "mirror-1")
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get("https://rss.example.com/mirror.xml").mock(
        return_value=httpx.Response(200, text=duplicate_feed)
    )

    config = make_config(
        Path(config.paths.database).parent,
        sources=[
            {"name": "feed", "kind": "rss", "url": FEED_URL},
            {"name": "mirror", "kind": "rss", "url": "https://rss.example.com/mirror.xml"},
        ],
    )
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        # Four entries arrive; the two titles are the same story twice over.
        assert result.collection.items_fetched == 4
        assert result.collection.items_new == 2
        assert result.collection.items_duplicate == 2
    finally:
        store.close()


@respx.mock
def test_url_only_dedupe_keeps_the_same_title_from_two_sources(tmp_path: Path) -> None:
    """With dedupe_by = ["url"], a mirrored story is kept twice, by design."""
    mirror_feed = (
        RSS_FIXTURE.replace("https://example.com/first?utm_source=rss", "https://mirror.example.net/first")
        .replace("https://example.com/second", "https://mirror.example.net/second")
        .replace("example-1", "mirror-1")
        .replace("example-2", "mirror-2")
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get("https://rss.example.com/mirror.xml").mock(
        return_value=httpx.Response(200, text=mirror_feed)
    )
    sources = [
        {"name": "feed", "kind": "rss", "url": FEED_URL},
        {"name": "mirror", "kind": "rss", "url": "https://rss.example.com/mirror.xml"},
    ]

    url_only = make_config(tmp_path / "url", bot={"dedupe_by": ["url"]}, sources=sources)
    pipeline, store = pipeline_for(url_only)
    try:
        result = pipeline.run(trigger="test")
        assert result.collection.items_fetched == 4
        assert result.collection.items_new == 4  # distinct URLs, both kept
    finally:
        store.close()

    with_titles = make_config(tmp_path / "title", bot={"dedupe_by": ["url", "title"]}, sources=sources)
    pipeline, store = pipeline_for(with_titles)
    try:
        result = pipeline.run(trigger="test")
        assert result.collection.items_new == 2  # same titles collapsed
    finally:
        store.close()


# ----------------------------------------------------------------------
# a failing source must be loud but must not stop the digest
# ----------------------------------------------------------------------


@respx.mock
def test_one_broken_source_does_not_stop_the_digest(config: Config) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get(API_URL).mock(return_value=httpx.Response(500, text="upstream exploded"))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")

        assert result.status == "partial"
        assert result.sent is True
        assert [o.name for o in result.collection.failed_sources] == ["api"]
        assert result.collection.items_new == 4  # the other two sources still delivered

        # The failure is in the digest body, the run record, and the event log.
        assert "Collection problems" in result.digest.body
        assert "`api`" in result.digest.body
        assert "http_500" in result.digest.body
        assert "2/3 sources healthy" in result.digest.body

        source_results = {r["source"]: r for r in store.source_results_for_run(result.run_id)}
        assert source_results["api"]["status"] == "error"
        assert "500" in source_results["api"]["error"]

        errors = store.recent_events(levels=["ERROR"])
        assert any("api" in (row["source"] or "") for row in errors)

        # And in the log file itself.
        log_text = Path(config.paths.log_file).read_text(encoding="utf-8")
        assert "source 'api' failed" in log_text
    finally:
        store.close()


@respx.mock
def test_scraper_that_matches_nothing_is_reported(config: Config) -> None:
    """A redesigned page is the quiet failure this bot refuses to have."""
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html><body>new design</body></html>"))

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.status == "partial"
        assert "markup has probably changed" in result.digest.body
        assert store.source_stats("page")["consecutive_failures"] == 1
    finally:
        store.close()


@respx.mock
def test_every_source_failing_still_posts_a_digest(config: Config) -> None:
    """Total collection failure must still produce a message, not silence."""
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("no route"))
    respx.get(PAGE_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.status == "partial"
        assert result.sent is True
        assert result.digest.item_count == 0
        assert "No new items" in result.digest.body
        assert "0/3 sources healthy" in result.digest.body
        assert len(result.collection.failed_sources) == 3
    finally:
        store.close()


@respx.mock
def test_empty_but_reachable_source_is_warned_about(config: Config) -> None:
    empty_feed = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=empty_feed))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.status == "ok"  # empty is not an error
        assert "Reachable but returned nothing" in result.digest.body
        warnings = store.recent_events(levels=["WARNING"])
        assert any("no items at all" in row["message"] for row in warnings)
    finally:
        store.close()


# ----------------------------------------------------------------------
# delivery failure must be loud and must lose nothing
# ----------------------------------------------------------------------


@respx.mock
def test_delivery_failure_marks_digest_failed_and_keeps_items(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        delivery={"kind": "slack", "webhook_url": "https://hooks.example.com/bad", "channel": "#test", "path": str(tmp_path / "out")},
    )
    mock_all_sources_ok()
    respx.post("https://hooks.example.com/bad").mock(
        return_value=httpx.Response(500, text="slack is down")
    )

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")

        assert result.status == "failed"
        assert result.sent is False
        assert "500" in result.delivery_error

        digest = store.get_digest(result.digest_id)
        assert digest["status"] == "failed"
        assert digest["attempts"] == 1

        # Nothing was lost: every item is back in the queue for the next run.
        assert store.count_pending() == 6
        assert store.stats()["items_sent"] == 0

        run = store.last_run()
        assert run["status"] == "failed" and run["sent"] == 0

        log_text = Path(config.paths.log_file).read_text(encoding="utf-8")
        assert "DIGEST NOT DELIVERED" in log_text
        assert any("DIGEST NOT DELIVERED" in r["message"] for r in store.recent_events(levels=["CRITICAL"]))
    finally:
        store.close()


@respx.mock
def test_next_run_retries_a_failed_delivery(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        delivery={"kind": "slack", "webhook_url": "https://hooks.example.com/flaky", "channel": "#test"},
    )
    mock_all_sources_ok()
    route = respx.post("https://hooks.example.com/flaky").mock(
        return_value=httpx.Response(503, text="try later")
    )

    pipeline, store = pipeline_for(config)
    try:
        first = pipeline.run(trigger="test")
        assert first.status == "failed"

        # Slack recovers; the same day's digest goes out with the items intact.
        route.mock(return_value=httpx.Response(200, text="ok"))
        second = pipeline.run(trigger="test")

        assert second.status == "ok"
        assert second.sent is True
        assert second.digest_id == first.digest_id
        assert second.digest.item_count == 6
        assert store.get_digest(second.digest_id)["attempts"] == 2
        assert store.count_pending() == 0
    finally:
        store.close()


@respx.mock
def test_alert_channel_is_used_when_delivery_fails(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        delivery={"kind": "slack", "webhook_url": "https://hooks.example.com/bad", "channel": "#test"},
        alerts={"enabled": True, "kind": "file", "path": str(tmp_path / "alerts"), "channel": "#alerts"},
    )
    mock_all_sources_ok()
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(403, text="revoked"))

    pipeline, store = pipeline_for(config)
    try:
        pipeline.run(trigger="test")
        alerts = list((tmp_path / "alerts").glob("*.md"))
        assert len(alerts) == 1
        body = alerts[0].read_text(encoding="utf-8")
        assert "delivery FAILED" in body
        assert "403" in body
    finally:
        store.close()


@respx.mock
def test_missing_alert_channel_is_itself_warned_about(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        delivery={"kind": "slack", "webhook_url": "https://hooks.example.com/bad", "channel": "#test"},
    )
    mock_all_sources_ok()
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(500))

    pipeline, store = pipeline_for(config)
    try:
        pipeline.run(trigger="test")
        warnings = [row["message"] for row in store.recent_events(levels=["WARNING"])]
        assert any("no alert channel configured" in message for message in warnings)
    finally:
        store.close()


@respx.mock
def test_4xx_is_marked_not_retryable(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        delivery={"kind": "slack", "webhook_url": "https://hooks.example.com/gone", "channel": "#test"},
    )
    mock_all_sources_ok()
    respx.post("https://hooks.example.com/gone").mock(return_value=httpx.Response(404, text="no such hook"))

    pipeline, store = pipeline_for(config)
    try:
        pipeline.run(trigger="test")
        log_text = Path(config.paths.log_file).read_text(encoding="utf-8")
        assert "retryable=False" in log_text
    finally:
        store.close()


# ----------------------------------------------------------------------
# one digest a day
# ----------------------------------------------------------------------


@respx.mock
def test_second_run_on_the_same_day_does_not_post_again(config: Config) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        first = pipeline.run(trigger="test")
        assert first.sent is True

        second = pipeline.run(trigger="test")
        assert second.sent is False
        assert second.skipped_reason is not None
        assert "already sent" in second.skipped_reason
        # Still only one file in the channel.
        assert len(list(Path(config.delivery.path).glob("*.md"))) == 1
        assert store.stats()["digests_sent"] == 1
    finally:
        store.close()


@respx.mock
def test_before_the_send_window_it_only_collects(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"send_after": "23:59"})
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="cron")
        assert result.sent is False
        assert "before the send window" in result.skipped_reason
        # Collection still happened, so the digest has material later.
        assert result.collection.items_new == 6
        assert store.count_pending() == 6
        assert store.stats()["digests_total"] == 0
    finally:
        store.close()


@respx.mock
def test_force_overrides_the_send_window(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"send_after": "23:59"})
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="manual", force=True)
        assert result.sent is True
        assert result.digest.item_count == 6
    finally:
        store.close()


@respx.mock
def test_yesterdays_undelivered_digest_is_folded_into_today(config: Config) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        # A digest from an earlier day that never went out, holding one item.
        from digestbot.models import Item

        orphan = Item(source="feed", title="Yesterday's item", url="https://example.com/yesterday")
        store.add_item(orphan, orphan.fingerprint(["url", "title"]))
        stale_id = store.create_digest(digest_date="2000-01-01", channel="#test", title="Old")
        store.attach_items_to_digest([store.pending_items()[0].id], stale_id)
        store.mark_digest_failed(stale_id, "webhook was down")

        result = pipeline.run(trigger="test")

        assert result.sent is True
        # The orphaned item rides along in today's digest.
        assert "Yesterday's item" in result.digest.body
        assert result.digest.item_count == 7
        assert store.get_digest(stale_id)["status"] == "skipped"
        warnings = [row["message"] for row in store.recent_events(levels=["WARNING"])]
        assert any("never delivered" in message for message in warnings)
    finally:
        store.close()


# ----------------------------------------------------------------------
# empty digests, dry runs, caps
# ----------------------------------------------------------------------


@respx.mock
def test_empty_digest_is_posted_by_default(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    pipeline, store = pipeline_for(config)
    try:
        pipeline.run(trigger="test")  # consumes everything
        # Next day, nothing new at all.
        store.conn.execute("UPDATE digests SET digest_date = '2000-01-01'")
        result = pipeline.run(trigger="test")

        assert result.sent is True
        assert result.digest.item_count == 0
        assert "proof of life" in result.digest.body
    finally:
        store.close()


@respx.mock
def test_post_when_empty_false_skips_and_warns(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"post_when_empty": False})
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text='<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html><body></body></html>"))

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.sent is False
        assert "post_when_empty" in result.skipped_reason
        warnings = [row["message"] for row in store.recent_events(levels=["WARNING"])]
        assert any("look identical to a broken bot" in message for message in warnings)
    finally:
        store.close()


@respx.mock
def test_dry_run_delivers_nothing_and_keeps_items_pending(config: Config) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test", dry_run=True)
        assert result.sent is False
        assert result.digest.item_count == 6
        assert not list(Path(config.delivery.path).glob("*.md"))
        # Items stay pending so a dry run never eats a day's content.
        assert store.count_pending() == 6
        assert store.stats()["items_sent"] == 0
    finally:
        store.close()


@respx.mock
def test_per_digest_cap_holds_the_rest_back(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"max_items_per_digest": 2})
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.digest.item_count == 2
        assert "further item(s) held back" in result.digest.body
        assert store.count_pending() == 4
    finally:
        store.close()


@respx.mock
def test_per_source_cap_is_applied(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"max_items_per_source": 1})
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.collection.items_new == 3  # one per source
    finally:
        store.close()


@respx.mock
def test_items_older_than_lookback_are_recorded_but_not_posted(tmp_path: Path) -> None:
    config = make_config(tmp_path, bot={"lookback_hours": 1})
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        # The fixtures are dated 2025, well outside a one-hour window.
        assert result.collection.items_new == 6
        assert result.digest.item_count == 0
        assert result.expired_items == 6
        assert store.stats()["items_expired"] == 6
        # They are still on record, so they never reappear.
        assert store.total_items() == 6
    finally:
        store.close()


# ----------------------------------------------------------------------
# crashes
# ----------------------------------------------------------------------


@respx.mock
def test_unexpected_adapter_crash_is_recorded_not_swallowed(config: Config, monkeypatch) -> None:
    mock_all_sources_ok()

    from digestbot.sources import rss as rss_module

    def explode(self, source, client):
        raise ZeroDivisionError("bug in the adapter")

    monkeypatch.setattr(rss_module.RssFetcher, "fetch", explode)

    pipeline, store = pipeline_for(config)
    try:
        result = pipeline.run(trigger="test")
        assert result.status == "partial"
        assert result.sent is True  # the other two sources still produced a digest
        failed = result.collection.failed_sources[0]
        assert failed.error_type == "unexpected"
        assert "ZeroDivisionError" in failed.error
        log_text = Path(config.paths.log_file).read_text(encoding="utf-8")
        assert "bug in the adapter" in log_text
        assert "Traceback" in log_text
    finally:
        store.close()


@respx.mock
def test_pipeline_crash_marks_the_run_failed_and_reraises(config: Config, monkeypatch) -> None:
    mock_all_sources_ok()
    pipeline, store = pipeline_for(config)
    try:
        monkeypatch.setattr(
            pipeline, "_run_inner", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone"))
        )
        with pytest.raises(RuntimeError, match="disk gone"):
            pipeline.run(trigger="test")

        run = store.last_run()
        assert run["status"] == "failed"
        assert "disk gone" in run["error"]
        assert any("crashed" in r["message"] for r in store.recent_events(levels=["CRITICAL"]))
    finally:
        store.close()


# ----------------------------------------------------------------------
# send window helper
# ----------------------------------------------------------------------


def test_send_window_open(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = make_config(tmp_path, bot={"send_after": "08:00", "timezone": "UTC"})
    pipeline, store = pipeline_for(config)
    try:
        tz = ZoneInfo("UTC")
        assert not pipeline.send_window_open(now=datetime(2025, 10, 8, 7, 59, tzinfo=tz))
        assert pipeline.send_window_open(now=datetime(2025, 10, 8, 8, 0, tzinfo=tz))
        assert pipeline.send_window_open(now=datetime(2025, 10, 8, 23, 0, tzinfo=tz))
    finally:
        store.close()
