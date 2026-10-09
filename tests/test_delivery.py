from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from digestbot.config import AlertConfig, DeliveryConfig
from digestbot.delivery import DeliveryError, build_channel


def channel_for(**kwargs):
    return build_channel(DeliveryConfig(**kwargs))


# ----------------------------------------------------------------------
# local channels
# ----------------------------------------------------------------------


def test_file_channel_writes_markdown(tmp_path: Path) -> None:
    channel = channel_for(kind="file", channel="#local", path=str(tmp_path / "out"))
    receipt = channel.send("Daily Digest - 2025-10-08", "- an item")

    written = Path(receipt["path"])
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("# Daily Digest - 2025-10-08")
    assert written.name.endswith("daily-digest-2025-10-08.md")
    assert channel.describe() == "file:#local"


def test_file_channel_never_overwrites(tmp_path: Path) -> None:
    channel = channel_for(kind="file", path=str(tmp_path / "out"))
    first = channel.send("Same Subject", "first body")
    second = channel.send("Same Subject", "second body")

    assert first["path"] != second["path"]
    assert Path(first["path"]).read_text(encoding="utf-8").endswith("first body\n")
    assert Path(second["path"]).read_text(encoding="utf-8").endswith("second body\n")


def test_file_channel_reports_an_unwritable_path(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    channel = channel_for(kind="file", path=str(blocker / "nested"))
    with pytest.raises(DeliveryError, match="could not write digest") as caught:
        channel.send("Subject", "body")
    assert caught.value.retryable is False


def test_stdout_channel(capsys) -> None:
    channel_for(kind="stdout").send("Subject", "body text")
    assert "body text" in capsys.readouterr().out


# ----------------------------------------------------------------------
# Slack
# ----------------------------------------------------------------------


@respx.mock
def test_slack_posts_blocks() -> None:
    route = respx.post("https://hooks.example.com/ok").mock(return_value=httpx.Response(200, text="ok"))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/ok", channel="#digest")
    receipt = channel.send("Daily Digest", "*feed*\n- <https://example.com/1|A post>")

    assert receipt["status_code"] == 200
    payload = respx.calls.last.request.read().decode()
    assert '"type": "header"' in payload or '"type":"header"' in payload
    assert "A post" in payload
    assert route.called


@respx.mock
def test_slack_long_digest_is_split_into_blocks() -> None:
    respx.post("https://hooks.example.com/ok").mock(return_value=httpx.Response(200))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/ok")
    body = "\n\n".join(f"- item {index} " + "x" * 500 for index in range(20))
    channel.send("Subject", body)

    import json

    payload = json.loads(respx.calls.last.request.read())
    assert len(payload["blocks"]) > 2
    assert all(
        len(block["text"]["text"]) <= 3000 for block in payload["blocks"] if block["type"] == "section"
    )


@respx.mock
def test_slack_5xx_is_retryable() -> None:
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(503, text="down"))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/bad")
    with pytest.raises(DeliveryError) as caught:
        channel.send("Subject", "body")
    assert caught.value.retryable is True
    assert caught.value.status_code == 503


@respx.mock
def test_slack_4xx_is_not_retryable() -> None:
    respx.post("https://hooks.example.com/gone").mock(return_value=httpx.Response(404, text="no_service"))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/gone")
    with pytest.raises(DeliveryError, match="no_service") as caught:
        channel.send("Subject", "body")
    assert caught.value.retryable is False


@respx.mock
def test_slack_429_is_retryable() -> None:
    respx.post("https://hooks.example.com/slow").mock(return_value=httpx.Response(429))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/slow")
    with pytest.raises(DeliveryError) as caught:
        channel.send("Subject", "body")
    assert caught.value.retryable is True


@respx.mock
def test_network_failure_becomes_delivery_error() -> None:
    respx.post("https://hooks.example.com/ok").mock(side_effect=httpx.ConnectError("dns"))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/ok")
    with pytest.raises(DeliveryError, match="network error"):
        channel.send("Subject", "body")


@respx.mock
def test_timeout_becomes_delivery_error() -> None:
    respx.post("https://hooks.example.com/ok").mock(side_effect=httpx.ReadTimeout("slow"))
    channel = channel_for(kind="slack", webhook_url="https://hooks.example.com/ok")
    with pytest.raises(DeliveryError, match="timed out"):
        channel.send("Subject", "body")


# ----------------------------------------------------------------------
# Discord and generic webhooks
# ----------------------------------------------------------------------


@respx.mock
def test_discord_posts_content() -> None:
    respx.post("https://discord.example.com/hook").mock(return_value=httpx.Response(204))
    channel = channel_for(kind="discord", webhook_url="https://discord.example.com/hook")
    receipt = channel.send("Daily Digest", "- a post")

    assert receipt["messages"] == 1
    import json

    payload = json.loads(respx.calls.last.request.read())
    assert payload["content"].startswith("**Daily Digest**")
    # Never let a digest ping @everyone.
    assert payload["allowed_mentions"] == {"parse": []}


@respx.mock
def test_discord_splits_long_digests() -> None:
    respx.post("https://discord.example.com/hook").mock(return_value=httpx.Response(204))
    channel = channel_for(kind="discord", webhook_url="https://discord.example.com/hook")
    body = "\n".join(f"- item {index} " + "y" * 200 for index in range(30))
    receipt = channel.send("Subject", body)
    assert receipt["messages"] > 1
    assert len(respx.calls) == receipt["messages"]


@respx.mock
def test_discord_partial_failure_raises() -> None:
    responses = [httpx.Response(204), httpx.Response(500, text="boom")]
    respx.post("https://discord.example.com/hook").mock(side_effect=responses)
    channel = channel_for(kind="discord", webhook_url="https://discord.example.com/hook")
    body = "\n".join(f"- item {index} " + "y" * 200 for index in range(30))
    with pytest.raises(DeliveryError, match="500"):
        channel.send("Subject", body)


@respx.mock
def test_generic_webhook_sends_structured_json() -> None:
    respx.post("https://hooks.example.com/generic").mock(return_value=httpx.Response(200))
    channel = channel_for(
        kind="webhook", webhook_url="https://hooks.example.com/generic", channel="ops"
    )
    channel.send("Daily Digest", "- a post")

    import json

    payload = json.loads(respx.calls.last.request.read())
    assert payload["subject"] == "Daily Digest"
    assert payload["markdown"] == "- a post"
    assert payload["channel"] == "ops"
    assert payload["source"] == "daily-digest-bot"


def test_unknown_kind_is_rejected() -> None:
    config = DeliveryConfig(kind="stdout")
    object.__setattr__(config, "kind", "carrier-pigeon")
    with pytest.raises(DeliveryError, match="unknown delivery kind"):
        build_channel(config)


def test_alert_config_builds_a_channel(tmp_path: Path) -> None:
    channel = build_channel(
        AlertConfig(enabled=True, kind="file", path=str(tmp_path / "alerts"), channel="#alerts")
    )
    assert channel.describe() == "file:#alerts"
