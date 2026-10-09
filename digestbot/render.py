"""Turning collected items into the message that gets posted.

Two deliberate choices here:

* The digest is grouped by source, newest first inside each group.
* Collection problems are rendered into the digest body itself. A failing
  source therefore shows up in the channel, not only in the log file -- the
  bot complains where people are actually looking.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import SourceOutcome, StoredItem

SLACK_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


@dataclass(slots=True)
class RenderedDigest:
    subject: str
    body: str
    item_count: int
    problem_count: int
    sources_included: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.item_count == 0


def escape_slack(text: str) -> str:
    out = text
    for char, replacement in SLACK_ESCAPES.items():
        out = out.replace(char, replacement)
    return out


def _link(title: str, url: str | None, *, flavour: str) -> str:
    if not url:
        return f"*{escape_slack(title)}*" if flavour == "slack" else f"**{title}**"
    if flavour == "slack":
        # Slack mrkdwn link syntax; titles must be escaped, URLs must not.
        return f"<{url}|{escape_slack(title)}>"
    return f"[{title}]({url})"


def _local_time(moment: datetime | None, tz: ZoneInfo) -> str:
    if moment is None:
        return ""
    return moment.astimezone(tz).strftime("%H:%M")


def group_by_source(items: list[StoredItem]) -> "OrderedDict[str, list[StoredItem]]":
    grouped: OrderedDict[str, list[StoredItem]] = OrderedDict()
    for item in items:
        grouped.setdefault(item.source, []).append(item)
    return grouped


def render_digest(
    items: list[StoredItem],
    outcomes: list[SourceOutcome],
    *,
    title: str,
    digest_date: str,
    timezone_name: str,
    flavour: str = "markdown",
    labels: dict[str, str] | None = None,
    pending_overflow: int = 0,
) -> RenderedDigest:
    """Render the digest body. ``flavour`` is "slack" or "markdown"."""
    tz = ZoneInfo(timezone_name)
    labels = labels or {}
    failures = [outcome for outcome in outcomes if not outcome.ok]
    empties = [outcome for outcome in outcomes if outcome.ok and outcome.items_fetched == 0]

    subject = f"{title} - {digest_date}"
    lines: list[str] = []

    if items:
        plural = "item" if len(items) == 1 else "items"
        source_count = len({item.source for item in items})
        sources_word = "source" if source_count == 1 else "sources"
        lines.append(f"{len(items)} new {plural} from {source_count} {sources_word}.")
        lines.append("")
        for source, group in group_by_source(items).items():
            heading = labels.get(source, source)
            lines.append(f"*{escape_slack(heading)}*" if flavour == "slack" else f"### {heading}")
            for item in group:
                stamp = _local_time(item.published_at, tz)
                bullet = f"- {_link(item.title, item.url, flavour=flavour)}"
                if stamp:
                    bullet += f"  _{stamp}_" if flavour != "slack" else f"  ({stamp})"
                lines.append(bullet)
                if item.summary:
                    summary = item.summary[:280].rstrip()
                    if len(item.summary) > 280:
                        summary += "..."
                    indent = "  " if flavour == "slack" else "  "
                    lines.append(f"{indent}{escape_slack(summary) if flavour == 'slack' else summary}")
            lines.append("")
    else:
        lines.append("No new items since the last digest.")
        lines.append("")
        lines.append(
            "This message is the bot's proof of life: if a day goes by with no digest at all, "
            "something is broken."
        )
        lines.append("")

    if pending_overflow > 0:
        lines.append(
            f"_{pending_overflow} further item(s) held back by the per-digest cap; "
            "they will appear in the next digest._"
        )
        lines.append("")

    if failures:
        lines.append("*Collection problems*" if flavour == "slack" else "### Collection problems")
        for outcome in failures:
            name = labels.get(outcome.name, outcome.name)
            reason = (outcome.error or "unknown error").replace("\n", " ")[:300]
            marker = "!" if flavour == "slack" else "**!**"
            lines.append(
                f"- {marker} `{escape_slack(name) if flavour == 'slack' else name}` "
                f"({outcome.error_type or 'error'}): "
                f"{escape_slack(reason) if flavour == 'slack' else reason}"
            )
        lines.append("")

    if empties:
        names = ", ".join(f"`{labels.get(o.name, o.name)}`" for o in empties)
        lines.append(f"_Reachable but returned nothing: {names}._")
        lines.append("")

    footprint = [outcome.name for outcome in outcomes if outcome.ok]
    checked = len(outcomes)
    ok_count = len(footprint)
    lines.append(
        f"_{ok_count}/{checked} sources healthy - generated "
        f"{datetime.now(tz).strftime('%Y-%m-%d %H:%M %Z')}_"
    )

    body = "\n".join(lines).strip()
    return RenderedDigest(
        subject=subject,
        body=body,
        item_count=len(items),
        problem_count=len(failures),
        sources_included=sorted({item.source for item in items}),
    )
