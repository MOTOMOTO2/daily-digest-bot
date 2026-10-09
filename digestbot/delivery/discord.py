"""Discord webhooks."""

from __future__ import annotations

from typing import Any

import httpx

from .base import Channel, post_json

# Discord rejects messages over 2000 characters.
MESSAGE_LIMIT = 1900


class DiscordChannel(Channel):
    kind = "discord"

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        assert self.config.webhook_url  # guaranteed by config validation
        body = f"**{subject}**\n\n{markdown}"
        parts = _split(body, MESSAGE_LIMIT)

        receipts: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {"content": part, "allowed_mentions": {"parse": []}}
            if self.config.username:
                payload["username"] = self.config.username
            # If a later part fails the error propagates: a half-delivered
            # digest is reported as a failure, not quietly accepted.
            receipts.append(
                post_json(
                    self.config.webhook_url,
                    payload,
                    timeout=self.config.timeout_seconds,
                    client=client,
                )
            )
            if index >= 4:  # keep a runaway digest from flooding the channel
                break
        return {"messages": len(receipts), "receipts": receipts}


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts
