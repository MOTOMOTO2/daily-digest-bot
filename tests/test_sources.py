from __future__ import annotations

import httpx
import pytest
import respx

from digestbot.config import ApiSource, Config, RssSource, ScrapeSource
from digestbot.sources import ApiFetcher, RssFetcher, ScrapeFetcher, apply_filters
from digestbot.sources.api import MISSING, dig
from digestbot.sources.base import SourceError
from digestbot.sources.scrape import split_selector

from tests.factories import API_FIXTURE, HTML_FIXTURE, RSS_FIXTURE, make_config


@pytest.fixture
def client():
    with httpx.Client() as client:
        yield client


def source_of(config: Config, name: str):
    found = config.source_by_name(name)
    assert found is not None
    return found


# ----------------------------------------------------------------------
# RSS
# ----------------------------------------------------------------------


@respx.mock
def test_rss_parses_entries(tmp_path, client) -> None:
    respx.get("https://rss.example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=RSS_FIXTURE)
    )
    items = RssFetcher().fetch(source_of(make_config(tmp_path), "feed"), client)

    assert [item.title for item in items] == ["First post", "Second post"]
    assert items[0].url == "https://example.com/first?utm_source=rss"
    # The tracking parameter survives in the stored URL but not in the identity.
    assert items[0].canonical == "https://example.com/first"
    assert items[0].external_id == "example-1"
    assert items[0].summary == "A first post."  # HTML stripped
    assert items[0].published_at is not None
    assert items[0].published_at.hour == 9


@respx.mock
def test_rss_http_error_becomes_source_error(tmp_path, client) -> None:
    respx.get("https://rss.example.com/feed.xml").mock(return_value=httpx.Response(503, text="busy"))
    with pytest.raises(SourceError, match="HTTP 503") as caught:
        RssFetcher().fetch(source_of(make_config(tmp_path), "feed"), client)
    assert caught.value.kind == "http_503"


@respx.mock
def test_rss_network_error_becomes_source_error(tmp_path, client) -> None:
    respx.get("https://rss.example.com/feed.xml").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(SourceError, match="network error") as caught:
        RssFetcher().fetch(source_of(make_config(tmp_path), "feed"), client)
    assert caught.value.kind == "network"


@respx.mock
def test_rss_timeout_becomes_source_error(tmp_path, client) -> None:
    respx.get("https://rss.example.com/feed.xml").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SourceError, match="timed out") as caught:
        RssFetcher().fetch(source_of(make_config(tmp_path), "feed"), client)
    assert caught.value.kind == "timeout"


@respx.mock
def test_rss_unparseable_body_raises(tmp_path, client) -> None:
    respx.get("https://rss.example.com/feed.xml").mock(
        return_value=httpx.Response(200, text="<<< definitely not a feed")
    )
    with pytest.raises(SourceError, match="not parseable"):
        RssFetcher().fetch(source_of(make_config(tmp_path), "feed"), client)


# ----------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------


def test_dig_follows_paths() -> None:
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert dig(data, "a.b.1.c") == 2
    assert dig(data, "") is data
    assert dig(data, "a.missing") is MISSING
    assert dig(data, "a.b.9.c") is MISSING
    assert dig(data, "a.b.c") is MISSING


@respx.mock
def test_api_maps_fields(tmp_path, client) -> None:
    respx.get("https://api.example.com/releases").mock(
        return_value=httpx.Response(200, json=API_FIXTURE)
    )
    items = ApiFetcher().fetch(source_of(make_config(tmp_path), "api"), client)

    assert [item.title for item in items] == ["widget-2.0 released", "widget-2.1 released"]
    assert items[0].url == "https://api.example.com/releases/101"
    assert items[0].author == "maintainer"
    assert items[0].external_id == "101"
    assert items[0].published_at is not None and items[0].published_at.hour == 11


@respx.mock
def test_api_bad_items_path_is_loud(tmp_path, client) -> None:
    config = make_config(tmp_path, sources=[
        {
            "name": "api",
            "kind": "api",
            "url": "https://api.example.com/releases",
            "items_path": "data.records",
            "fields": {"title": "name"},
        }
    ])
    respx.get("https://api.example.com/releases").mock(
        return_value=httpx.Response(200, json=API_FIXTURE)
    )
    with pytest.raises(SourceError, match="items_path='data.records' not found") as caught:
        ApiFetcher().fetch(source_of(config, "api"), client)
    assert caught.value.kind == "mapping"
    # The message names what the response actually held, so the fix is obvious.
    assert "results" in str(caught.value)


@respx.mock
def test_api_title_path_that_matches_nothing_is_loud(tmp_path, client) -> None:
    config = make_config(tmp_path, sources=[
        {
            "name": "api",
            "kind": "api",
            "url": "https://api.example.com/releases",
            "items_path": "results",
            "fields": {"title": "headline"},
        }
    ])
    respx.get("https://api.example.com/releases").mock(
        return_value=httpx.Response(200, json=API_FIXTURE)
    )
    with pytest.raises(SourceError, match="API shape probably changed"):
        ApiFetcher().fetch(source_of(config, "api"), client)


