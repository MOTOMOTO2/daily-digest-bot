"""Source adapter registry.

Adding a source type means writing a Fetcher and registering it here; config
validation in :mod:`digestbot.config` picks it up through the ``kind`` field.
"""

from __future__ import annotations

from .api import ApiFetcher
from .base import Fetcher, SourceError, apply_filters
from .rss import RssFetcher
from .scrape import ScrapeFetcher

FETCHERS: dict[str, Fetcher] = {
    "rss": RssFetcher(),
    "api": ApiFetcher(),
    "scrape": ScrapeFetcher(),
}


def get_fetcher(kind: str) -> Fetcher:
    try:
        return FETCHERS[kind]
    except KeyError:
        raise SourceError(
            f"unknown source kind {kind!r}; available: {sorted(FETCHERS)}", kind="config"
        ) from None


__all__ = [
    "FETCHERS",
    "ApiFetcher",
    "Fetcher",
    "RssFetcher",
    "ScrapeFetcher",
    "SourceError",
    "apply_filters",
    "get_fetcher",
]
