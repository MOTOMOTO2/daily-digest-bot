"""Health assessment.

The bot's worst failure is going quiet, so "quiet" is itself a measured
condition: if no digest has been delivered within ``max_silence_hours``, the
bot reports itself unhealthy, ``digest-bot healthcheck`` exits non-zero, and
the dashboard turns red. Point an uptime monitor at that exit code and
silence becomes an alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .models import parse_iso, utcnow
from .store import Store, row_to_dict

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"

# Worst-first, used to combine individual checks into one overall status.
SEVERITY = {OK: 0, UNKNOWN: 1, DEGRADED: 2, DOWN: 3}


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class HealthReport:
    status: str
    checks: list[Check] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    last_digest: dict[str, Any] | None = None
    last_run: dict[str, Any] | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    hours_since_digest: float | None = None
    generated_at: str = ""
    config_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.status == OK

    def problems(self) -> list[Check]:
        return [check for check in self.checks if check.status != OK]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "healthy": self.healthy,
            "hours_since_digest": self.hours_since_digest,
            "checks": [check.as_dict() for check in self.checks],
            "sources": self.sources,
            "last_digest": self.last_digest,
            "last_run": self.last_run,
            "stats": self.stats,
            "generated_at": self.generated_at,
            "config": self.config_summary,
        }


def _combine(checks: list[Check]) -> str:
    if not checks:
        return UNKNOWN
    return max((check.status for check in checks), key=lambda status: SEVERITY.get(status, 0))


def assess(config: Config, store: Store, *, now: datetime | None = None) -> HealthReport:
    moment = now or utcnow()
    checks: list[Check] = []

    # --- has a digest gone out recently? ------------------------------
    last_digest = row_to_dict(store.last_sent_digest())
    hours_since: float | None = None
    if last_digest is None:
        checks.append(
            Check(
                "digest_delivered",
                UNKNOWN,
                "no digest has ever been delivered; run `digest-bot run --force` once to prove the path works",
            )
        )
    else:
        sent_at = parse_iso(last_digest.get("sent_at"))
        if sent_at is None:
            checks.append(Check("digest_delivered", UNKNOWN, "last digest has no send timestamp"))
        else:
            hours_since = (moment - sent_at).total_seconds() / 3600.0
            limit = config.bot.max_silence_hours
            if hours_since > limit * 2:
                checks.append(
                    Check(
                        "digest_delivered",
                        DOWN,
                        f"no digest for {hours_since:.1f}h, more than twice the {limit}h limit",
                    )
                )
            elif hours_since > limit:
                checks.append(
                    Check(
                        "digest_delivered",
                        DEGRADED,
                        f"no digest for {hours_since:.1f}h, over the {limit}h limit",
                    )
                )
            else:
                checks.append(
                    Check("digest_delivered", OK, f"last digest {hours_since:.1f}h ago")
                )

    # --- is a digest stuck undelivered? -------------------------------
    undelivered = [row_to_dict(row) for row in store.undelivered_digests()]
    stuck = [row for row in undelivered if row and row["status"] == "failed"]
    if stuck:
        dates = ", ".join(str(row["digest_date"]) for row in stuck[:5])
        checks.append(
            Check(
                "delivery",
                DOWN,
                f"{len(stuck)} digest(s) failed to deliver and are still unsent: {dates}",
            )
        )
    else:
        checks.append(Check("delivery", OK, "no failed digests outstanding"))

    # --- last run ------------------------------------------------------
    last_run = row_to_dict(store.last_run())
    if last_run is None:
        checks.append(Check("last_run", UNKNOWN, "the bot has never completed a run"))
    elif last_run["status"] == "failed":
        checks.append(
            Check("last_run", DOWN, f"last run failed: {last_run.get('error') or 'see the log'}")
        )
    elif last_run["status"] == "partial":
        checks.append(Check("last_run", DEGRADED, "last run completed with source failures"))
    else:
        finished = parse_iso(last_run.get("finished_at"))
        when = (
            finished.astimezone(ZoneInfo(config.bot.timezone)).strftime("%Y-%m-%d %H:%M %Z")
            if finished
            else "an unknown time"
        )
        checks.append(Check("last_run", OK, f"last run ok at {when}"))

    # --- per-source health ---------------------------------------------
    sources: list[dict[str, Any]] = []
    broken: list[str] = []
    threshold = config.bot.unhealthy_after_consecutive_failures
    for source in config.sources:
        stats = store.source_stats(source.name)
        failures = int(stats["consecutive_failures"])
        if not source.enabled:
            status = "disabled"
        elif stats["last_status"] is None:
            status = UNKNOWN
        elif failures >= threshold:
            status = DOWN
        elif failures > 0:
            status = DEGRADED
        else:
            status = OK
        if status == DOWN:
            broken.append(f"{source.name} ({failures} failures)")
        sources.append(
            {
                **stats,
                "kind": source.kind,
                "url": source.url,
                "label": source.display_name,
                "enabled": source.enabled,
                "status": status,
            }
        )

    if broken:
        checks.append(
            Check(
                "sources",
                DOWN,
                f"{len(broken)} source(s) failing at least {threshold} times in a row: "
                + ", ".join(broken),
            )
        )
    else:
        degraded = [s["source"] for s in sources if s["status"] == DEGRADED]
        if degraded:
            checks.append(
                Check("sources", DEGRADED, "recent failures on: " + ", ".join(degraded))
            )
        else:
            enabled = sum(1 for s in sources if s["enabled"])
            checks.append(Check("sources", OK, f"all {enabled} enabled source(s) healthy"))

    # --- recent errors in the bot's own log ----------------------------
    recent_errors = store.count_events_since(moment - timedelta(hours=24))
    if recent_errors:
        checks.append(
            Check(
                "log",
                DEGRADED,
                f"{recent_errors} error-level event(s) logged in the last 24h",
            )
        )
    else:
        checks.append(Check("log", OK, "no errors logged in the last 24h"))

    tz = ZoneInfo(config.bot.timezone)
    return HealthReport(
        status=_combine(checks),
        checks=checks,
        sources=sources,
        last_digest=last_digest,
        last_run=last_run,
        stats=store.stats(),
        hours_since_digest=round(hours_since, 2) if hours_since is not None else None,
        generated_at=moment.astimezone(tz).isoformat(),
        config_summary={
            "timezone": config.bot.timezone,
            "send_after": config.bot.send_after,
            "channel": config.delivery.channel,
            "delivery_kind": config.delivery.kind,
            "alerts_enabled": config.alerts.enabled,
            "max_silence_hours": config.bot.max_silence_hours,
            "post_when_empty": config.bot.post_when_empty,
            "log_file": config.paths.log_file,
            "database": config.paths.database,
            "source_count": len(config.sources),
            "enabled_source_count": len(config.enabled_sources),
        },
    )
