"""Fixture payloads and a config factory shared by the test modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from digestbot.config import Config

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First post</title>
      <link>https://example.com/first?utm_source=rss</link>
      <guid>example-1</guid>
      <description>&lt;p&gt;A &lt;b&gt;first&lt;/b&gt; post.&lt;/p&gt;</description>
      <pubDate>Wed, 08 Oct 2025 09:00:00 GMT</pubDate>
      <author>alex@example.com</author>
    </item>
    <item>
      <title>Second post</title>
      <link>https://example.com/second</link>
      <guid>example-2</guid>
      <description>Another one.</description>
      <pubDate>Wed, 08 Oct 2025 10:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

API_FIXTURE: dict[str, Any] = {
    "meta": {"count": 2},
    "results": [
        {
            "id": 101,
            "name": "widget-2.0 released",
            "html_url": "https://api.example.com/releases/101",
            "body": "Ships the new widget.",
            "author": {"login": "maintainer"},
            "published_at": "2025-10-08T11:00:00Z",
        },
        {
            "id": 102,
            "name": "widget-2.1 released",
            "html_url": "https://api.example.com/releases/102",
            "body": "Fixes a bug.",
            "author": {"login": "maintainer"},
            "published_at": "2025-10-08T12:00:00Z",
        },
    ],
}

HTML_FIXTURE = """<!doctype html>
<html><body>
  <div class="post">
    <h3 class="post-title"><a href="/posts/alpha">Alpha announcement</a></h3>
    <div class="post-body">Alpha is out.</div>
    <abbr class="published" title="2025-10-08T08:15:00Z">today</abbr>
  </div>
  <div class="post">
    <h3 class="post-title"><a href="https://scrape.example.com/posts/beta">Beta announcement</a></h3>
    <div class="post-body">Beta is out.</div>
    <abbr class="published" title="2025-10-08T07:00:00Z">today</abbr>
  </div>
</body></html>
"""


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    """A three-source config wired to local file delivery."""
    data: dict[str, Any] = {
        "bot": {
            "timezone": "UTC",
            "send_after": "00:00",
            "title": "Test Digest",
            "max_items_per_digest": 30,
            "max_items_per_source": 10,
            "lookback_hours": 100000,  # fixtures carry fixed 2025 dates
            "post_when_empty": True,
        },
        "paths": {
            "database": str(tmp_path / "digest.db"),
            "log_file": str(tmp_path / "digest-bot.log"),
            "log_level": "DEBUG",
        },
        "delivery": {
            "kind": "file",
            "channel": "#test",
            "path": str(tmp_path / "out"),
        },
        "alerts": {"enabled": False, "kind": "file", "path": str(tmp_path / "alerts")},
        "sources": [
            {"name": "feed", "kind": "rss", "url": "https://rss.example.com/feed.xml"},
            {
                "name": "api",
                "kind": "api",
                "url": "https://api.example.com/releases",
                "items_path": "results",
                "fields": {
                    "id": "id",
                    "title": "name",
                    "url": "html_url",
                    "summary": "body",
                    "author": "author.login",
                    "published_at": "published_at",
                },
            },
            {
                "name": "page",
                "kind": "scrape",
                "url": "https://scrape.example.com/blog",
                "selectors": {
                    "item": "div.post",
                    "title": "h3.post-title a",
                    "url": "h3.post-title a@href",
                    "summary": "div.post-body",
                    "published_at": "abbr.published@title",
                },
            },
        ],
    }
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            data[section].update(values)
        else:
            data[section] = values
    config = Config.model_validate(data)
    config.config_path = tmp_path / "config.toml"
    return config
