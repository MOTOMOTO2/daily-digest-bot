"""Self-contained end-to-end demo. No network, no webhook, no API keys.

Spins up a throwaway HTTP server in a background thread, points the bot at it
as all three source kinds, and walks the whole lifecycle:

    1. a healthy day            -> a digest is delivered
    2. the same day again       -> nothing new, nothing posted twice
    3. a broken page and API    -> the digest still goes out, saying what broke
    4. the webhook goes down    -> the digest fails loudly, nothing is lost
    5. the webhook recovers     -> the same items go out intact

Run it with:  python scripts/demo.py [workspace]

With no argument it uses a temporary directory. Pass a path to keep the
result somewhere you can point `digest-bot serve` at.

The process starts the server, does its work and exits; nothing is left
listening. Everything it writes goes in a temporary directory, printed at the
end so you can poke at the database and the log.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sqlite3
import sys
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from digestbot import cli  # noqa: E402

# --------------------------------------------------------------------------
# fixtures served by the throwaway server
# --------------------------------------------------------------------------

FEED_DAY_1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Demo Feed</title>
    <item>
      <title>Postgres 18 adds asynchronous I/O</title>
      <link>https://demo.example/postgres-18?utm_source=rss</link>
      <guid>demo-feed-1</guid>
      <description>&lt;p&gt;A &lt;b&gt;big&lt;/b&gt; change to the storage layer.&lt;/p&gt;</description>
      <pubDate>{today} 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>A tiny CSS reset for 2026</title>
      <link>https://demo.example/css-reset</link>
      <guid>demo-feed-2</guid>
      <description>Six rules, nothing else.</description>
      <pubDate>{today} 08:10:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

FEED_ITEM = """    <item>
      <title>{title}</title>
      <link>https://demo.example/{slug}</link>
      <guid>demo-feed-{guid}</guid>
      <description>{summary}</description>
      <pubDate>{today} 07:30:00 GMT</pubDate>
    </item>
"""

API_PAYLOAD = {
    "meta": {"generated": "demo"},
    "results": [
        {
            "id": 9001,
            "name": "widget-cli 3.2.0",
            "html_url": "https://demo.example/releases/9001",
            "body": "Adds a --dry-run flag and fixes two crashes.",
            "author": {"login": "release-bot"},
            "published_at": "{iso}T11:00:00Z",
        },
        {
            "id": 9002,
            "name": "widget-cli 3.2.1",
            "html_url": "https://demo.example/releases/9002",
            "body": "Reverts the crash fix that caused a worse crash.",
            "author": {"login": "release-bot"},
            "published_at": "{iso}T12:30:00Z",
        },
    ],
}

PAGE_HTML = """<!doctype html>
<html><body>
  <main>
    <div class="post">
      <h3 class="post-title"><a href="/blog/typed-config">Typed configuration, finally</a></h3>
      <div class="post-body">Why we moved the schema into the config loader.</div>
      <abbr class="published" title="{iso}T08:15:00Z">today</abbr>
    </div>
    <div class="post">
      <h3 class="post-title"><a href="/blog/one-digest">One digest a day</a></h3>
      <div class="post-body">A constraint that removed three bugs.</div>
      <abbr class="published" title="{iso}T06:40:00Z">today</abbr>
    </div>
  </main>
</body></html>
"""

# The same page after a redesign: the selectors no longer match anything.
PAGE_HTML_REDESIGNED = """<!doctype html>
<html><body>
  <main>
    <article class="entry"><h2><a href="/blog/typed-config">Typed configuration, finally</a></h2></article>
  </main>
