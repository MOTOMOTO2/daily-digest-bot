"""HTML pages, via CSS selectors.

``selectors.item`` picks each row; the remaining selectors run inside a row.
Append ``@attr`` to read an attribute instead of the element text, so
``a@href`` gives you the link. Relative URLs are resolved against the page.
"""

from __future__ import annotations

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..config import ScrapeSource, SourceConfig
from ..models import Item, parse_iso
from .base import Fetcher, SourceError, http_get


def split_selector(selector: str) -> tuple[str, str | None]:
    """Split "a@href" into ("a", "href")."""
    if "@" in selector:
        css, _, attribute = selector.rpartition("@")
        css = css.strip()
        attribute = attribute.strip()
        if attribute and css:
            return css, attribute
        if attribute and not css:
            # "@href" means an attribute of the row element itself.
            return "", attribute
    return selector.strip(), None


def extract(row: Tag, selector: str, *, limit: int = 600) -> str | None:
    css, attribute = split_selector(selector)
    target: Tag | None
    if css == "":
        target = row
    else:
        found = row.select_one(css)
        target = found if isinstance(found, Tag) else None
    if target is None:
        return None
    if attribute:
        value = target.get(attribute)
        if isinstance(value, list):
            value = " ".join(value)
        text = (value or "").strip() if isinstance(value, str) else ""
    else:
        text = " ".join(target.get_text(" ", strip=True).split())
    if not text:
        return None
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


class ScrapeFetcher(Fetcher):
    kind = "scrape"

    def fetch(self, source: SourceConfig, client: httpx.Client) -> list[Item]:
        assert isinstance(source, ScrapeSource)
        response = http_get(source, client)

        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:  # pragma: no cover - html.parser is forgiving
            raise SourceError(f"could not parse HTML from {source.url}: {exc}", kind="parse") from exc

        item_selector = source.selectors["item"]
        rows = [row for row in soup.select(item_selector) if isinstance(row, Tag)]
        if not rows:
            # A scraper that matches nothing is the classic silent failure:
            # the page was redesigned and the bot just goes quiet. Shout.
            raise SourceError(
                f"selector {item_selector!r} matched no elements on {source.url}; "
                "the page markup has probably changed",
                kind="selector",
            )

        base_url = str(response.url)
        title_selector = source.selectors["title"]
        items: list[Item] = []
        for row in rows:
            title = extract(row, title_selector, limit=300)
            if not title:
                continue
            url = extract(row, source.selectors["url"], limit=2000) if "url" in source.selectors else None
            if url:
                url = urljoin(base_url, url)
            published_raw = (
                extract(row, source.selectors["published_at"], limit=120)
                if "published_at" in source.selectors
                else None
            )
            items.append(
                Item(
                    source=source.name,
                    title=title,
                    url=url,
                    summary=extract(row, source.selectors["summary"]) if "summary" in source.selectors else None,
                    author=extract(row, source.selectors["author"], limit=120)
                    if "author" in source.selectors
                    else None,
                    published_at=parse_iso(published_raw) if published_raw else None,
                    raw={"scraped_from": base_url, "published_text": published_raw},
                )
            )

        if not items:
            raise SourceError(
                f"selector {item_selector!r} matched {len(rows)} element(s) on {source.url} "
                f"but title selector {title_selector!r} found no text in any of them",
                kind="selector",
            )
        return items
