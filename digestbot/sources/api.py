"""JSON APIs.

The response shape is described declaratively in config: ``items_path`` points
at the list, and ``fields`` maps digest fields to dotted paths inside each
element. No code changes needed to add an API.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import ApiSource, SourceConfig
from ..models import Item, parse_iso
from .base import Fetcher, SourceError, http_request

MISSING = object()


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path through dicts and lists.

    Numeric segments index into lists (``items.0.title``). Returns MISSING when
    the path does not resolve, which the caller reports as a mapping problem
    rather than silently inserting an empty string.
    """
    if path == "":
        return data
    current = data
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list):
            if not segment.lstrip("-").isdigit():
                return MISSING
            index = int(segment)
            if not (-len(current) <= index < len(current)):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _as_text(value: Any, *, limit: int = 600) -> str | None:
    if value is None or value is MISSING:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    elif isinstance(value, (list, dict)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    if not text:
        return None
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


class ApiFetcher(Fetcher):
    kind = "api"

    def fetch(self, source: SourceConfig, client: httpx.Client) -> list[Item]:
        assert isinstance(source, ApiSource)
        kwargs: dict[str, Any] = {}
        if source.params:
            kwargs["params"] = source.params
        if source.json_body is not None:
            kwargs["json"] = source.json_body
        response = http_request(source, client, method=source.method, **kwargs)

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            preview = response.text[:200].replace("\n", " ")
            raise SourceError(
                f"{source.url} did not return JSON: {exc}. First bytes: {preview!r}", kind="parse"
            ) from exc

        raw_items = dig(payload, source.items_path)
        if raw_items is MISSING:
            available = sorted(payload)[:12] if isinstance(payload, dict) else type(payload).__name__
            raise SourceError(
                f"items_path={source.items_path!r} not found in the response from {source.url}. "
                f"Top level holds: {available}",
                kind="mapping",
            )
        if isinstance(raw_items, dict):
            # A dict of id -> object is a common shape; take the values.
            raw_items = list(raw_items.values())
        if not isinstance(raw_items, list):
            raise SourceError(
                f"items_path={source.items_path!r} points at {type(raw_items).__name__}, "
                "expected a list of items",
                kind="mapping",
            )

        title_path = source.fields["title"]
        items: list[Item] = []
        missing_titles = 0

        for element in raw_items:
            title = _as_text(dig(element, title_path), limit=300)
            if not title:
                missing_titles += 1
                continue
            published = dig(element, source.fields["published_at"]) if "published_at" in source.fields else None
            items.append(
                Item(
                    source=source.name,
                    title=title,
                    url=_as_text(dig(element, source.fields["url"]), limit=2000)
                    if "url" in source.fields
                    else None,
                    summary=_as_text(dig(element, source.fields["summary"]))
                    if "summary" in source.fields
                    else None,
                    author=_as_text(dig(element, source.fields["author"]), limit=120)
                    if "author" in source.fields
                    else None,
                    published_at=self._parse_time(published),
                    external_id=_as_text(dig(element, source.fields["id"]), limit=200)
                    if "id" in source.fields
                    else None,
                    raw=element if isinstance(element, dict) else {"value": element},
                )
            )

        if raw_items and not items:
            raise SourceError(
                f"none of the {len(raw_items)} elements from {source.url} had a value at "
                f"fields.title={title_path!r}; the API shape probably changed",
                kind="mapping",
            )
        if missing_titles:
            # Partial mapping failure: surfaced as a warning by the collector.
            items[0].raw.setdefault("_digestbot_skipped_without_title", missing_titles)
        return items

    @staticmethod
    def _parse_time(value: Any) -> Any:
        if value is None or value is MISSING:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            from datetime import datetime, timezone

            # Heuristic: anything past the year 5138 in seconds is milliseconds.
            seconds = value / 1000 if value > 100_000_000_000 else value
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
        if isinstance(value, str):
            return parse_iso(value)
        return None
