"""The bot's own log.

Two sinks, always both:

* a rotating file (``logs/digest-bot.log``) plus stderr, for humans and cron;
* the ``events`` table in SQLite, for the dashboard.

The design rule for this project is that nothing fails quietly. Every WARNING
and above is mirrored into the database by :class:`StoreLogHandler`, so a
failure is visible in the dashboard even if nobody ever opens the log file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .store import Store

LOGGER_NAME = "digestbot"

FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
CONSOLE_FORMAT = "%(levelname)-8s %(message)s"


class StoreLogHandler(logging.Handler):
    """Mirror log records into the ``events`` table.

    Only WARNING and above by default: the point is that problems end up
    somewhere queryable, not that the database becomes a second debug log.
    """

    def __init__(self, store: Store, *, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail: dict[str, Any] = {}
            if record.exc_info:
                detail["exception"] = self.format_exception(record)
            for key in ("source", "event", "run_id", "digest_id", "detail"):
                value = getattr(record, key, None)
                if value is not None and key not in {"source", "event", "run_id"}:
                    detail[key] = value
            self.store.record_event(
                level=record.levelname,
                message=record.getMessage(),
                source=getattr(record, "source", None),
                event=getattr(record, "event", record.module),
                run_id=getattr(record, "run_id", None),
                detail=detail or None,
            )
        except Exception:  # pragma: no cover - a logging sink must never raise
            # Logging must not be able to break a run; fall back to stderr.
            self.handleError(record)

    def format_exception(self, record: logging.LogRecord) -> str:
        formatter = self.formatter or logging.Formatter()
        return formatter.formatException(record.exc_info) if record.exc_info else ""


def setup_logging(
    *,
    log_file: str | os.PathLike[str] | None = "logs/digest-bot.log",
    level: str | int = "INFO",
    max_bytes: int = 5_000_000,
    backups: int = 5,
    console: bool = True,
    quiet: bool = False,
) -> logging.Logger:
    """Configure and return the bot's logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    # Drop handlers from a previous call so repeated setup does not duplicate
    # every line (matters in tests and in long-lived processes).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False

    resolved = logging.getLevelName(level) if isinstance(level, str) else level
    if not isinstance(resolved, int):
        resolved = logging.INFO

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        file_handler.setLevel(resolved)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        # When piping output somewhere else, keep the console to real problems
        # but never silence them entirely.
        stream.setLevel(logging.WARNING if quiet else resolved)
        stream.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        logger.addHandler(stream)

    if not logger.handlers:
        # Never leave the logger sink-less: a bot with no log cannot fail loudly.
        logger.addHandler(logging.StreamHandler(sys.stderr))

    return logger


def attach_store(logger: logging.Logger, store: Store, *, level: int = logging.WARNING) -> StoreLogHandler:
    """Start mirroring warnings and errors into the database."""
    for handler in list(logger.handlers):
        if isinstance(handler, StoreLogHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = StoreLogHandler(store, level=level)
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    logger.addHandler(handler)
    return handler


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
