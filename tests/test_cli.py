from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from digestbot import cli
from digestbot.store import Store
from tests.factories import API_FIXTURE, HTML_FIXTURE, RSS_FIXTURE

FEED_URL = "https://rss.example.com/feed.xml"
API_URL = "https://api.example.com/releases"
PAGE_URL = "https://scrape.example.com/blog"

CONFIG_TOML = """
[bot]
timezone = "UTC"
send_after = "00:00"
title = "Test Digest"
lookback_hours = 100000

[paths]
database = "{database}"
log_file = "{log_file}"
log_level = "DEBUG"

[delivery]
kind = "{delivery_kind}"
channel = "#test"
path = "{out}"
{webhook}

[[sources]]
name = "feed"
kind = "rss"
url = "https://rss.example.com/feed.xml"

[[sources]]
name = "api"
kind = "api"
url = "https://api.example.com/releases"
items_path = "results"
fields = {{ id = "id", title = "name", url = "html_url", published_at = "published_at" }}

[[sources]]
name = "page"
kind = "scrape"
url = "https://scrape.example.com/blog"
selectors = {{ item = "div.post", title = "h3.post-title a", url = "h3.post-title a@href" }}
"""


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


@pytest.fixture
def workspace(tmp_path: Path):
    def write(delivery_kind: str = "file", webhook: str = "") -> Path:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            CONFIG_TOML.format(
                database=_posix(tmp_path / "digest.db"),
                log_file=_posix(tmp_path / "digest-bot.log"),
                out=_posix(tmp_path / "out"),
                delivery_kind=delivery_kind,
                webhook=webhook,
            ),
            encoding="utf-8",
        )
        return config_path

    return write, tmp_path


def run_cli(config_path: Path, *args: str) -> int:
    return cli.main(["--config", str(config_path), *args])


def mock_sources_ok() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))


# ----------------------------------------------------------------------
# config and init
# ----------------------------------------------------------------------


def test_missing_config_exits_with_the_config_code(tmp_path: Path, capsys) -> None:
    assert run_cli(tmp_path / "nope.toml", "status") == cli.EXIT_CONFIG
    assert "config file not found" in capsys.readouterr().err


def test_invalid_config_exits_with_the_config_code(tmp_path: Path, capsys) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text('[delivery]\nkind = "slack"\n', encoding="utf-8")
    assert run_cli(broken, "status") == cli.EXIT_CONFIG
    assert "requires delivery.webhook_url" in capsys.readouterr().err


def test_init_creates_the_database(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    assert run_cli(config_path, "init") == cli.EXIT_OK
    assert (tmp_path / "digest.db").exists()
    assert "config is valid" in capsys.readouterr().out


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert "daily-digest-bot" in capsys.readouterr().out


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


@respx.mock
def test_run_delivers_and_exits_zero(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    mock_sources_ok()

    assert run_cli(config_path, "run", "--trigger", "cron") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "delivered 6 item(s)" in out
    assert len(list((tmp_path / "out").glob("*.md"))) == 1


@respx.mock
def test_run_json_output_is_parseable(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()

    assert run_cli(config_path, "--json", "run") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is True
    assert payload["items_new"] == 6
    assert payload["failed_sources"] == []


@respx.mock
def test_run_dry_run_prints_the_digest_and_sends_nothing(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    mock_sources_ok()

    assert run_cli(config_path, "run", "--dry-run") == cli.EXIT_OK
    assert "First post" in capsys.readouterr().out
    assert not list((tmp_path / "out").glob("*.md"))


@respx.mock
def test_run_exits_two_when_delivery_fails(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write(
        delivery_kind="slack", webhook='webhook_url = "https://hooks.example.com/bad"'
    )
    mock_sources_ok()
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(500, text="down"))

    assert run_cli(config_path, "run") == cli.EXIT_DELIVERY_FAILED
    assert "DELIVERY FAILED" in capsys.readouterr().out
    # The failure is in the bot's own log, not just on the console.
    assert "DIGEST NOT DELIVERED" in (tmp_path / "digest-bot.log").read_text(encoding="utf-8")


@respx.mock
def test_source_failure_alone_exits_zero(workspace) -> None:
    """A broken source is reported but must not fail the cron job by default."""
    write, _ = workspace
    config_path = write()
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    assert run_cli(config_path, "run") == cli.EXIT_OK


@respx.mock
def test_strict_turns_a_source_failure_into_exit_three(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    assert run_cli(config_path, "run", "--strict") == cli.EXIT_SOURCE_FAILED
    assert "failed sources: feed" in capsys.readouterr().out


@respx.mock
def test_run_twice_posts_once(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    mock_sources_ok()

    assert run_cli(config_path, "run") == cli.EXIT_OK
    capsys.readouterr()
    assert run_cli(config_path, "run") == cli.EXIT_OK
    assert "already sent" in capsys.readouterr().out
    assert len(list((tmp_path / "out").glob("*.md"))) == 1


# ----------------------------------------------------------------------
# collect
# ----------------------------------------------------------------------


@respx.mock
def test_collect_records_without_sending(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    mock_sources_ok()

    assert run_cli(config_path, "collect") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "collected 6 new item(s)" in out
    assert "6 item(s) waiting for the next digest" in out
    assert not list((tmp_path / "out").glob("*.md"))


@respx.mock
def test_collect_strict_exits_three(workspace) -> None:
    write, _ = workspace
    config_path = write()
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("dns"))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))

    assert run_cli(config_path, "collect", "--strict") == cli.EXIT_SOURCE_FAILED


# ----------------------------------------------------------------------
# healthcheck: the command an uptime monitor watches
# ----------------------------------------------------------------------


def test_healthcheck_on_a_fresh_install_is_not_down(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    assert run_cli(config_path, "healthcheck") == cli.EXIT_OK
    assert "unknown" in capsys.readouterr().out


@respx.mock
def test_healthcheck_is_ok_after_a_digest(workspace) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "run")
    assert run_cli(config_path, "healthcheck") == cli.EXIT_OK


@respx.mock
def test_healthcheck_exits_four_when_a_digest_is_stuck(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write(
        delivery_kind="slack", webhook='webhook_url = "https://hooks.example.com/bad"'
    )
    mock_sources_ok()
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(500))
    run_cli(config_path, "run")
    capsys.readouterr()

    assert run_cli(config_path, "healthcheck") == cli.EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "[DOWN]" in out
    assert "failed to deliver" in out


@respx.mock
def test_healthcheck_detects_silence(workspace) -> None:
    write, tmp_path = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "run")

    # Backdate the delivery well past max_silence_hours.
    store = Store(tmp_path / "digest.db")
    store.conn.execute("UPDATE digests SET sent_at = '2020-01-01T00:00:00+00:00'")
    store.close()

    assert run_cli(config_path, "healthcheck") == cli.EXIT_UNHEALTHY


@respx.mock
def test_healthcheck_json(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    assert run_cli(config_path, "--json", "healthcheck") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "checks" in payload and "sources" in payload


# ----------------------------------------------------------------------
# reporting commands
# ----------------------------------------------------------------------


@respx.mock
def test_status_summarises_everything(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "run")
    capsys.readouterr()

    assert run_cli(config_path, "status") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "daily-digest-bot" in out
    assert "file:#test" in out
    assert "6 total" in out
    assert "sources:" in out


@respx.mock
def test_sources_command(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    respx.get(FEED_URL).mock(return_value=httpx.Response(404, text="gone"))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))
    run_cli(config_path, "collect")
    capsys.readouterr()

    assert run_cli(config_path, "sources") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "feed" in out
    assert "HTTP 404" in out
    assert "1 failing in a row" in out


@respx.mock
def test_digests_list_and_show(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "run")
    capsys.readouterr()

    assert run_cli(config_path, "digests") == cli.EXIT_OK
    assert "sent" in capsys.readouterr().out

    assert run_cli(config_path, "digests", "--show", "1") == cli.EXIT_OK
    assert "First post" in capsys.readouterr().out


def test_digests_show_unknown_id(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    assert run_cli(config_path, "digests", "--show", "42") == cli.EXIT_CRASH
    assert "no digest with id 42" in capsys.readouterr().err


@respx.mock
def test_log_command_shows_errors(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=API_FIXTURE))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=HTML_FIXTURE))
    run_cli(config_path, "collect")
    capsys.readouterr()

    assert run_cli(config_path, "log", "--level", "ERROR") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "feed" in out


@respx.mock
def test_preview_renders_pending_items(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "collect")
    capsys.readouterr()

    assert run_cli(config_path, "preview") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Test Digest" in out
    assert "First post" in out


@respx.mock
def test_prune_keeps_dedupe_memory(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write()
    mock_sources_ok()
    run_cli(config_path, "run")
    capsys.readouterr()

    assert run_cli(config_path, "prune", "--days", "0") == cli.EXIT_OK
    assert "Fingerprints were kept" in capsys.readouterr().out


def test_test_channel_reports_success(workspace, capsys) -> None:
    write, tmp_path = workspace
    config_path = write()
    assert run_cli(config_path, "test-channel") == cli.EXIT_OK
    assert "posted a test message" in capsys.readouterr().out
    assert len(list((tmp_path / "out").glob("*.md"))) == 1


@respx.mock
def test_test_channel_reports_failure_with_exit_two(workspace, capsys) -> None:
    write, _ = workspace
    config_path = write(
        delivery_kind="slack", webhook='webhook_url = "https://hooks.example.com/bad"'
    )
    respx.post("https://hooks.example.com/bad").mock(return_value=httpx.Response(401, text="invalid_token"))

    assert run_cli(config_path, "test-channel") == cli.EXIT_DELIVERY_FAILED
    assert "invalid_token" in capsys.readouterr().out


# ----------------------------------------------------------------------
# crash handling
# ----------------------------------------------------------------------


def test_unexpected_crash_exits_one_and_is_logged(workspace, capsys, monkeypatch) -> None:
    write, tmp_path = workspace
    config_path = write()
    monkeypatch.setitem(
        cli.COMMANDS, "status", lambda *a: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )

    assert run_cli(config_path, "status") == cli.EXIT_CRASH
    assert "kaboom" in capsys.readouterr().err
    log_text = (tmp_path / "digest-bot.log").read_text(encoding="utf-8")
    assert "kaboom" in log_text
    assert "Traceback" in log_text


def test_keyboard_interrupt_exits_one(workspace, monkeypatch) -> None:
    write, _ = workspace
    config_path = write()
    monkeypatch.setitem(
        cli.COMMANDS, "status", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert run_cli(config_path, "status") == cli.EXIT_CRASH
