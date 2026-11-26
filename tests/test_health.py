from __future__ import annotations

import pytest

from datetime import timedelta
from pathlib import Path

from digestbot.config import Config
from digestbot.health import DEGRADED, DOWN, OK, UNKNOWN, assess
from digestbot.models import SourceOutcome, utcnow
from digestbot.store import Store, open_store
from tests.factories import make_config


def check(report, name: str):
    return next(c for c in report.checks if c.name == name)


def test_fresh_install_is_unknown_not_ok(config: Config, store: Store) -> None:
    report = assess(config, store)
    assert check(report, "digest_delivered").status == UNKNOWN
    assert check(report, "last_run").status == UNKNOWN
    assert report.status != OK  # never claim health without evidence
    assert "run `digest-bot run --force`" in check(report, "digest_delivered").detail


def test_recent_digest_is_healthy(config: Config, store: Store) -> None:
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.mark_digest_sent(digest_id)
    run_id = store.start_run("cron")
    for source in config.sources:
        store.record_source_result(run_id, SourceOutcome(name=source.name, kind=source.kind, status="ok"))
    store.finish_run(run_id, status="ok", sent=True)

    report = assess(config, store)
    assert report.status == OK
    assert report.healthy
    assert check(report, "digest_delivered").status == OK
    assert check(report, "sources").status == OK
    assert report.hours_since_digest is not None and report.hours_since_digest < 1


@pytest.mark.sqlite_only
def test_silence_past_the_limit_is_degraded_then_down(config: Config, store: Store) -> None:
    digest_id = store.create_digest(digest_date="2025-10-01", channel="#c", title="T")
    store.mark_digest_sent(digest_id, sent_at=utcnow() - timedelta(hours=30))

    report = assess(config, store)
    assert check(report, "digest_delivered").status == DEGRADED
    assert "over the 26h limit" in check(report, "digest_delivered").detail

    store.conn.execute(
        "UPDATE digests SET sent_at = ?", ((utcnow() - timedelta(hours=100)).isoformat(),)
    )
    report = assess(config, store)
    assert check(report, "digest_delivered").status == DOWN
    assert report.status == DOWN


def test_failed_digest_is_down(config: Config, store: Store) -> None:
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.mark_digest_failed(digest_id, "webhook returned HTTP 500")

    report = assess(config, store)
    assert check(report, "delivery").status == DOWN
    assert "2025-10-08" in check(report, "delivery").detail
    assert report.status == DOWN


def test_source_failing_repeatedly_is_down(config: Config, store: Store) -> None:
    run_id = store.start_run("cron")
    for _ in range(3):
        store.record_source_result(
            run_id, SourceOutcome(name="api", kind="api", status="error", error="HTTP 500")
        )
    report = assess(config, store)
    assert check(report, "sources").status == DOWN
    assert "api (3 failures)" in check(report, "sources").detail
    api = next(source for source in report.sources if source["source"] == "api")
    assert api["status"] == DOWN
    assert api["consecutive_failures"] == 3


def test_single_source_failure_is_only_degraded(config: Config, store: Store) -> None:
    run_id = store.start_run("cron")
    store.record_source_result(
        run_id, SourceOutcome(name="api", kind="api", status="error", error="timeout")
    )
    assert check(assess(config, store), "sources").status == DEGRADED


def test_disabled_source_is_not_a_problem(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        sources=[
            {"name": "feed", "kind": "rss", "url": "https://rss.example.com/feed.xml"},
            {"name": "off", "kind": "rss", "url": "https://rss.example.com/off.xml", "enabled": False},
        ],
    )
    store = open_store(config.paths.database)
    try:
        report = assess(config, store)
        off = next(source for source in report.sources if source["source"] == "off")
        assert off["status"] == "disabled"
        assert check(report, "sources").status == OK
    finally:
        store.close()


def test_failed_last_run_is_down(config: Config, store: Store) -> None:
    run_id = store.start_run("cron")
    store.finish_run(run_id, status="failed", error="delivery failed")
    report = assess(config, store)
    assert check(report, "last_run").status == DOWN
    assert "delivery failed" in check(report, "last_run").detail


def test_recent_errors_show_up_as_degraded(config: Config, store: Store) -> None:
    store.record_event(level="ERROR", message="source broke", source="api")
    assert check(assess(config, store), "log").status == DEGRADED


def test_report_serialises(config: Config, store: Store) -> None:
    data = assess(config, store).as_dict()
    assert set(data) >= {"status", "healthy", "checks", "sources", "stats", "config"}
    assert data["config"]["source_count"] == 3
    assert data["config"]["enabled_source_count"] == 3
    assert isinstance(data["checks"], list)


def test_problems_lists_only_non_ok(config: Config, store: Store) -> None:
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#c", title="T")
    store.mark_digest_sent(digest_id)
    run_id = store.start_run("cron")
    store.finish_run(run_id, status="ok")
    report = assess(config, store)
    assert all(problem.status != OK for problem in report.problems())