</body></html>
"""


class State:
    """Mutable server state, so the demo can break things on purpose."""

    def __init__(self) -> None:
        today = date.today()
        self.rfc = today.strftime("%a, %d %b %Y")
        iso = today.isoformat()
        self.feed = FEED_DAY_1.format(today=self.rfc)
        self.api = json.dumps(
            json.loads(json.dumps(API_PAYLOAD).replace("{iso}", iso)), indent=2
        )
        self.page = PAGE_HTML.format(iso=iso)
        self.api_status = 200
        self.webhook_status = 200

    def publish(self, guid: int, title: str, slug: str, summary: str) -> None:
        """Add a genuinely new entry to the feed, with its own guid and link."""
        entry = FEED_ITEM.format(
            guid=guid, title=title, slug=slug, summary=summary, today=self.rfc
        )
        self.feed = self.feed.replace("  </channel>", entry + "  </channel>")


STATE = State()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep the demo output readable
        pass

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/feed.xml"):
            self._send(200, STATE.feed, "application/rss+xml; charset=utf-8")
        elif self.path.startswith("/api/releases"):
            if STATE.api_status != 200:
                self._send(STATE.api_status, '{"message":"Not Found"}', "application/json")
            else:
                self._send(200, STATE.api, "application/json")
        elif self.path.startswith("/blog"):
            self._send(200, STATE.page, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if STATE.webhook_status == 200:
            self._send(200, "ok", "text/plain")
        else:
            self._send(STATE.webhook_status, "simulated webhook outage", "text/plain")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


CONFIG = """
[bot]
timezone = "UTC"
send_after = "00:00"
title = "Demo Digest"
max_items_per_digest = 20
max_items_per_source = 10
lookback_hours = 72
post_when_empty = true
max_silence_hours = 26
unhealthy_after_consecutive_failures = 2
dedupe_by = ["url", "title"]

[paths]
database = "{workspace}/data/digest.db"
log_file = "{workspace}/logs/digest-bot.log"
log_level = "INFO"

[delivery]
kind = "{delivery_kind}"
channel = "#demo"
path = "{workspace}/channel"
webhook_url = "http://127.0.0.1:{port}/webhook"

[alerts]
enabled = true
kind = "file"
channel = "#demo-alerts"
path = "{workspace}/alerts"

[[sources]]
name = "demo-feed"
kind = "rss"
label = "Demo RSS feed"
url = "http://127.0.0.1:{port}/feed.xml"

[[sources]]
name = "demo-api"
kind = "api"
label = "Demo JSON API"
url = "http://127.0.0.1:{port}/api/releases"
items_path = "results"
fields = {{ id = "id", title = "name", url = "html_url", summary = "body", author = "author.login", published_at = "published_at" }}

