"""The run loop: collect, deduplicate, build one digest a day, deliver, record.

Failure policy, which is the whole point of this module:

* One source failing never aborts the run. It is logged at ERROR, written to
  ``source_results`` and ``events``, and listed inside the digest body.
* Delivery failing is a hard failure: the digest row is marked ``failed``, its
  items are released back to the pending queue so nothing is lost, a CRITICAL
  line goes to the log, the alert channel is tried, and the run exits non-zero.
* Nothing is swallowed. The broadest handler in here re-raises after recording.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Config, SourceConfig
from .delivery import Channel, DeliveryError, build_channel
from .logging_setup import attach_store, get_logger
from .models import Item, SourceOutcome, StoredItem, iso, utcnow
from .render import RenderedDigest, render_digest
from .sources import apply_filters, get_fetcher
from .sources.base import SourceError
from .store import Store

log = get_logger("pipeline")

# Delivery flavour per channel kind: Slack has its own mrkdwn dialect.
SLACK_FLAVOUR_KINDS = {"slack"}


@dataclass
class CollectionReport:
    outcomes: list[SourceOutcome] = field(default_factory=list)
    items_fetched: int = 0
    items_new: int = 0
    items_duplicate: int = 0

    @property
    def failed_sources(self) -> list[SourceOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    @property
    def ok(self) -> bool:
        return not self.failed_sources


@dataclass
class RunResult:
    status: str  # "ok" | "partial" | "failed" | "skipped"
    run_id: int
    collection: CollectionReport
    digest_id: int | None = None
    digest: RenderedDigest | None = None
    sent: bool = False
    skipped_reason: str | None = None
    delivery_error: str | None = None
    expired_items: int = 0
    receipt: dict[str, Any] | None = None

    @property
    def had_source_failures(self) -> bool:
        return bool(self.collection.failed_sources)


class Pipeline:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        # Mirror warnings and errors into the database from here rather than
        # relying on the caller to wire it up: anything holding a Pipeline
        # gets the loud-failure guarantee for free.
        attach_store(get_logger(), store)

    # ------------------------------------------------------------------
    # collection
    # ------------------------------------------------------------------

    def collect(self, run_id: int | None = None) -> CollectionReport:
        """Poll every enabled source. Never raises for a single bad source."""
        report = CollectionReport()
        headers = {"User-Agent": self.config.bot.user_agent}
        with httpx.Client(follow_redirects=True, headers=headers) as client:
            for source in self.config.enabled_sources:
                outcome = self._collect_one(source, client, run_id=run_id)
                report.outcomes.append(outcome)
                report.items_fetched += outcome.items_fetched
                report.items_new += outcome.items_new
                report.items_duplicate += outcome.items_duplicate
                if run_id is not None:
                    self.store.record_source_result(run_id, outcome)
        log.info(
            "collection finished: %d/%d sources ok, %d fetched, %d new, %d duplicate",
            len(report.outcomes) - len(report.failed_sources),
            len(report.outcomes),
            report.items_fetched,
            report.items_new,
            report.items_duplicate,
            extra={"event": "collect.done", "run_id": run_id},
        )
        return report

    def _collect_one(
        self, source: SourceConfig, client: httpx.Client, *, run_id: int | None
    ) -> SourceOutcome:
        started = time.perf_counter()
        outcome = SourceOutcome(name=source.name, kind=source.kind, status="ok")
        try:
            fetcher = get_fetcher(source.kind)
            items = fetcher.fetch(source, client)
            outcome.items_fetched = len(items)

            items, filtered = apply_filters(source, items)
            outcome.items_filtered = filtered

            cap = source.max_items or self.config.bot.max_items_per_source
            if len(items) > cap:
                items = items[:cap]

            new, duplicate, stale = self._store_items(items)
            outcome.items_new = new
            outcome.items_duplicate = duplicate
            outcome.items_stale = stale

            if outcome.items_fetched == 0:
                # Reachable but empty. Not fatal, but never silent either.
                log.warning(
                    "source %r returned no items at all (reachable, but nothing parsed)",
                    source.name,
                    extra={"event": "collect.empty", "source": source.name, "run_id": run_id},
                )
            else:
                log.info(
                    "source %r: %d fetched, %d new, %d duplicate, %d filtered",
                    source.name,
                    outcome.items_fetched,
                    new,
                    duplicate,
                    filtered,
                    extra={"event": "collect.source", "source": source.name, "run_id": run_id},
                )
        except SourceError as exc:
            outcome.status = "error"
            outcome.error = str(exc)
            outcome.error_type = exc.kind
            streak = self.store.consecutive_failures(source.name) + 1
            log.error(
                "source %r failed (%s): %s [%d consecutive failure(s)]",
                source.name,
                exc.kind,
                exc,
                streak,
                extra={"event": "collect.failed", "source": source.name, "run_id": run_id},
            )
        except Exception as exc:  # unexpected bug in an adapter
            outcome.status = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.error_type = "unexpected"
            log.exception(
                "source %r raised an unexpected %s; this is a bug in the adapter",
                source.name,
                type(exc).__name__,
                extra={"event": "collect.crashed", "source": source.name, "run_id": run_id},
            )
        finally:
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    def _store_items(self, items: list[Item]) -> tuple[int, int, int]:
        """Insert items, skipping anything already on record.

        Returns (new, duplicate, stale). Deduplication happens three ways:
        inside the batch, against stored fingerprints, and -- when title
        deduplication is enabled -- against stored normalized titles, which
        catches the same story arriving from two different sources.
        """
        dedupe_by = self.config.bot.dedupe_by
        use_titles = "title" in dedupe_by
        cutoff = utcnow() - timedelta(hours=self.config.bot.lookback_hours)

        fingerprints = [item.fingerprint(dedupe_by) for item in items]
        already = self.store.seen_fingerprints(fingerprints)
        known_titles = (
            self.store.seen_title_keys([item.title_key for item in items]) if use_titles else set()
        )

        new = duplicate = stale = 0
        batch_fingerprints: set[str] = set()
        batch_titles: set[str] = set()

        for item, fingerprint in zip(items, fingerprints):
            if not item.title:
                continue
            if item.published_at is not None and item.published_at < cutoff:
                # Too old to post. Record it anyway so it is deduplicated
                # forever, but mark it expired up front.
                stale += 1
            if fingerprint in already or fingerprint in batch_fingerprints:
                duplicate += 1
                continue
            if use_titles and item.title_key and (
                item.title_key in known_titles or item.title_key in batch_titles
            ):
                duplicate += 1
                continue

            item_id = self.store.add_item(item, fingerprint)
            if item_id is None:
                # Lost a race against another process; the UNIQUE constraint
                # is the authority, so this counts as a duplicate.
                duplicate += 1
                continue
            new += 1
            batch_fingerprints.add(fingerprint)
            if item.title_key:
                batch_titles.add(item.title_key)
        return new, duplicate, stale

    # ------------------------------------------------------------------
    # the daily digest
    # ------------------------------------------------------------------

    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.bot.timezone))

    def today(self) -> str:
        return self.local_now().date().isoformat()

    def send_window_open(self, *, now: datetime | None = None) -> bool:
        moment = now or self.local_now()
        hour, minute = (int(part) for part in self.config.bot.send_after.split(":"))
        return (moment.hour, moment.minute) >= (hour, minute)

    def supersede_old_digests(self, today: str, run_id: int | None = None) -> int:
        """Fold an earlier undelivered digest back into the pending queue.

        Yesterday's failed digest is not re-sent verbatim; its items return to
        the queue and go out in today's digest instead. Loudly logged, because
        it means a previous day's delivery broke.
        """
        released = 0
        for row in self.store.undelivered_digests():
            if row["digest_date"] >= today:
                continue
            count = self.store.release_items(row["id"])
            self.store.mark_digest_skipped(
                row["id"], f"superseded by the digest for {today} after status={row['status']}"
            )
            released += count
            log.warning(
                "digest for %s was never delivered (status=%s, attempts=%s, last_error=%s); "
                "released %d item(s) back into today's digest",
                row["digest_date"],
                row["status"],
                row["attempts"],
                row["last_error"],
                count,
                extra={"event": "digest.superseded", "run_id": run_id},
            )
        return released

    def run(self, *, trigger: str = "manual", force: bool = False, dry_run: bool = False) -> RunResult:
        """Collect and, if the daily slot is open, deliver the digest."""
        run_id = self.store.start_run(trigger)
        log.info(
            "run %d started (trigger=%s, force=%s, dry_run=%s)",
            run_id,
            trigger,
            force,
            dry_run,
            extra={"event": "run.start", "run_id": run_id},
        )
        try:
            return self._run_inner(run_id, trigger=trigger, force=force, dry_run=dry_run)
        except Exception as exc:
            # A crash must leave a trace in the run record and the log, then
            # keep travelling so the exit code is non-zero.
            log.critical(
                "run %d crashed: %s: %s",
                run_id,
                type(exc).__name__,
                exc,
                exc_info=True,
                extra={"event": "run.crashed", "run_id": run_id},
            )
            self.store.finish_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def _run_inner(self, run_id: int, *, trigger: str, force: bool, dry_run: bool) -> RunResult:
        config = self.config
        today = self.today()

        collection = self.collect(run_id=run_id)
        result = RunResult(status="ok", run_id=run_id, collection=collection)

        existing = self.store.get_digest_for_date(today)
        if existing is not None and existing["status"] == "sent" and not force:
            reason = f"a digest for {today} was already sent at {existing['sent_at']}"
            log.info(
                "collection only: %s (use --force to send another)",
                reason,
                extra={"event": "digest.already_sent", "run_id": run_id},
            )
            result.status = "partial" if collection.failed_sources else "ok"
            result.skipped_reason = reason
            result.digest_id = existing["id"]
            self.store.finish_run(
                run_id,
                status=result.status,
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                digest_id=existing["id"],
                sent=False,
                notes=reason,
            )
            return result

        if existing is None and not (force or self.send_window_open()):
            reason = (
                f"before the send window ({config.bot.send_after} {config.bot.timezone}); "
                f"collected {collection.items_new} new item(s) for later"
            )
            log.info("collection only: %s", reason, extra={"event": "digest.waiting", "run_id": run_id})
            result.status = "partial" if collection.failed_sources else "ok"
            result.skipped_reason = reason
            self.store.finish_run(
                run_id,
                status=result.status,
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                sent=False,
                notes=reason,
            )
            return result

        released = self.supersede_old_digests(today, run_id=run_id)
        if released:
            log.info(
                "%d item(s) recovered from undelivered digests",
                released,
                extra={"event": "digest.recovered", "run_id": run_id},
            )

        cutoff = utcnow() - timedelta(hours=config.bot.lookback_hours)
        expired = self.store.expire_stale_pending(cutoff)
        if expired:
            log.info(
                "retired %d pending item(s) older than %dh without posting them",
                expired,
                config.bot.lookback_hours,
                extra={"event": "items.expired", "run_id": run_id},
            )
        result.expired_items = expired

        # Claim (or reuse) today's slot. The UNIQUE index on digest_date is
        # what makes "one digest a day" true even under concurrent runs.
        digest_id = existing["id"] if existing is not None else None
        if digest_id is None:
            digest_id = self.store.create_digest(
                digest_date=today, channel=config.delivery.channel, title=config.bot.title
            )
        result.digest_id = digest_id

        pending = self.store.pending_items(since=cutoff)
        cap = config.bot.max_items_per_digest
        chosen = pending[:cap]
        overflow = max(len(pending) - len(chosen), 0)
        if chosen:
            self.store.attach_items_to_digest([item.id for item in chosen], digest_id)
        # Re-read from the digest so a forced re-send includes items that were
        # already attached on a previous attempt.
        items: list[StoredItem] = self.store.items_for_digest(digest_id)

        channel = build_channel(config.delivery)
        flavour = "slack" if channel.kind in SLACK_FLAVOUR_KINDS else "markdown"
        labels = {source.name: source.display_name for source in config.sources}
        rendered = render_digest(
            items,
            collection.outcomes,
            title=config.bot.title,
            digest_date=today,
            timezone_name=config.bot.timezone,
            flavour=flavour,
            labels=labels,
            pending_overflow=overflow,
        )
        result.digest = rendered
        self.store.update_digest_body(
            digest_id, body=rendered.body, item_count=rendered.item_count, title=rendered.subject
        )

        if rendered.is_empty and not config.bot.post_when_empty:
            reason = "nothing new and bot.post_when_empty = false"
            log.warning(
                "not posting: %s. A quiet channel will look identical to a broken bot.",
                reason,
                extra={"event": "digest.empty_skipped", "run_id": run_id},
            )
            self.store.mark_digest_skipped(digest_id, reason)
            self.store.release_items(digest_id)
            result.status = "partial" if collection.failed_sources else "ok"
            result.skipped_reason = reason
            self.store.finish_run(
                run_id,
                status=result.status,
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                digest_id=digest_id,
                sent=False,
                notes=reason,
            )
            return result

        if dry_run:
            reason = "dry run: nothing was delivered and no item was marked sent"
            log.warning("%s", reason, extra={"event": "digest.dry_run", "run_id": run_id})
            self.store.release_items(digest_id)
            self.store.mark_digest_skipped(digest_id, reason)
            result.status = "partial" if collection.failed_sources else "ok"
            result.skipped_reason = reason
            self.store.finish_run(
                run_id,
                status=result.status,
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                digest_id=digest_id,
                sent=False,
                notes=reason,
            )
            return result

        delivered = self._deliver(channel, rendered, digest_id=digest_id, run_id=run_id)
        result.sent = delivered is not None
        result.receipt = delivered

        if result.sent:
            result.status = "partial" if collection.failed_sources else "ok"
            self.store.finish_run(
                run_id,
                status=result.status,
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                digest_id=digest_id,
                sent=True,
                notes=f"delivered {rendered.item_count} item(s) to {channel.describe()}",
            )
        else:
            row = self.store.get_digest(digest_id)
            result.status = "failed"
            result.delivery_error = row["last_error"] if row else "delivery failed"
            self.store.finish_run(
                run_id,
                status="failed",
                items_fetched=collection.items_fetched,
                items_new=collection.items_new,
                digest_id=digest_id,
                sent=False,
                error=result.delivery_error,
            )
        return result

    # ------------------------------------------------------------------
    # delivery
    # ------------------------------------------------------------------

    def _deliver(
        self, channel: Channel, rendered: RenderedDigest, *, digest_id: int, run_id: int
    ) -> dict[str, Any] | None:
        try:
            receipt = channel.send(rendered.subject, rendered.body)
        except DeliveryError as exc:
            self._handle_delivery_failure(
                channel, rendered, exc, digest_id=digest_id, run_id=run_id, retryable=exc.retryable
            )
            return None
        except Exception as exc:  # a bug in a channel must not look like success
            self._handle_delivery_failure(
                channel,
                rendered,
                exc,
                digest_id=digest_id,
                run_id=run_id,
                retryable=True,
                unexpected=True,
            )
            return None

        self.store.mark_digest_sent(digest_id)
        log.info(
            "digest %d delivered to %s: %d item(s), %d collection problem(s)",
            digest_id,
            channel.describe(),
            rendered.item_count,
            rendered.problem_count,
            extra={"event": "digest.sent", "run_id": run_id, "digest_id": digest_id},
        )
        return receipt

    def _handle_delivery_failure(
        self,
        channel: Channel,
        rendered: RenderedDigest,
        exc: Exception,
        *,
        digest_id: int,
        run_id: int,
        retryable: bool,
        unexpected: bool = False,
    ) -> None:
        message = f"{type(exc).__name__}: {exc}" if unexpected else str(exc)
        self.store.mark_digest_failed(digest_id, message)
        # Put the items back so the next run can try again with them intact.
        released = self.store.release_items(digest_id)
        log.critical(
            "DIGEST NOT DELIVERED to %s: %s (retryable=%s). "
            "%d item(s) returned to the pending queue; digest %d marked failed.",
            channel.describe(),
            message,
            retryable,
            released,
            digest_id,
            exc_info=unexpected,
            extra={"event": "digest.delivery_failed", "run_id": run_id, "digest_id": digest_id},
        )
        self._raise_alarm(rendered, message, channel=channel, run_id=run_id)

    def _raise_alarm(
        self, rendered: RenderedDigest, message: str, *, channel: Channel, run_id: int
    ) -> None:
        """Try the alert channel. If that fails too, say so at CRITICAL."""
        alerts = self.config.alerts
        if not alerts.enabled:
            log.warning(
                "no alert channel configured, so this failure only exists in %s. "
                "Set [alerts] in the config to get paged.",
                self.config.paths.log_file,
                extra={"event": "alert.not_configured", "run_id": run_id},
            )
            return
        try:
            alert_channel = build_channel(alerts)
            body = (
                f"The daily digest could not be delivered to {channel.describe()}.\n\n"
                f"Error: {message}\n"
                f"Digest: {rendered.subject} ({rendered.item_count} item(s))\n"
                f"Time: {iso(utcnow())}\n"
                f"Log: {self.config.paths.log_file}"
            )
            alert_channel.send(f"[daily-digest-bot] delivery FAILED - {rendered.subject}", body)
            log.warning(
                "delivery failure escalated to the alert channel %s",
                alert_channel.describe(),
                extra={"event": "alert.sent", "run_id": run_id},
            )
        except Exception as exc:
            log.critical(
                "could not reach the alert channel either (%s: %s). "
                "This failure is only recorded in %s and the local database.",
                type(exc).__name__,
                exc,
                self.config.paths.log_file,
                extra={"event": "alert.failed", "run_id": run_id},
            )
