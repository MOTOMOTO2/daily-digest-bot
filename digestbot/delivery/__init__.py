"""Delivery channels.

Every channel takes a rendered :class:`~digestbot.render.RenderedDigest` and
either returns normally or raises :class:`DeliveryError`. There is no
"best effort" path: a channel that cannot deliver must say so, because a
digest that vanishes is the failure mode this bot is built to avoid.
"""

from __future__ import annotations

from ..config import AlertConfig, DeliveryConfig
from .base import Channel, DeliveryError
from .discord import DiscordChannel
from .file import FileChannel, StdoutChannel
from .slack import SlackChannel
from .webhook import GenericWebhookChannel

CHANNELS: dict[str, type[Channel]] = {
    "slack": SlackChannel,
    "discord": DiscordChannel,
    "webhook": GenericWebhookChannel,
    "file": FileChannel,
    "stdout": StdoutChannel,
}


def build_channel(config: DeliveryConfig | AlertConfig) -> Channel:
    try:
        channel_class = CHANNELS[config.kind]
    except KeyError:
        raise DeliveryError(
            f"unknown delivery kind {config.kind!r}; available: {sorted(CHANNELS)}"
        ) from None
    return channel_class(config)


__all__ = [
    "CHANNELS",
    "Channel",
    "DeliveryError",
    "DiscordChannel",
    "FileChannel",
    "GenericWebhookChannel",
    "SlackChannel",
    "StdoutChannel",
    "build_channel",
]