[[sources]]
name = "demo-page"
kind = "scrape"
label = "Demo scraped page"
url = "http://127.0.0.1:{port}/blog"
selectors = {{ item = "div.post", title = "h3.post-title a", url = "h3.post-title a@href", summary = "div.post-body", published_at = "abbr.published@title" }}
"""


def banner(step: str, title: str) -> None:
    print(f"\n{'=' * 78}\n  {step}  {title}\n{'=' * 78}")


def run(config: Path, *args: str, expect: int | None = 0) -> int:
    printable = " ".join(args)
    print(f"\n$ digest-bot {printable}")
    code = cli.main(["--config", str(config), *args])
    print(f"  -> exit {code}")
    if expect is not None and code != expect:
        raise SystemExit(f"FAILED: expected exit {expect} from `{printable}`, got {code}")
    return code


def rewind_a_day(database: Path) -> None:
    """Shift every recorded digest back one day, freeing up today's slot.

    A demo cannot wait 24 hours; this is the one piece of sleight of hand. The
    shift walks oldest-first, because ``digests.digest_date`` is UNIQUE -- the
    constraint that enforces one digest a day -- and moving the newest row
    first would collide with the row already sitting on that date.
    """
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, digest_date FROM digests ORDER BY digest_date ASC"
        ).fetchall()
        for digest_id, digest_date in rows:
            shifted = (date.fromisoformat(digest_date) - timedelta(days=1)).isoformat()
            connection.execute(
                "UPDATE digests SET digest_date = ? WHERE id = ?", (shifted, digest_id)
            )
        connection.commit()
    finally:
        connection.close()
    print(f"\n  [demo] shifted {len(rows)} recorded digest(s) back a day to free up today's slot")


def show_channel(workspace: Path) -> None:
    # Newest by write time: the digest just posted, not the alphabetically last.
    files = sorted((workspace / "channel").glob("*.md"), key=lambda path: path.stat().st_mtime)
    if not files:
        print("\n  (nothing in the channel)")
        return
    latest = files[-1]
    print(f"\n  --- posted to the channel: {latest.name} ---")
    for line in latest.read_text(encoding="utf-8").splitlines():
        print(f"  | {line}")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    # An explicit path keeps the demo data around for `digest-bot serve`.
    if args:
        workspace = Path(args[0]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        for stale in ("data", "logs", "channel", "alerts"):
            target = workspace / stale
            if target.is_dir():
                for child in target.iterdir():
                    child.unlink()
    else:
        workspace = Path(tempfile.mkdtemp(prefix="digest-bot-demo-"))
    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def write_config(delivery_kind: str) -> Path:
        path = workspace / "config.toml"
        path.write_text(
            CONFIG.format(
                workspace=workspace.as_posix(), port=port, delivery_kind=delivery_kind
            ),
            encoding="utf-8",
        )
        return path

    config = write_config("file")
    database = workspace / "data" / "digest.db"

    try:
        print(f"demo server listening on 127.0.0.1:{port}")
        print(f"workspace: {workspace}")

        banner("1/6", "A healthy day: three sources, one digest")
        run(config, "init")
        run(config, "run", "--trigger", "demo")
        show_channel(workspace)

        banner("2/6", "The same day again: nothing new, nothing posted twice")
        run(config, "run", "--trigger", "demo")
        print("\n  Deduplication: every item was already on record, so items_new is 0,")
        print("  and the once-a-day guard means the channel still holds one digest.")
        run(config, "status")

        banner("3/6", "A new day, with the page redesigned and the API down")
        STATE.publish(3, "SQLite ships a new query planner", "sqlite-planner",
                      "Faster joins on wide tables.")
        STATE.page = PAGE_HTML_REDESIGNED
        STATE.api_status = 404
        rewind_a_day(database)
        # Two sources are broken; the digest must still go out and say so.
        run(config, "run", "--trigger", "demo")
        show_channel(workspace)
        print("\n  Note the 'Collection problems' section above: a failing source is")
        print("  reported in the channel, not just buried in the log.")

        print("\n  One failure is 'degraded', which is not worth paging anyone over:")
        run(config, "healthcheck")
        print("\n  Collecting again keeps the digest alone but records a second failure,")
        print("  crossing unhealthy_after_consecutive_failures = 2:")
        run(config, "collect")
        run(config, "healthcheck", expect=cli.EXIT_UNHEALTHY)
        print("\n  Now healthcheck exits 4, so cron or an uptime monitor notices.")

        banner("4/6", "Everything recovers, and --strict is honoured")
        STATE.page = PAGE_HTML.format(iso=date.today().isoformat())
        STATE.api_status = 200
        rewind_a_day(database)
        run(config, "run", "--trigger", "demo", "--strict")
        show_channel(workspace)

        banner("5/6", "The webhook goes down: the digest fails loudly, nothing is lost")
        config = write_config("slack")
        STATE.webhook_status = 500
        STATE.publish(4, "A story that must survive a failed delivery", "survives",
                      "This item is collected before the webhook breaks.")
        STATE.publish(5, "A second story in the undelivered digest", "survives-too",
                      "Both of these have to reappear once the webhook recovers.")
        rewind_a_day(database)
        run(config, "run", "--trigger", "demo", expect=cli.EXIT_DELIVERY_FAILED)
        run(config, "healthcheck", expect=cli.EXIT_UNHEALTHY)
        pending = sqlite3.connect(database).execute(
            "SELECT COUNT(*) FROM items WHERE digest_id IS NULL AND expired_at IS NULL"
        ).fetchone()[0]
        print(f"\n  {pending} item(s) were released back into the queue, so none were lost.")
        alerts = sorted((workspace / "alerts").glob("*.md"))
        print(f"  {len(alerts)} alert(s) written to the alert channel: {[a.name for a in alerts]}")

        banner("6/6", "The webhook recovers: the same items go out intact")
        STATE.webhook_status = 200
        run(config, "run", "--trigger", "demo")
        run(config, "healthcheck")
        run(config, "digests")

        banner("--", "The bot's own log, last 20 lines")
        log = (workspace / "logs" / "digest-bot.log").read_text(encoding="utf-8").splitlines()
        for line in log[-20:]:
            print(f"  | {line}")

        print(f"\n{'=' * 78}")
        print("  Demo finished. Everything above ran against a real HTTP server.")
        print(f"  Workspace kept for inspection: {workspace}")
        print(f"    database : {database}")
        print(f"    log      : {workspace / 'logs' / 'digest-bot.log'}")
        print(f"    channel  : {workspace / 'channel'}")
        print(f"\n  To browse it in the dashboard:")
        print(f"    digest-bot --config {workspace / 'config.toml'} serve")
        print(f"{'=' * 78}")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
