"""Slack incoming webhooks."""

from __future__ import annotations

from typing import Any

import httpx

from .base import Channel, post_json

# Slack rejects blocks over 3000 characters of text, so long digests are split
# across several section blocks rather than being truncated.
SECTION_LIMIT = 2900


def chunk_markdown(markdown: str, limit: int = SECTION_LIMIT) -> list[str]:
    """Split text into Slack-sized pieces, preferring paragraph boundaries."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in markdown.split("\n\n"):
        piece = paragraph.strip("\n")
        if not piece:
            continue
        if len(piece) > limit:
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            for start in range(0, len(piece), limit):
                chunks.append(piece[start : start + limit])
            continue
        if size + len(piece) + 2 > limit and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [markdown[:limit] or "(empty digest)"]


class SlackChannel(Channel):
    kind = "slack"

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": subject[:150], "emoji": True}}
        ]
        for chunk in chunk_markdown(markdown):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
            # Slack caps a message at 50 blocks.
            if len(blocks) >= 48:
                break

        payload: dict[str, Any] = {
            # `text` is the notification fallback and the plain-text archive.
            "text": subject[:3000],
            "blocks": blocks,
        }
        if self.config.username:
            payload["username"] = self.config.username

        assert self.config.webhook_url  # guaranteed by config validation
        return post_json(
            self.config.webhook_url,
            payload,
            timeout=self.config.timeout_seconds,
            client=client,
        )
