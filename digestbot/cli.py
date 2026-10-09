"""Command line entry point.

Exit codes matter here: this thing runs under cron or Task Scheduler, and the
exit code is how the surrounding system learns something went wrong.

    0  all good
    1  unexpected crash
    2  the digest could not be delivered
    3  the run finished but at least one source failed (with --strict)
    4  healthcheck says the bot is unhealthy
    5  bad configuration
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import Any, Sequence

from . import __version__
from .config import Config, ConfigError, load_config
from .delivery import DeliveryError, build_channel
from .health import DOWN, assess
from .logging_setup import attach_store, get_logger, setup_logging
from .models import utcnow
from .pipeline import Pipeline
from .store import Store, StoreError, row_to_dict

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_DELIVERY_FAILED = 2
EXIT_SOURCE_FAILED = 3
EXIT_UNHEALTHY = 4
EXIT_CONFIG = 5

log = get_logger("cli")


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digest-bot",
        description="Collect from RSS, APIs and scraped pages; post one deduplicated digest a day.",
    )
    parser.add_argument("--version", action="version", version=f"daily-digest-bot {__version__}")
    parser.add_argument("-c", "--config", default="config.toml", help="path to the TOML config")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--quiet", action="store_true", help="only print warnings and errors to the console"
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect, then deliver today's digest if it is due")
    run.add_argument(
        "--force", action="store_true", help="ignore the send window and the once-a-day guard"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="build the digest and print it, deliver nothing"
    )
    run.add_argument(
        "--strict", action="store_true", help="exit 3 if any source failed during collection"
    )
    run.add_argument("--trigger", default="manual", help="label recorded with the run (e.g. cron)")

    collect = sub.add_parser("collect", help="poll sources and record new items, send nothing")
    collect.add_argument("--strict", action="store_true", help="exit 3 if any source failed")

    sub.add_parser("healthcheck", help="exit non-zero if the bot is unhealthy or has gone quiet")
    sub.add_parser("status", help="human-readable summary of the local record")
    sub.add_parser("init", help="create the database and verify the config")
    sub.add_parser("test-channel", help="post a test message to the configured channel")
    sub.add_parser("sources", help="list configured sources and their health")

    preview = sub.add_parser("preview", help="render today's digest to stdout without sending")
    preview.add_argument("--limit", type=int, default=None, help="cap the number of items shown")

    digests = sub.add_parser("digests", help="list recorded digests")
    digests.add_argument("--limit", type=int, default=10)
    digests.add_argument("--show", type=int, default=None, metavar="ID", help="print one digest body")

    events = sub.add_parser("log", help="show the bot's recent warnings and errors")
    events.add_argument("--limit", type=int, default=25)
    events.add_argument(
        "--level",
        action="append",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="filter by level; repeatable",
    )

    prune = sub.add_parser("prune", help="drop old raw payloads, events and runs")
    prune.add_argument("--days", type=int, default=90)

    serve = sub.add_parser("serve", help="run the read-only dashboard API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    return parser


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _bootstrap(args: argparse.Namespace) -> tuple[Config, Store]:
    """Load config, set up the log and the store. Raises ConfigError/StoreError."""
    config = load_config(args.config)
    setup_logging(
        log_file=config.paths.log_file,
        level=args.log_level or config.paths.log_level,
        max_bytes=config.paths.log_max_bytes,
        backups=config.paths.log_backups,
        quiet=args.quiet or args.json,
    )
    store = Store(config.paths.database)
    attach_store(get_logger(), store, level=logging.WARNING)
    return config, store


def _emit(payload: dict[str, Any], *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def _status_mark(status: str) -> str:
    return {"ok": "[ok]", "degraded": "[warn]", "down": "[DOWN]", "unknown": "[?]", "disabled": "[off]"}.get(
        status, f"[{status}]"
    )


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, config: Config, store: Store) -> int:
    pipeline = Pipeline(config, store)
    result = pipeline.run(trigger=args.trigger, force=args.force, dry_run=args.dry_run)

    failed = [outcome.name for outcome in result.collection.failed_sources]
    summary = {
        "run_id": result.run_id,
        "status": result.status,
        "sent": result.sent,
        "digest_id": result.digest_id,
        "items_in_digest": result.digest.item_count if result.digest else 0,
        "items_fetched": result.collection.items_fetched,
        "items_new": result.collection.items_new,
        "items_duplicate": result.collection.items_duplicate,
        "items_expired": result.expired_items,
        "failed_sources": failed,
        "skipped_reason": result.skipped_reason,
        "delivery_error": result.delivery_error,
    }

    lines = [
        f"run {result.run_id}: {result.status}",
        f"  fetched {result.collection.items_fetched}, new {result.collection.items_new}, "
        f"duplicate {result.collection.items_duplicate}",
    ]
    if result.sent:
        lines.append(
            f"  delivered {summary['items_in_digest']} item(s) to "
            f"{config.delivery.kind}:{config.delivery.channel}"
        )
    elif result.skipped_reason:
        lines.append(f"  not sent: {result.skipped_reason}")
    elif result.delivery_error:
        lines.append(f"  DELIVERY FAILED: {result.delivery_error}")
    if failed:
        lines.append(f"  failed sources: {', '.join(failed)}")
    if args.dry_run and result.digest:
        lines.append("")
        lines.append(result.digest.subject)
        lines.append("=" * len(result.digest.subject))
        lines.append(result.digest.body)

    _emit(summary, as_json=args.json, text="\n".join(lines))

    if result.status == "failed":
        return EXIT_DELIVERY_FAILED
    if failed and getattr(args, "strict", False):
        return EXIT_SOURCE_FAILED
    return EXIT_OK


def cmd_collect(args: argparse.Namespace, config: Config, store: Store) -> int:
    pipeline = Pipeline(config, store)
    run_id = store.start_run("collect")
    report = pipeline.collect(run_id=run_id)
    failed = [outcome.name for outcome in report.failed_sources]
    store.finish_run(
        run_id,
        status="partial" if failed else "ok",
        items_fetched=report.items_fetched,
        items_new=report.items_new,
        sent=False,
        notes="collect only",
    )

    lines = [f"collected {report.items_new} new item(s) of {report.items_fetched} fetched"]
    for outcome in report.outcomes:
        mark = "[ok]" if outcome.ok else "[FAIL]"
        detail = (
            f"{outcome.items_fetched} fetched, {outcome.items_new} new, "
            f"{outcome.items_duplicate} dup, {outcome.duration_ms}ms"
            if outcome.ok
            else f"{outcome.error_type}: {outcome.error}"
        )
        lines.append(f"  {mark} {outcome.name}: {detail}")
    lines.append(f"  {store.count_pending()} item(s) waiting for the next digest")

    _emit(
        {
            "run_id": run_id,
            "items_fetched": report.items_fetched,
            "items_new": report.items_new,
            "items_duplicate": report.items_duplicate,
            "pending": store.count_pending(),
            "failed_sources": failed,
            "sources": [asdict(outcome) for outcome in report.outcomes],
        },
        as_json=args.json,
        text="\n".join(lines),
    )
    if failed and args.strict:
        return EXIT_SOURCE_FAILED
    return EXIT_OK


def cmd_healthcheck(args: argparse.Namespace, config: Config, store: Store) -> int:
    report = assess(config, store)
    lines = [f"status: {report.status}"]
    for check in report.checks:
        lines.append(f"  {_status_mark(check.status)} {check.name}: {check.detail}")
    _emit(report.as_dict(), as_json=args.json, text="\n".join(lines))

    if report.status == DOWN:
        # Log it too, so the failure is in the bot's own record and not only
        # in whatever ran the healthcheck.
        log.error(
            "healthcheck reports DOWN: %s",
            "; ".join(f"{c.name}: {c.detail}" for c in report.problems()),
            extra={"event": "healthcheck.down"},
        )
        return EXIT_UNHEALTHY
    return EXIT_OK


def cmd_status(args: argparse.Namespace, config: Config, store: Store) -> int:
    report = assess(config, store)
    stats = report.stats
    lines = [
        f"daily-digest-bot {__version__}  ({config.config_path})",
        f"status            : {report.status}",
        f"channel           : {config.delivery.kind}:{config.delivery.channel}",
        f"timezone          : {config.bot.timezone} (send after {config.bot.send_after})",
        f"database          : {stats['database']}",
        f"log               : {config.paths.log_file}",
        f"items             : {stats['items_total']} total, {stats['items_pending']} pending, "
        f"{stats['items_sent']} sent, {stats['items_expired']} expired",
        f"digests           : {stats['digests_sent']} sent, {stats['digests_failed']} failed, "
        f"{stats['digests_total']} total",
        f"runs              : {stats['runs_total']}",
    ]
    if report.last_digest:
        lines.append(
            f"last digest       : {report.last_digest['digest_date']} "
            f"({report.last_digest['item_count']} items, {report.hours_since_digest}h ago)"
        )
    else:
        lines.append("last digest       : never")
    lines.append("")
    lines.append("checks:")
    for check in report.checks:
        lines.append(f"  {_status_mark(check.status)} {check.name}: {check.detail}")
    lines.append("")
    lines.append("sources:")
    for source in report.sources:
        lines.append(
            f"  {_status_mark(source['status'])} {source['source']} ({source['kind']}) "
            f"- {source['items_sent_total']} item(s) posted"
            + (f" - last error: {source['last_error']}" if source["last_error"] and source["status"] != "ok" else "")
        )
    _emit(report.as_dict(), as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def cmd_sources(args: argparse.Namespace, config: Config, store: Store) -> int:
    report = assess(config, store)
    lines = []
    for source in report.sources:
        lines.append(f"{_status_mark(source['status'])} {source['source']}  [{source['kind']}]")
        lines.append(f"    url            : {source['url']}")
        lines.append(f"    last checked   : {source['last_checked_at'] or 'never'}")
        lines.append(f"    last success   : {source['last_success_at'] or 'never'}")
        lines.append(
            f"    checks         : {source['successful_checks']}/{source['checks']} ok, "
            f"{source['consecutive_failures']} failing in a row"
        )
        lines.append(f"    items posted   : {source['items_sent_total']}")
        if source["last_error"]:
            lines.append(f"    last error     : {source['last_error']}")
    _emit({"sources": report.sources}, as_json=args.json, text="\n".join(lines) or "no sources")
    return EXIT_OK


def cmd_init(args: argparse.Namespace, config: Config, store: Store) -> int:
    stats = store.stats()
    lines = [
        "config is valid",
        f"  sources   : {len(config.enabled_sources)} enabled of {len(config.sources)}",
        f"  channel   : {config.delivery.kind}:{config.delivery.channel}",
        f"  database  : {stats['database']} (schema ready)",
        f"  log       : {config.paths.log_file}",
        "",
        "next: `digest-bot test-channel` to prove delivery works, "
        "then `digest-bot run --dry-run` to see a digest.",
    ]
    log.info("init completed", extra={"event": "init"})
    _emit({"ok": True, "config": config.redacted(), "stats": stats}, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def cmd_test_channel(args: argparse.Namespace, config: Config, store: Store) -> int:
    channel = build_channel(config.delivery)
    subject = "daily-digest-bot test message"
    body = (
        "If you can read this, delivery works.\n\n"
        f"- channel: {channel.describe()}\n"
        f"- sent at: {utcnow().isoformat()}\n"
        f"- log: {config.paths.log_file}"
    )
    try:
        receipt = channel.send(subject, body)
    except DeliveryError as exc:
        log.critical(
            "test message to %s FAILED: %s",
            channel.describe(),
            exc,
            extra={"event": "test_channel.failed"},
        )
        _emit(
            {"ok": False, "channel": channel.describe(), "error": str(exc)},
            as_json=args.json,
            text=f"FAILED to post to {channel.describe()}: {exc}",
        )
        return EXIT_DELIVERY_FAILED
    log.info("test message delivered to %s", channel.describe(), extra={"event": "test_channel.ok"})
    _emit(
        {"ok": True, "channel": channel.describe(), "receipt": receipt},
        as_json=args.json,
        text=f"posted a test message to {channel.describe()}",
    )
    return EXIT_OK


def cmd_preview(args: argparse.Namespace, config: Config, store: Store) -> int:
    from datetime import timedelta

    from .render import render_digest

    pipeline = Pipeline(config, store)
    cutoff = utcnow() - timedelta(hours=config.bot.lookback_hours)
    limit = args.limit or config.bot.max_items_per_digest
    pending = store.pending_items(since=cutoff, limit=limit)
    channel_kind = config.delivery.kind
    rendered = render_digest(
        pending,
        [],
        title=config.bot.title,
        digest_date=pipeline.today(),
        timezone_name=config.bot.timezone,
        flavour="slack" if channel_kind == "slack" else "markdown",
        labels={source.name: source.display_name for source in config.sources},
        pending_overflow=max(store.count_pending() - len(pending), 0),
    )
    _emit(
        {"subject": rendered.subject, "body": rendered.body, "item_count": rendered.item_count},
        as_json=args.json,
        text=f"{rendered.subject}\n{'=' * len(rendered.subject)}\n{rendered.body}",
    )
    return EXIT_OK


def cmd_digests(args: argparse.Namespace, config: Config, store: Store) -> int:
    if args.show is not None:
        row = row_to_dict(store.get_digest(args.show))
        if row is None:
            print(f"no digest with id {args.show}", file=sys.stderr)
            return EXIT_CRASH
        items = [
            {"title": item.title, "url": item.url, "source": item.source}
            for item in store.items_for_digest(args.show)
        ]
        _emit(
            {**row, "items": items},
            as_json=args.json,
            text=f"{row['title']} [{row['status']}]\n\n{row['body'] or '(no body recorded)'}",
        )
        return EXIT_OK

    rows = [row_to_dict(row) for row in store.recent_digests(args.limit)]
    lines = []
    for row in rows:
        assert row is not None
        lines.append(
            f"{row['id']:>4}  {row['digest_date']}  {row['status']:<8} "
            f"{row['item_count']:>3} item(s)  {row['channel']}"
            + (f"  error: {row['last_error']}" if row["last_error"] else "")
        )
    _emit({"digests": rows}, as_json=args.json, text="\n".join(lines) or "no digests recorded yet")
    return EXIT_OK


def cmd_log(args: argparse.Namespace, config: Config, store: Store) -> int:
    rows = [row_to_dict(row) for row in store.recent_events(limit=args.limit, levels=args.level)]
    lines = []
    for row in rows:
        assert row is not None
        where = f" [{row['source']}]" if row["source"] else ""
        lines.append(f"{row['ts']}  {row['level']:<8}{where} {row['message']}")
    _emit(
        {"events": rows, "log_file": config.paths.log_file},
        as_json=args.json,
        text="\n".join(lines) or f"no events recorded; the full log is at {config.paths.log_file}",
    )
    return EXIT_OK


def cmd_prune(args: argparse.Namespace, config: Config, store: Store) -> int:
    result = store.prune(days=args.days)
    log.info("pruned history older than %d days: %s", args.days, result, extra={"event": "prune"})
    _emit(
        result,
        as_json=args.json,
        text=(
            f"cleared {result['raw_payloads_cleared']} raw payload(s), "
            f"deleted {result['events_deleted']} event(s) and {result['runs_deleted']} run(s). "
            "Fingerprints were kept, so deduplication is unaffected."
        ),
    )
    return EXIT_OK


def cmd_serve(args: argparse.Namespace, config: Config, store: Store) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "the dashboard API needs the server extra: pip install 'fastapi' 'uvicorn'",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    from .api import create_app

    store.close()  # the app opens its own connection per request
    app = create_app(config_path=str(args.config))
    log.info(
        "serving the dashboard API on http://%s:%d", args.host, args.port, extra={"event": "serve"}
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "collect": cmd_collect,
    "healthcheck": cmd_healthcheck,
    "status": cmd_status,
    "sources": cmd_sources,
    "init": cmd_init,
    "test-channel": cmd_test_channel,
    "preview": cmd_preview,
    "digests": cmd_digests,
    "log": cmd_log,
    "prune": cmd_prune,
    "serve": cmd_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config, store = _bootstrap(args)
    except ConfigError as exc:
        # No log file is configured yet at this point, so stderr is the record.
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except StoreError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return EXIT_CRASH

    handler = COMMANDS[args.command]
    try:
        return handler(args, config, store)
    except KeyboardInterrupt:
        log.warning("interrupted by the user", extra={"event": "interrupted"})
        return EXIT_CRASH
    except Exception as exc:
        log.critical(
            "command %r crashed: %s: %s",
            args.command,
            type(exc).__name__,
            exc,
            exc_info=True,
            extra={"event": "cli.crashed"},
        )
        print(
            f"\n{args.command} failed: {type(exc).__name__}: {exc}\n"
            f"Full traceback in {config.paths.log_file}",
            file=sys.stderr,
        )
        return EXIT_CRASH
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
