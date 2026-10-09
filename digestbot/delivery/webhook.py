"""A plain JSON webhook, for anything that is neither Slack nor Discord."""

from __future__ import annotations

from typing import Any

import httpx

from ..models import iso, utcnow
from .base import Channel, post_json


class GenericWebhookChannel(Channel):
    kind = "webhook"

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        assert self.config.webhook_url  # guaranteed by config validation
        payload = {
            "subject": subject,
            "markdown": markdown,
            "channel": self.config.channel,
            "sent_at": iso(utcnow()),
            "source": "daily-digest-bot",
        }
        return post_json(
            self.config.webhook_url,
            payload,
            timeout=self.config.timeout_seconds,
            client=client,
        )
