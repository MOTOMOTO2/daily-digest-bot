"""Shared plumbing for source adapters."""

from __future__ import annotations

import httpx

from ..config import SourceConfig
from ..models import Item


class SourceError(Exception):
    """A source could not be collected.

    Carries a short machine-readable ``kind`` so the dashboard can tell a
    network blip apart from a selector that no longer matches anything.
    """

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class Fetcher:
    """Collects items for one kind of source.

    Subclasses implement :meth:`fetch` and raise :class:`SourceError` with a
    message a human can act on. Returning an empty list is allowed but is
    reported as a warning upstream, because a source that silently stops
    producing anything is exactly the failure this project cares about.
    """

    kind: str = "base"

    def fetch(self, source: SourceConfig, client: httpx.Client) -> list[Item]:
        raise NotImplementedError


def http_get(source: SourceConfig, client: httpx.Client, **kwargs: object) -> httpx.Response:
    """GET a URL, converting every failure mode into a SourceError."""
    return http_request(source, client, method="GET", **kwargs)


def http_request(
    source: SourceConfig,
    client: httpx.Client,
    *,
    method: str = "GET",
    **kwargs: object,
) -> httpx.Response:
    try:
        response = client.request(
            method,
            source.url,
            headers=source.headers or None,
            timeout=source.timeout_seconds,
            **kwargs,  # type: ignore[arg-type]
        )
    except httpx.TimeoutException as exc:
        raise SourceError(
            f"timed out after {source.timeout_seconds:g}s requesting {source.url}", kind="timeout"
        ) from exc
    except httpx.TransportError as exc:
        raise SourceError(f"network error requesting {source.url}: {exc}", kind="network") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"HTTP error requesting {source.url}: {exc}", kind="http") from exc

    if response.status_code >= 400:
        body = response.text[:300].replace("\n", " ").strip()
        raise SourceError(
            f"{source.url} returned HTTP {response.status_code}" + (f": {body}" if body else ""),
            kind=f"http_{response.status_code}",
        )
    return response


def apply_filters(source: SourceConfig, items: list[Item]) -> tuple[list[Item], int]:
    """Apply the source's include/exclude substring filters.

    Returns the kept items and how many were dropped.
    """
    include = [term.casefold() for term in source.include if term.strip()]
    exclude = [term.casefold() for term in source.exclude if term.strip()]
    if not include and not exclude:
        return items, 0

    kept: list[Item] = []
    for item in items:
        haystack = f"{item.title} {item.summary or ''}".casefold()
        if include and not any(term in haystack for term in include):
            continue
        if exclude and any(term in haystack for term in exclude):
            continue
        kept.append(item)
    return kept, len(items) - len(kept)
