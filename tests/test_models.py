from __future__ import annotations

from datetime import datetime, timezone

import pytest

from digestbot.models import Item, canonical_url, normalize_title, parse_iso


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Example.com/post/", "https://example.com/post"),
        ("https://www.example.com/post", "https://example.com/post"),
        ("http://example.com/post#section", "http://example.com/post"),
        ("https://example.com/post?utm_source=rss&id=7", "https://example.com/post?id=7"),
        ("https://example.com/post?b=2&a=1", "https://example.com/post?a=1&b=2"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
    ],
)
def test_canonical_url_normalizes(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_canonical_url_handles_empty() -> None:
    assert canonical_url("") == ""


def test_normalize_title_folds_case_and_punctuation() -> None:
    assert normalize_title("The  Big: News!") == normalize_title("the big news")


def test_fingerprint_prefers_external_id() -> None:
    first = Item(source="s", title="A", url="https://a.example/1", external_id="abc")
    second = Item(source="s", title="Different", url="https://b.example/2", external_id="abc")
    assert first.fingerprint(["url", "title"]) == second.fingerprint(["url", "title"])


def test_fingerprint_matches_on_canonical_url() -> None:
    first = Item(source="one", title="A post", url="https://example.com/p?utm_medium=x")
    second = Item(source="two", title="A post, revisited", url="https://www.example.com/p/")
    assert first.fingerprint(["url"]) == second.fingerprint(["url"])


def test_fingerprint_falls_back_to_title_when_no_url() -> None:
    first = Item(source="one", title="Same Story")
    second = Item(source="two", title="same   story")
    assert first.fingerprint(["url", "title"]) == second.fingerprint(["url", "title"])


def test_fingerprint_without_title_dedupe_scopes_titles_to_source() -> None:
    first = Item(source="one", title="Same Story")
    second = Item(source="two", title="Same Story")
    assert first.fingerprint(["url"]) != second.fingerprint(["url"])


def test_item_strips_whitespace() -> None:
    item = Item(source="s", title="  spaced   out  ", summary="a\n\nb")
    assert item.title == "spaced out"
    assert item.summary == "a b"


def test_parse_iso_accepts_z_suffix() -> None:
    assert parse_iso("2025-10-08T09:00:00Z") == datetime(2025, 10, 8, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_assumes_utc_when_naive() -> None:
    assert parse_iso("2025-10-08T09:00:00") == datetime(2025, 10, 8, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_returns_none_on_garbage() -> None:
    assert parse_iso("not a date") is None
    assert parse_iso(None) is None


def test_age_hours() -> None:
    now = datetime(2025, 10, 8, 12, 0, tzinfo=timezone.utc)
    item = Item(source="s", title="t", published_at=datetime(2025, 10, 8, 9, 0, tzinfo=timezone.utc))
    assert item.age_hours(now=now) == pytest.approx(3.0)
    assert Item(source="s", title="t").age_hours(now=now) is None
