from __future__ import annotations

from datetime import datetime, timezone

from digestbot.delivery.slack import chunk_markdown
from digestbot.models import SourceOutcome, StoredItem
from digestbot.render import escape_slack, render_digest


def stored(
    item_id: int, source: str, title: str, url: str | None = "https://example.com/x", **kwargs
) -> StoredItem:
    return StoredItem(
        id=item_id,
        fingerprint=f"fp-{item_id}",
        source=source,
        title=title,
        url=url,
        summary=kwargs.get("summary"),
        author=kwargs.get("author"),
        published_at=kwargs.get("published_at", datetime(2025, 10, 8, 9, 0, tzinfo=timezone.utc)),
        first_seen_at=datetime(2025, 10, 8, 9, 5, tzinfo=timezone.utc),
        digest_id=1,
        sent_at=None,
    )


def test_markdown_digest_groups_and_links() -> None:
    items = [
        stored(1, "feed", "First post", "https://example.com/1", summary="A summary."),
        stored(2, "feed", "Second post", "https://example.com/2"),
        stored(3, "api", "A release", "https://example.com/3"),
    ]
    rendered = render_digest(
        items,
        [SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=2)],
        title="Daily Digest",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert rendered.subject == "Daily Digest - 2025-10-08"
    assert rendered.item_count == 3
    assert "3 new items from 2 sources" in rendered.body
    assert "### feed" in rendered.body and "### api" in rendered.body
    assert "[First post](https://example.com/1)" in rendered.body
    assert "A summary." in rendered.body
    assert rendered.sources_included == ["api", "feed"]


def test_singular_wording() -> None:
    rendered = render_digest(
        [stored(1, "feed", "Only one")],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert "1 new item from 1 source." in rendered.body


def test_slack_flavour_uses_slack_link_syntax_and_escapes() -> None:
    rendered = render_digest(
        [stored(1, "feed", "Tom & Jerry <fight>", "https://example.com/1")],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
        flavour="slack",
    )
    assert "<https://example.com/1|Tom &amp; Jerry &lt;fight&gt;>" in rendered.body
    assert "[Tom" not in rendered.body


def test_item_without_url_is_still_rendered() -> None:
    rendered = render_digest(
        [stored(1, "feed", "No link here", None)],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert "**No link here**" in rendered.body


def test_empty_digest_says_so_and_explains_itself() -> None:
    rendered = render_digest(
        [],
        [SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=3)],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert rendered.is_empty
    assert "No new items" in rendered.body
    assert "proof of life" in rendered.body
    assert "1/1 sources healthy" in rendered.body


def test_failures_are_rendered_into_the_body() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post")],
        [
            SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=1),
            SourceOutcome(
                name="api",
                kind="api",
                status="error",
                error="api.example.com returned HTTP 500",
                error_type="http_500",
            ),
        ],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert rendered.problem_count == 1
    assert "### Collection problems" in rendered.body
    assert "http_500" in rendered.body
    assert "returned HTTP 500" in rendered.body
    assert "1/2 sources healthy" in rendered.body


def test_reachable_but_empty_sources_are_listed() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post")],
        [
            SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=1),
            SourceOutcome(name="quiet", kind="rss", status="ok", items_fetched=0),
        ],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert "Reachable but returned nothing" in rendered.body
    assert "`quiet`" in rendered.body


def test_labels_replace_source_names() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post")],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
        labels={"feed": "Hacker News front page"},
    )
    assert "### Hacker News front page" in rendered.body


def test_overflow_notice() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post")],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
        pending_overflow=12,
    )
    assert "12 further item(s) held back" in rendered.body


def test_timezone_is_applied_to_item_times() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post", published_at=datetime(2025, 10, 8, 13, 0, tzinfo=timezone.utc))],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="America/Toronto",  # UTC-4 in October
    )
    assert "09:00" in rendered.body


def test_long_summary_is_truncated() -> None:
    rendered = render_digest(
        [stored(1, "feed", "A post", summary="x" * 400)],
        [],
        title="D",
        digest_date="2025-10-08",
        timezone_name="UTC",
    )
    assert "..." in rendered.body
    assert "x" * 400 not in rendered.body


def test_escape_slack() -> None:
    assert escape_slack("a & b < c > d") == "a &amp; b &lt; c &gt; d"


# ----------------------------------------------------------------------
# Slack block chunking
# ----------------------------------------------------------------------


def test_chunk_markdown_keeps_short_text_whole() -> None:
    assert chunk_markdown("short text") == ["short text"]


def test_chunk_markdown_splits_on_paragraphs() -> None:
    text = "\n\n".join(["para " + "y" * 1000 for _ in range(5)])
    chunks = chunk_markdown(text, limit=2900)
    assert len(chunks) > 1
    assert all(len(chunk) <= 2900 for chunk in chunks)
    assert "".join(chunks).count("y") == text.count("y")


def test_chunk_markdown_splits_an_oversized_paragraph() -> None:
    chunks = chunk_markdown("z" * 7000, limit=2900)
    assert len(chunks) == 3
    assert all(len(chunk) <= 2900 for chunk in chunks)


def test_chunk_markdown_never_returns_empty() -> None:
    assert chunk_markdown("") == ["(empty digest)"]
