"""Core data types and the fingerprinting rules that drive deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that identify a campaign, not a document. Stripping them
# keeps the same article from arriving twice via different referrers.
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
    "spm",
    "igshid",
    "_hsenc",
    "_hsmi",
    "cmpid",
}

WHITESPACE = re.compile(r"\s+")
NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Serialise a datetime as a UTC ISO-8601 string."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_url(url: str) -> str:
    """Normalise a URL so cosmetic differences do not defeat deduplication."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # Keep a non-default port; drop the implied ones.
    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query = urlencode(sorted(kept))
    # The fragment is never part of a document's identity here.
    return urlunsplit((scheme, host, path, query, ""))


def normalize_title(title: str) -> str:
    """Fold a title down to a comparable key."""
    folded = unicodedata.normalize("NFKD", title or "").casefold()
    folded = NON_WORD.sub(" ", folded)
    return WHITESPACE.sub(" ", folded).strip()


def _digest(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Item:
    """One collected thing, before and after it lands in the store."""

    source: str
    title: str
    url: str | None = None
    summary: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    # Identifier supplied by the source itself (RSS guid, API id). Most
    # trustworthy dedupe signal when present.
    external_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = WHITESPACE.sub(" ", (self.title or "")).strip()
        if self.summary:
            self.summary = WHITESPACE.sub(" ", self.summary).strip()
        if self.url:
            self.url = self.url.strip() or None
        if self.author:
            self.author = WHITESPACE.sub(" ", self.author).strip() or None

    @property
    def canonical(self) -> str:
        return canonical_url(self.url) if self.url else ""

    @property
    def title_key(self) -> str:
        return _digest("title", self.normalized_title) if self.normalized_title else ""

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    def fingerprint(self, dedupe_by: list[str] | tuple[str, ...]) -> str:
        """Primary identity used for the UNIQUE constraint in the store.

        A source-provided id wins; then the canonical URL if URL deduplication
        is on; then the normalized title. The last fallback is scoped to the
        source so two sources with identical titles are not collapsed when
        title deduplication is switched off.
        """
        if self.external_id:
            return _digest("external", self.source, self.external_id)
        if "url" in dedupe_by and self.canonical:
            return _digest("url", self.canonical)
        if "title" in dedupe_by and self.normalized_title:
            return _digest("title", self.normalized_title)
        if self.canonical:
            return _digest("url", self.canonical)
        return _digest("source-title", self.source, self.normalized_title)

    def age_hours(self, *, now: datetime | None = None) -> float | None:
        if self.published_at is None:
            return None
        reference = now or utcnow()
        return (reference - self.published_at).total_seconds() / 3600.0


@dataclass(slots=True)
class StoredItem:
    """An item as it exists in SQLite."""

    id: int
    fingerprint: str
    source: str
    title: str
    url: str | None
    summary: str | None
    author: str | None
    published_at: datetime | None
    first_seen_at: datetime | None
    digest_id: int | None
    sent_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> StoredItem:
        return cls(
            id=row["id"],
            fingerprint=row["fingerprint"],
            source=row["source"],
            title=row["title"],
            url=row["url"],
            summary=row["summary"],
            author=row["author"],
            published_at=parse_iso(row["published_at"]),
            first_seen_at=parse_iso(row["first_seen_at"]),
            digest_id=row["digest_id"],
            sent_at=parse_iso(row["sent_at"]),
        )


@dataclass(slots=True)
class SourceOutcome:
    """What happened when one source was polled."""

    name: str
    kind: str
    status: str  # "ok" | "error"
    items_fetched: int = 0
    items_new: int = 0
    items_duplicate: int = 0
    items_stale: int = 0
    items_filtered: int = 0
    duration_ms: int = 0
    error: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"
