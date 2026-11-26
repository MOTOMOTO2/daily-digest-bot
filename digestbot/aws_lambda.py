"""AWS Lambda entry point: the same pipeline the CLI runs, on a schedule.

EventBridge Scheduler invokes the function with one of:

``{"mode": "run"}``
    Collect from every source and, once the day's send window opens, post the
    digest. Scheduled hourly, like the cron job the README suggests.
``{"mode": "healthcheck"}``
    The same checks as ``digest-bot healthcheck``. A DOWN result fails the
    invocation, so the function's error alarm fires.

A failed run or an unhealthy bot raises instead of returning: Lambda then
records the invocation as an error, which is what the CloudWatch alarm in
the template watches. That is the cloud version of the CLI's non-zero exit.

Secrets never sit in the function's environment as plain text. For each
``SSM_<NAME>=/parameter/path`` variable, the value of that SecureString
parameter is read once per container and exported as ``<NAME>``, where the
config's ``${NAME}`` interpolation picks it up.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import load_config
from .health import DOWN, assess
from .logging_setup import attach_store, get_logger, setup_logging
from .pipeline import Pipeline
from .store import open_store

log = logging.getLogger("digestbot.lambda")

_secrets_loaded = False


def load_secrets() -> None:
    """Export ``SSM_<NAME>`` parameters as ``<NAME>``, once per warm container."""
    global _secrets_loaded
    if _secrets_loaded:
        return
    wanted = {
        name.removeprefix("SSM_"): path
        for name, path in os.environ.items()
        if name.startswith("SSM_") and path
    }
    if wanted:
        import boto3

        response = boto3.client("ssm").get_parameters(Names=sorted(set(wanted.values())), WithDecryption=True)
        values = {parameter["Name"]: parameter["Value"] for parameter in response["Parameters"]}
        missing = sorted(path for path in wanted.values() if path not in values)
        if missing:
            raise RuntimeError(f"SSM parameter(s) not found: {', '.join(missing)}")
        for name, path in wanted.items():
            os.environ[name] = values[path]
    _secrets_loaded = True


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    mode = (event or {}).get("mode", "run")
    load_secrets()
    config = load_config(os.environ.get("DIGEST_CONFIG", "config.toml"))
    # CloudWatch collects stdout and stderr; there is no file to rotate here.
    setup_logging(log_file=None, level=config.paths.log_level, console=True)
    store = open_store(config.paths.database)
    attach_store(get_logger(), store, level=logging.WARNING)

    if mode == "run":
        result = Pipeline(config, store).run(trigger="schedule")
        summary = {
            "mode": mode,
            "run_id": result.run_id,
            "status": result.status,
            "sent": result.sent,
            "digest_id": result.digest_id,
            "items_in_digest": result.digest.item_count if result.digest else 0,
            "items_fetched": result.collection.items_fetched,
            "items_new": result.collection.items_new,
            "failed_sources": [outcome.name for outcome in result.collection.failed_sources],
            "skipped_reason": result.skipped_reason,
            "delivery_error": result.delivery_error,
        }
        log.info("lambda run finished: %s", summary, extra={"event": "lambda.run"})
        if result.status == "failed":
            raise RuntimeError(f"digest delivery failed: {result.delivery_error}")
        return summary

    if mode == "healthcheck":
        report = assess(config, store)
        if report.status == DOWN:
            problems = "; ".join(f"{check.name}: {check.detail}" for check in report.problems())
            log.error("healthcheck reports DOWN: %s", problems, extra={"event": "healthcheck.down"})
            raise RuntimeError(f"digest bot is DOWN: {problems}")
        return {"mode": mode, "status": report.status, "checks": [check.as_dict() for check in report.checks]}

    raise ValueError(f"unknown mode {mode!r}; expected 'run' or 'healthcheck'")
