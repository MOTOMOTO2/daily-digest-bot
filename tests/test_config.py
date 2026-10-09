from __future__ import annotations

from pathlib import Path

import pytest

from digestbot.config import ConfigError, load_config

MINIMAL = """
[delivery]
kind = "stdout"
channel = "local"

[[sources]]
name = "feed"
kind = "rss"
url = "https://example.com/feed.xml"
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_minimal_config(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL), dotenv=None)
    assert len(config.sources) == 1
    assert config.sources[0].kind == "rss"
    assert config.bot.timezone == "UTC"
    assert config.config_path is not None


def test_missing_file_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml", dotenv=None)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(tmp_path, "this is not toml ["), dotenv=None)


def test_env_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_HOOK", "https://hooks.example.com/abc")
    body = MINIMAL.replace(
        '[delivery]\nkind = "stdout"',
        '[delivery]\nkind = "slack"\nwebhook_url = "${MY_HOOK}"',
    )
    config = load_config(write(tmp_path, body), dotenv=None)
    assert config.delivery.webhook_url == "https://hooks.example.com/abc"


def test_missing_env_var_is_a_hard_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_HOOK", raising=False)
    body = MINIMAL.replace(
        '[delivery]\nkind = "stdout"',
        '[delivery]\nkind = "slack"\nwebhook_url = "${ABSENT_HOOK}"',
    )
    with pytest.raises(ConfigError, match=r"\$\{ABSENT_HOOK\} is not set"):
        load_config(write(tmp_path, body), dotenv=None)


def test_env_default_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_TITLE", raising=False)
    body = MINIMAL + '\n[bot]\ntitle = "${ABSENT_TITLE:-Fallback}"\n'
    assert load_config(write(tmp_path, body), dotenv=None).bot.title == "Fallback"


def test_dotenv_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOTENV_HOOK", raising=False)
    (tmp_path / ".env").write_text('DOTENV_HOOK="https://hooks.example.com/from-dotenv"\n', encoding="utf-8")
    body = MINIMAL.replace(
        '[delivery]\nkind = "stdout"',
        '[delivery]\nkind = "slack"\nwebhook_url = "${DOTENV_HOOK}"',
    )
    config = load_config(write(tmp_path, body))
    assert config.delivery.webhook_url == "https://hooks.example.com/from-dotenv"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="timezon"):
        load_config(write(tmp_path, MINIMAL + '\n[bot]\ntimezon = "UTC"\n'), dotenv=None)


def test_no_sources_is_rejected(tmp_path: Path) -> None:
    body = '[delivery]\nkind = "stdout"\n'
    with pytest.raises(ConfigError, match="no \\[\\[sources\\]\\] configured"):
        load_config(write(tmp_path, body), dotenv=None)


def test_duplicate_source_names_rejected(tmp_path: Path) -> None:
    body = MINIMAL + '\n[[sources]]\nname = "feed"\nkind = "rss"\nurl = "https://other.example/f"\n'
    with pytest.raises(ConfigError, match="duplicate source name"):
        load_config(write(tmp_path, body), dotenv=None)


def test_all_sources_disabled_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace('url = "https://example.com/feed.xml"', 'url = "https://example.com/feed.xml"\nenabled = false')
    with pytest.raises(ConfigError, match="every source is disabled"):
        load_config(write(tmp_path, body), dotenv=None)


def test_slack_without_webhook_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace('kind = "stdout"', 'kind = "slack"')
    with pytest.raises(ConfigError, match="requires delivery.webhook_url"):
        load_config(write(tmp_path, body), dotenv=None)


def test_bad_timezone_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown timezone"):
        load_config(write(tmp_path, MINIMAL + '\n[bot]\ntimezone = "Mars/Olympus"\n'), dotenv=None)


def test_bad_send_after_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="send_after"):
        load_config(write(tmp_path, MINIMAL + '\n[bot]\nsend_after = "9am"\n'), dotenv=None)
    with pytest.raises(ConfigError, match="real time of day"):
        load_config(write(tmp_path, MINIMAL + '\n[bot]\nsend_after = "25:00"\n'), dotenv=None)


def test_non_http_url_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace("https://example.com/feed.xml", "ftp://example.com/feed.xml")
    with pytest.raises(ConfigError, match="must start with http"):
        load_config(write(tmp_path, body), dotenv=None)


def test_scrape_requires_item_and_title(tmp_path: Path) -> None:
    body = """
[delivery]
kind = "stdout"

[[sources]]
name = "page"
kind = "scrape"
url = "https://example.com/blog"
selectors = { title = "h2" }
"""
    with pytest.raises(ConfigError, match="selectors must include 'item'"):
        load_config(write(tmp_path, body), dotenv=None)


def test_api_requires_title_field(tmp_path: Path) -> None:
    body = """
[delivery]
kind = "stdout"

[[sources]]
name = "svc"
kind = "api"
url = "https://example.com/api"
fields = { url = "link" }
"""
    with pytest.raises(ConfigError, match="must map a 'title'"):
        load_config(write(tmp_path, body), dotenv=None)


def test_unknown_source_kind_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace('kind = "rss"', 'kind = "carrier-pigeon"')
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body), dotenv=None)


def test_redacted_masks_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_HOOK", "https://hooks.slack.com/services/T0/B0/supersecrettoken")
    body = MINIMAL.replace(
        '[delivery]\nkind = "stdout"',
        '[delivery]\nkind = "slack"\nwebhook_url = "${MY_HOOK}"',
    )
    redacted = load_config(write(tmp_path, body), dotenv=None).redacted()
    assert "supersecrettoken" not in str(redacted)
    assert "..." in redacted["delivery"]["webhook_url"]


def test_redacted_masks_auth_headers(tmp_path: Path) -> None:
    body = MINIMAL + '\n[sources.headers]\nAuthorization = "Bearer hunter2"\n'
    redacted = load_config(write(tmp_path, body), dotenv=None).redacted()
    assert redacted["sources"][0]["headers"]["Authorization"] == "***"
