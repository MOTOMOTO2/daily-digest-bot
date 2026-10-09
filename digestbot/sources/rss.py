"""RSS and Atom feeds, via feedparser."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from ..config import RssSource, SourceConfig
from ..models import Item, parse_iso
from .base import Fetcher, SourceError, http_get

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None, *, limit: int = 600) -> str | None:
    if not text:
        return None
    cleaned = TAG_RE.sub(" ", text)
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:limit].rstrip() + ("..." if len(cleaned) > limit else "")


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        parsed_iso = parse_iso(value) if isinstance(value, str) else None
        if parsed_iso:
            return parsed_iso
    return None


def _entry_summary(entry: Any) -> str | None:
    content = entry.get("content")
    if isinstance(content, list) and content:
        value = content[0].get("value") if isinstance(content[0], dict) else None
        summary = strip_html(value)
        if summary:
            return summary
    return strip_html(entry.get("summary") or entry.get("description"))


class RssFetcher(Fetcher):
    kind = "rss"

    def fetch(self, source: SourceConfig, client: httpx.Client) -> list[Item]:
        assert isinstance(source, RssSource)
        response = http_get(source, client)

        parsed = feedparser.parse(response.content)
        # bozo means the XML was malformed. feedparser often still recovers
        # entries, so only treat it as fatal when nothing came out.
        if parsed.bozo and not parsed.entries:
            reason = getattr(parsed, "bozo_exception", "unknown parse error")
            raise SourceError(f"feed at {source.url} is not parseable: {reason}", kind="parse")

        items: list[Item] = []
        for entry in parsed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip() or None
            if not title and not link:
                continue
            author = entry.get("author") or None
            if not author:
                detail = entry.get("author_detail")
                if isinstance(detail, dict):
                    author = detail.get("name")
            external_id = entry.get("id") or entry.get("guid") or None
            items.append(
                Item(
                    source=source.name,
                    title=title or link or "(untitled)",
                    url=link,
                    summary=_entry_summary(entry),
                    author=author,
                    published_at=_entry_datetime(entry),
                    external_id=str(external_id) if external_id else None,
                    raw={
                        "feed_title": parsed.feed.get("title") if parsed.get("feed") else None,
                        "tags": [
                            tag.get("term")
                            for tag in entry.get("tags", [])
                            if isinstance(tag, dict) and tag.get("term")
                        ],
                    },
                )
            )
        return items