@respx.mock
def test_api_non_json_is_loud(tmp_path, client) -> None:
    respx.get("https://api.example.com/releases").mock(
        return_value=httpx.Response(200, text="<html>login page</html>")
    )
    with pytest.raises(SourceError, match="did not return JSON") as caught:
        ApiFetcher().fetch(source_of(make_config(tmp_path), "api"), client)
    assert caught.value.kind == "parse"


@respx.mock
def test_api_root_list_and_epoch_dates(tmp_path, client) -> None:
    config = make_config(tmp_path, sources=[
        {
            "name": "api",
            "kind": "api",
            "url": "https://api.example.com/flat",
            "items_path": "",
            "fields": {"title": "t", "published_at": "ts"},
        }
    ])
    respx.get("https://api.example.com/flat").mock(
        return_value=httpx.Response(200, json=[{"t": "one", "ts": 1759914000}])
    )
    items = ApiFetcher().fetch(source_of(config, "api"), client)
    assert items[0].title == "one"
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2025


@respx.mock
def test_api_empty_list_is_not_an_error(tmp_path, client) -> None:
    respx.get("https://api.example.com/releases").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert ApiFetcher().fetch(source_of(make_config(tmp_path), "api"), client) == []


@respx.mock
def test_api_post_with_body(tmp_path, client) -> None:
    config = make_config(tmp_path, sources=[
        {
            "name": "api",
            "kind": "api",
            "url": "https://api.example.com/search",
            "method": "POST",
            "json_body": {"query": "python"},
            "items_path": "hits",
            "fields": {"title": "title"},
        }
    ])
    route = respx.post("https://api.example.com/search").mock(
        return_value=httpx.Response(200, json={"hits": [{"title": "found"}]})
    )
    items = ApiFetcher().fetch(source_of(config, "api"), client)
    assert items[0].title == "found"
    assert route.called


# ----------------------------------------------------------------------
# Scraping
# ----------------------------------------------------------------------


def test_split_selector() -> None:
    assert split_selector("a@href") == ("a", "href")
    assert split_selector("h2 a") == ("h2 a", None)
    assert split_selector("@data-id") == ("", "data-id")


@respx.mock
def test_scrape_extracts_rows_and_resolves_relative_urls(tmp_path, client) -> None:
    respx.get("https://scrape.example.com/blog").mock(
        return_value=httpx.Response(200, text=HTML_FIXTURE)
    )
    items = ScrapeFetcher().fetch(source_of(make_config(tmp_path), "page"), client)

    assert [item.title for item in items] == ["Alpha announcement", "Beta announcement"]
    assert items[0].url == "https://scrape.example.com/posts/alpha"
    assert items[1].url == "https://scrape.example.com/posts/beta"
    assert items[0].summary == "Alpha is out."
    assert items[0].published_at is not None and items[0].published_at.hour == 8


@respx.mock
def test_scrape_selector_matching_nothing_is_loud(tmp_path, client) -> None:
    """The classic silent failure: the page was redesigned. It must shout."""
    respx.get("https://scrape.example.com/blog").mock(
        return_value=httpx.Response(200, text="<html><body><main>redesigned</main></body></html>")
    )
    with pytest.raises(SourceError, match="matched no elements") as caught:
        ScrapeFetcher().fetch(source_of(make_config(tmp_path), "page"), client)
    assert caught.value.kind == "selector"
    assert "markup has probably changed" in str(caught.value)


@respx.mock
def test_scrape_rows_without_titles_is_loud(tmp_path, client) -> None:
    respx.get("https://scrape.example.com/blog").mock(
        return_value=httpx.Response(200, text='<div class="post"><p>no heading here</p></div>')
    )
    with pytest.raises(SourceError, match="title selector"):
        ScrapeFetcher().fetch(source_of(make_config(tmp_path), "page"), client)


# ----------------------------------------------------------------------
# filters
# ----------------------------------------------------------------------


def test_include_and_exclude_filters(tmp_path) -> None:
    from digestbot.models import Item

    config = make_config(tmp_path)
    source = source_of(config, "feed")
    items = [
        Item(source="feed", title="Python 3.14 released"),
        Item(source="feed", title="We are hiring engineers"),
        Item(source="feed", title="Rust news", summary="mentions python too"),
    ]

    only_python = source.model_copy(update={"include": ["python"]})
    kept, dropped = apply_filters(only_python, items)
    assert [item.title for item in kept] == ["Python 3.14 released", "Rust news"]
    assert dropped == 1

    no_hiring = source.model_copy(update={"exclude": ["hiring"]})
    kept, dropped = apply_filters(no_hiring, items)
    assert dropped == 1
    assert all("hiring" not in item.title for item in kept)

    kept, dropped = apply_filters(source, items)
    assert dropped == 0 and len(kept) == 3
