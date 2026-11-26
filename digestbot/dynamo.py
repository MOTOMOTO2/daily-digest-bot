"""The same record as store.py, kept in Amazon DynamoDB.

Selected with ``database = "dynamodb://<table>"``. It is a drop-in for
:class:`digestbot.store.Store`: every method the pipeline, the health check
and the dashboard call exists here with the same behaviour, and the shared
tests run against both.

One table, single-table design. DynamoDB has no UNIQUE constraints, no
autoincrement and no secondary indexes over arbitrary columns, so each SQLite
guarantee is rebuilt from DynamoDB's own primitives:

==========================  =================================================
SQLite                      DynamoDB
==========================  =================================================
``items.fingerprint``       A marker record ``FP#<fingerprint>`` written in the
UNIQUE                      same transaction as the item, on the condition that
                            it does not exist yet. Two bots racing to record
                            the same item: one transaction wins, one cancels.
``digests.digest_date``     The same pattern: ``DAY#<date>`` is claimed in a
UNIQUE                      transaction with the digest, which is what keeps
                            "one digest a day" true under concurrent runs.
``INTEGER PRIMARY KEY       An atomic ``ADD`` on a counter record per kind.
AUTOINCREMENT``
indexes for the queue,      One generic index, ``gsi1``. Each record type puts
runs, events, per-source    its list key in ``gsi1pk`` and its sort order in
history                     ``gsi1sk``. A pending item sits under ``PENDING``;
                            attaching it to a digest moves it under
                            ``DIGEST#<id>#ITEMS``; expiring it removes the key,
                            so the queue is a sparse index of exactly the
                            pending items.
``COUNT(*)``, ``SUM()``     Running totals kept with atomic ``ADD`` updates.
``ON DELETE CASCADE``,      A run's source results share its partition key, and
``prune``                   runs, source results and events carry an
                            ``expires_at`` attribute for DynamoDB's TTL.
==========================  =================================================

Rows come back as plain dicts with every column present (``None`` where
SQLite would have NULL), so ``row["x"]``, ``dict(row)`` and
``StoredItem.from_row`` work unchanged.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

import boto3
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from botocore.exceptions import ClientError

from .models import Item, SourceOutcome, StoredItem, iso, utcnow
from .store import StoreConflict, StoreError

INDEX = "gsi1"
PENDING = "PENDING"

# A single item may be at most 400 KB; raw payloads are only kept for
# debugging, so a large one is dropped rather than failing the write.
MAX_RAW_CHARS = 50_000

ITEM_COLUMNS = (
    "id", "fingerprint", "title_key", "source", "title", "url", "canonical_url", "summary",
    "author", "published_at", "first_seen_at", "raw", "digest_id", "sent_at", "expired_at",
)
DIGEST_COLUMNS = (
    "id", "digest_date", "status", "item_count", "channel", "title", "body", "created_at",
    "sent_at", "attempts", "last_error",
)
RUN_COLUMNS = (
    "id", "started_at", "finished_at", "status", "trigger", "items_fetched", "items_new",
    "digest_id", "sent", "error", "notes",
)
SOURCE_RESULT_COLUMNS = (
    "id", "run_id", "source", "kind", "status", "items_fetched", "items_new", "duration_ms",
    "error", "error_type", "created_at",
)
EVENT_COLUMNS = ("id", "ts", "level", "source", "event", "message", "detail", "run_id")


def _plain(value: Any) -> Any:
    """DynamoDB numbers arrive as Decimal; the rest of the bot expects ints."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _row(record: dict[str, Any] | None, columns: Sequence[str]) -> dict[str, Any] | None:
    if record is None:
        return None
    return {column: _plain(record.get(column)) for column in columns}


def _order(timestamp: str, ident: int) -> str:
    """Sort key that orders by time, then id, like ``ORDER BY ts DESC, id DESC``."""
    return f"{timestamp}#{ident:012d}"


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """DynamoDB has no NULL column: an absent attribute is the NULL."""
    return {key: value for key, value in record.items() if value is not None}


class DynamoStore:
    """Data-access layer over one DynamoDB table, matching :class:`Store`."""

    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        retention_days: int | None = None,
    ) -> None:
        if not table_name:
            raise StoreError("dynamodb:// needs a table name, e.g. dynamodb://daily-digest-bot")
        resource = boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint_url)
        self.table_name = table_name
        self.table = resource.Table(table_name)
        self.client = resource.meta.client
        self.retention_days = retention_days
        self.path = f"dynamodb://{table_name}"

    @classmethod
    def from_url(cls, url: str) -> DynamoStore:
        """``dynamodb://<table>``; region, endpoint and retention from the environment."""
        retention = os.environ.get("DIGEST_RETENTION_DAYS")
        return cls(
            url.removeprefix("dynamodb://").strip("/"),
            region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            endpoint_url=os.environ.get("DIGEST_DYNAMODB_ENDPOINT") or None,
            retention_days=int(retention) if retention else None,
        )

    @staticmethod
    def create_table(
        table_name: str, *, region: str | None = None, endpoint_url: str | None = None
    ) -> None:
        """Create the table and its index. In AWS the CloudFormation template does this."""
        client = boto3.client("dynamodb", region_name=region, endpoint_url=endpoint_url)
        strings = [{"AttributeName": name, "AttributeType": "S"} for name in ("pk", "sk", "gsi1pk", "gsi1sk")]
        client.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=strings,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": INDEX,
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        client.get_waiter("table_exists").wait(TableName=table_name)
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def migrate(self) -> None:
        """Nothing to migrate: the schema lives in the items themselves."""

    def close(self) -> None:
        """Nothing to close: boto3 clients hold no open connection to release."""

    def __enter__(self) -> DynamoStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[DynamoStore]:
        """Each write here is atomic on its own, and the invariants that span
        records are enforced by conditional transactions inside the methods,
        so there is no session-wide transaction to open."""
        yield self

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _expires_at(self) -> int | None:
        if not self.retention_days:
            return None
        return int(time.time()) + self.retention_days * 86_400

    def _next_id(self, kind: str) -> int:
        response = self.table.update_item(
            Key={"pk": "COUNTER", "sk": kind},
            UpdateExpression="ADD #value :one",
            ExpressionAttributeNames={"#value": "value"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["value"])

    def _add(self, pk: str, sk: str, **deltas: int) -> None:
        deltas = {name: amount for name, amount in deltas.items() if amount}
        if not deltas:
            return
        self.table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="ADD " + ", ".join(f"#{name} :{name}" for name in deltas),
            ExpressionAttributeNames={f"#{name}": name for name in deltas},
            ExpressionAttributeValues={f":{name}": amount for name, amount in deltas.items()},
        )

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")

    def _query(
        self,
        key: ConditionBase,
        *,
        index: str | None = INDEX,
        descending: bool = False,
        limit: int | None = None,
        filter: ConditionBase | None = None,
    ) -> list[dict[str, Any]]:
        """Query with pagination. ``limit`` counts rows after the filter, which
        DynamoDB's own Limit does not, so pages are read until it is met."""
        kwargs: dict[str, Any] = {"KeyConditionExpression": key, "ScanIndexForward": not descending}
        if index:
            kwargs["IndexName"] = index
        if filter is not None:
            kwargs["FilterExpression"] = filter
        if limit is not None and filter is None:
            kwargs["Limit"] = limit
        rows: list[dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            rows.extend(response.get("Items", []))
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if "LastEvaluatedKey" not in response:
                return rows
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _count(self, key: ConditionBase, *, filter: ConditionBase | None = None) -> int:
        kwargs: dict[str, Any] = {"IndexName": INDEX, "KeyConditionExpression": key, "Select": "COUNT"}
        if filter is not None:
            kwargs["FilterExpression"] = filter
        total = 0
        while True:
            response = self.table.query(**kwargs)
            total += int(response.get("Count", 0))
            if "LastEvaluatedKey" not in response:
                return total
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _update_if(self, key: dict[str, Any], condition: ConditionBase, **update: Any) -> bool:
        """An update guarded by a condition; False if the condition did not hold."""
        try:
            self.table.update_item(Key=key, ConditionExpression=condition, **update)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def _put(self, record: dict[str, Any], *, unique: bool = False) -> dict[str, Any]:
        """One Put inside a TransactWriteItems call. The resource's client
        converts plain Python values to DynamoDB's typed form itself."""
        put: dict[str, Any] = {"TableName": self.table_name, "Item": _compact(record)}
        if unique:
            put["ConditionExpression"] = "attribute_not_exists(pk)"
        return {"Put": put}

    def _transact(self, actions: list[dict[str, Any]]) -> bool:
        """Run a transaction; False if one of its conditions failed."""
        try:
            self.client.transact_write_items(TransactItems=actions)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons") or []
                codes = {reason.get("Code") for reason in reasons}
                if not reasons or "ConditionalCheckFailed" in codes:
                    return False
            raise
        return True

    def _batch_exists(self, prefix: str, keys: Iterable[str]) -> set[str]:
        """Which of ``prefix + key`` records exist. BatchGetItem rejects duplicate
        keys and takes 100 at a time."""
        wanted = sorted({key for key in keys if key})
        found: set[str] = set()
        for start in range(0, len(wanted), 100):
            request = {
                self.table_name: {
                    "Keys": [{"pk": f"{prefix}{key}", "sk": prefix.rstrip("#")} for key in wanted[start : start + 100]],
                    "ProjectionExpression": "pk",
                }
            }
            while request:
                response = self.client.batch_get_item(RequestItems=request)
                for record in response.get("Responses", {}).get(self.table_name, []):
                    found.add(record["pk"])
                request = response.get("UnprocessedKeys") or {}
        return {pk.removeprefix(prefix) for pk in found}

    def _item_row(self, record: dict[str, Any]) -> dict[str, Any]:
        return _row(record, ITEM_COLUMNS)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # items / deduplication
    # ------------------------------------------------------------------

    def seen_fingerprints(self, fingerprints: Sequence[str]) -> set[str]:
        return self._batch_exists("FP#", fingerprints)

    def seen_title_keys(self, title_keys: Sequence[str]) -> set[str]:
        return self._batch_exists("TITLE#", title_keys)

    def add_item(self, item: Item, fingerprint: str) -> int | None:
        """Record an item. Returns its id, or None if the fingerprint is known.

        The fingerprint marker and the item are written in one transaction,
        and the marker's ``attribute_not_exists`` condition is the real
        deduplication guarantee, as SQLite's UNIQUE constraint is there.
        """
        if fingerprint in self.seen_fingerprints([fingerprint]):
            return None
        item_id = self._next_id("items")
        now = iso(utcnow())
        published = iso(item.published_at) if item.published_at else None
        raw = json.dumps(item.raw, default=str) if item.raw else None
        record = {
            "pk": f"ITEM#{item_id}",
            "sk": "ITEM",
            "type": "item",
            "id": item_id,
            "fingerprint": fingerprint,
            "title_key": item.title_key or None,
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "canonical_url": item.canonical or None,
            "summary": item.summary,
            "author": item.author,
            "published_at": published,
            "first_seen_at": now,
            "raw": raw if raw and len(raw) <= MAX_RAW_CHARS else None,
            "gsi1pk": PENDING,
            "gsi1sk": _order(published or now, item_id),
        }
        actions = [
            self._put({"pk": f"FP#{fingerprint}", "sk": "FP", "item_id": item_id}, unique=True),
            self._put(record, unique=True),
        ]
        if item.title_key:
            actions.append(self._put({"pk": f"TITLE#{item.title_key}", "sk": "TITLE", "item_id": item_id}))
        if not self._transact(actions):
            return None
        self._add("TOTALS", "TOTALS", items_total=1)
        return item_id

    def pending_items(self, *, limit: int | None = None, since: datetime | None = None) -> list[StoredItem]:
        """Collected items that have not been part of any digest yet."""
        recent = None
        if since is not None:
            # Keep an item if it is recent, or if it carries no date at all.
            recent = Attr("published_at").not_exists() | Attr("published_at").gte(iso(since))
        rows = self._query(Key("gsi1pk").eq(PENDING), descending=True, limit=limit, filter=recent)
        return [StoredItem.from_row(self._item_row(row)) for row in rows]

    def count_pending(self) -> int:
        return self._count(Key("gsi1pk").eq(PENDING))

    def expire_stale_pending(self, cutoff: datetime) -> int:
        """Retire pending items published before ``cutoff``; their fingerprints stay."""
        # The sort key starts with the publish time (or first-seen time for
        # undated items), so the key condition narrows to old items and the
        # filter keeps only the ones that actually carry a date.
        stale = self._query(
            Key("gsi1pk").eq(PENDING) & Key("gsi1sk").lt(iso(cutoff)),
            filter=Attr("published_at").exists() & Attr("published_at").lt(iso(cutoff)),
        )
        now = iso(utcnow())
        expired = 0
        for row in stale:
            if self._update_if(
                {"pk": row["pk"], "sk": "ITEM"},
                Attr("gsi1pk").eq(PENDING),
                UpdateExpression="SET expired_at = :now REMOVE gsi1pk, gsi1sk",
                ExpressionAttributeValues={":now": now},
            ):
                expired += 1
        self._add("TOTALS", "TOTALS", items_expired=expired)
        return expired

    def items_for_digest(self, digest_id: int) -> list[StoredItem]:
        rows = self._query(Key("gsi1pk").eq(f"DIGEST#{digest_id}#ITEMS"), descending=True)
        return [StoredItem.from_row(self._item_row(row)) for row in rows]

    def attach_items_to_digest(self, item_ids: Iterable[int], digest_id: int) -> int:
        attached = 0
        for item_id in item_ids:
            if self._update_if(
                {"pk": f"ITEM#{item_id}", "sk": "ITEM"},
                Attr("pk").exists() & Attr("digest_id").not_exists(),
                UpdateExpression="SET digest_id = :digest, gsi1pk = :list",
                ExpressionAttributeValues={":digest": digest_id, ":list": f"DIGEST#{digest_id}#ITEMS"},
            ):
                attached += 1
        return attached

    def mark_items_sent(self, digest_id: int, sent_at: datetime) -> int:
        moment = iso(sent_at)
        sent_by_source: dict[str, int] = {}
        for row in self._query(Key("gsi1pk").eq(f"DIGEST#{digest_id}#ITEMS")):
            if row.get("sent_at"):
                continue
            if self._update_if(
                {"pk": row["pk"], "sk": "ITEM"},
                Attr("sent_at").not_exists(),
                UpdateExpression="SET sent_at = :sent",
                ExpressionAttributeValues={":sent": moment},
            ):
                sent_by_source[row["source"]] = sent_by_source.get(row["source"], 0) + 1
        for source, count in sent_by_source.items():
            self._add(f"SOURCESTATS#{source}", "STATS", items_sent_total=count)
        total = sum(sent_by_source.values())
        self._add("TOTALS", "TOTALS", items_sent=total)
        return total

    def release_items(self, digest_id: int) -> int:
        """Detach items from a digest so a failed digest's items are not lost."""
        released = 0
        for row in self._query(Key("gsi1pk").eq(f"DIGEST#{digest_id}#ITEMS")):
            if row.get("sent_at"):
                continue
            if self._update_if(
                {"pk": row["pk"], "sk": "ITEM"},
                Attr("digest_id").eq(digest_id) & Attr("sent_at").not_exists(),
                UpdateExpression="SET gsi1pk = :pending REMOVE digest_id, sent_at, expired_at",
                ExpressionAttributeValues={":pending": PENDING},
            ):
                released += 1
        return released

    def total_items(self) -> int:
        totals = self._get("TOTALS", "TOTALS") or {}
        return int(totals.get("items_total", 0))

    # ------------------------------------------------------------------
    # digests
    # ------------------------------------------------------------------

    def get_digest_for_date(self, digest_date: str) -> dict[str, Any] | None:
        day = self._get(f"DAY#{digest_date}", "DAY")
        return self.get_digest(int(day["digest_id"])) if day else None

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        return _row(self._get(f"DIGEST#{digest_id}", "DIGEST"), DIGEST_COLUMNS)

    def create_digest(self, *, digest_date: str, channel: str, title: str) -> int:
        """Claim the day's digest slot. Raises StoreConflict if it is taken."""
        digest_id = self._next_id("digests")
        claimed = self._transact(
            [
                self._put({"pk": f"DAY#{digest_date}", "sk": "DAY", "digest_id": digest_id}, unique=True),
                self._put(
                    {
                        "pk": f"DIGEST#{digest_id}",
                        "sk": "DIGEST",
                        "type": "digest",
                        "id": digest_id,
                        "digest_date": digest_date,
                        "status": "pending",
                        "item_count": 0,
                        "channel": channel,
                        "title": title,
                        "created_at": iso(utcnow()),
                        "attempts": 0,
                        "gsi1pk": "DIGESTS",
                        "gsi1sk": digest_date,
                    },
                    unique=True,
                ),
            ]
        )
        if not claimed:
            raise StoreConflict(f"a digest for {digest_date} already exists")
        return digest_id

    def update_digest_body(self, digest_id: int, *, body: str, item_count: int, title: str) -> None:
        self.table.update_item(
            Key={"pk": f"DIGEST#{digest_id}", "sk": "DIGEST"},
            UpdateExpression="SET body = :body, item_count = :count, title = :title",
            ExpressionAttributeValues={":body": body, ":count": item_count, ":title": title},
        )

    def mark_digest_sent(self, digest_id: int, *, sent_at: datetime | None = None) -> None:
        moment = sent_at or utcnow()
        self.table.update_item(
            Key={"pk": f"DIGEST#{digest_id}", "sk": "DIGEST"},
            UpdateExpression="SET #status = :sent, sent_at = :at REMOVE last_error ADD attempts :one",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":sent": "sent", ":at": iso(moment), ":one": 1},
        )
        # Point at the most recently sent digest, so the health check reads one record.
        self._update_if(
            {"pk": "LASTSENT", "sk": "LASTSENT"},
            Attr("sent_at").not_exists() | Attr("sent_at").lte(iso(moment)),
            UpdateExpression="SET digest_id = :digest, sent_at = :at",
            ExpressionAttributeValues={":digest": digest_id, ":at": iso(moment)},
        )
        self.mark_items_sent(digest_id, moment)

    def mark_digest_failed(self, digest_id: int, error: str) -> None:
        self.table.update_item(
            Key={"pk": f"DIGEST#{digest_id}", "sk": "DIGEST"},
            UpdateExpression="SET #status = :failed, last_error = :error ADD attempts :one",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": "failed", ":error": error[:2000], ":one": 1},
        )

    def mark_digest_skipped(self, digest_id: int, reason: str) -> None:
        self.table.update_item(
            Key={"pk": f"DIGEST#{digest_id}", "sk": "DIGEST"},
            UpdateExpression="SET #status = :skipped, last_error = :reason",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":skipped": "skipped", ":reason": reason[:2000]},
        )

    def recent_digests(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._query(Key("gsi1pk").eq("DIGESTS"), descending=True, limit=limit)
        return [_row(row, DIGEST_COLUMNS) for row in rows]  # type: ignore[misc]

    def last_sent_digest(self) -> dict[str, Any] | None:
        pointer = self._get("LASTSENT", "LASTSENT")
        return self.get_digest(int(pointer["digest_id"])) if pointer else None

    def undelivered_digests(self) -> list[dict[str, Any]]:
        rows = self._query(
            Key("gsi1pk").eq("DIGESTS"), filter=Attr("status").is_in(["pending", "failed"])
        )
        return [_row(row, DIGEST_COLUMNS) for row in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    def start_run(self, trigger: str) -> int:
        run_id = self._next_id("runs")
        started = iso(utcnow())
        self.table.put_item(
            Item=_compact(
                {
                    "pk": f"RUN#{run_id}",
                    "sk": "RUN",
                    "type": "run",
                    "id": run_id,
                    "started_at": started,
                    "status": "running",
                    "trigger": trigger,
                    "items_fetched": 0,
                    "items_new": 0,
                    "sent": 0,
                    "gsi1pk": "RUNS",
                    "gsi1sk": _order(started, run_id),
                    "expires_at": self._expires_at(),
                }
            )
        )
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        items_fetched: int = 0,
        items_new: int = 0,
        digest_id: int | None = None,
        sent: bool = False,
        error: str | None = None,
        notes: str | None = None,
    ) -> None:
        values = {
            "finished_at": iso(utcnow()),
            "status": status,
            "items_fetched": items_fetched,
            "items_new": items_new,
            "sent": 1 if sent else 0,
            "digest_id": digest_id,
            "error": error[:2000] if error else None,
            "notes": notes,
        }
        present = {key: value for key, value in values.items() if value is not None}
        absent = [key for key, value in values.items() if value is None]
        expression = "SET " + ", ".join(f"#{key} = :{key}" for key in present)
        if absent:
            expression += " REMOVE " + ", ".join(f"#{key}" for key in absent)
        self.table.update_item(
            Key={"pk": f"RUN#{run_id}", "sk": "RUN"},
            UpdateExpression=expression,
            ExpressionAttributeNames={f"#{key}": key for key in values},
            ExpressionAttributeValues={f":{key}": value for key, value in present.items()},
        )

    def record_source_result(self, run_id: int, outcome: SourceOutcome) -> None:
        result_id = self._next_id("source_results")
        created = iso(utcnow())
        self.table.put_item(
            Item=_compact(
                {
                    # Same partition as the run: one query reads a run's results.
                    "pk": f"RUN#{run_id}",
                    "sk": f"SOURCE#{result_id:012d}",
                    "type": "source_result",
                    "id": result_id,
                    "run_id": run_id,
                    "source": outcome.name,
                    "kind": outcome.kind,
                    "status": outcome.status,
                    "items_fetched": outcome.items_fetched,
                    "items_new": outcome.items_new,
                    "duration_ms": outcome.duration_ms,
                    "error": outcome.error[:2000] if outcome.error else None,
                    "error_type": outcome.error_type,
                    "created_at": created,
                    "gsi1pk": f"SOURCE#{outcome.name}",
                    "gsi1sk": _order(created, result_id),
                    "expires_at": self._expires_at(),
                }
            )
        )
        self._add(
            f"SOURCESTATS#{outcome.name}",
            "STATS",
            checks=1,
            successful_checks=1 if outcome.status == "ok" else 0,
            items_new_total=outcome.items_new,
        )

    def recent_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._query(Key("gsi1pk").eq("RUNS"), descending=True, limit=limit)
        return [_row(row, RUN_COLUMNS) for row in rows]  # type: ignore[misc]

    def last_run(self) -> dict[str, Any] | None:
        rows = self._query(
            Key("gsi1pk").eq("RUNS"), descending=True, limit=1, filter=Attr("status").ne("running")
        )
        return _row(rows[0], RUN_COLUMNS) if rows else None

    def source_results_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._query(
            Key("pk").eq(f"RUN#{run_id}") & Key("sk").begins_with("SOURCE#"), index=None
        )
        results = [_row(row, SOURCE_RESULT_COLUMNS) for row in rows]
        return sorted(results, key=lambda row: row["source"])  # type: ignore[index]

    def consecutive_failures(self, source: str, *, window: int = 20) -> int:
        """How many times in a row this source has failed, most recent first."""
        rows = self._query(Key("gsi1pk").eq(f"SOURCE#{source}"), descending=True, limit=window)
        streak = 0
        for row in rows:
            if row["status"] == "ok":
                break
            streak += 1
        return streak

    def source_stats(self, source: str) -> dict[str, Any]:
        history = Key("gsi1pk").eq(f"SOURCE#{source}")
        latest_rows = self._query(history, descending=True, limit=1)
        latest = _row(latest_rows[0], SOURCE_RESULT_COLUMNS) if latest_rows else None
        ok_rows = self._query(history, descending=True, limit=1, filter=Attr("status").eq("ok"))
        totals = self._get(f"SOURCESTATS#{source}", "STATS") or {}
        return {
            "source": source,
            "last_status": latest["status"] if latest else None,
            "last_checked_at": latest["created_at"] if latest else None,
            "last_error": latest["error"] if latest else None,
            "last_error_type": latest["error_type"] if latest else None,
            "last_duration_ms": latest["duration_ms"] if latest else None,
            "last_success_at": ok_rows[0]["created_at"] if ok_rows else None,
            "consecutive_failures": self.consecutive_failures(source),
            "checks": int(totals.get("checks", 0)),
            "successful_checks": int(totals.get("successful_checks", 0)),
            "items_new_total": int(totals.get("items_new_total", 0)),
            "items_sent_total": int(totals.get("items_sent_total", 0)),
        }

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        level: str,
        message: str,
        source: str | None = None,
        event: str | None = None,
        run_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        event_id = self._next_id("events")
        ts = iso(utcnow())
        self.table.put_item(
            Item=_compact(
                {
                    "pk": f"EVENT#{event_id}",
                    "sk": "EVENT",
                    "type": "event",
                    "id": event_id,
                    "ts": ts,
                    "level": level,
                    "source": source,
                    "event": event,
                    "message": message[:4000],
                    "detail": json.dumps(detail, default=str) if detail else None,
                    "run_id": run_id,
                    "gsi1pk": "EVENTS",
                    "gsi1sk": _order(ts, event_id),
                    "expires_at": self._expires_at(),
                }
            )
        )

    def recent_events(
        self, *, limit: int = 100, levels: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        wanted = Attr("level").is_in([level.upper() for level in levels]) if levels else None
        rows = self._query(Key("gsi1pk").eq("EVENTS"), descending=True, limit=limit, filter=wanted)
        return [_row(row, EVENT_COLUMNS) for row in rows]  # type: ignore[misc]

    def count_events_since(self, since: datetime, *, levels: Sequence[str] = ("ERROR", "CRITICAL")) -> int:
        return self._count(
            Key("gsi1pk").eq("EVENTS") & Key("gsi1sk").gte(iso(since)),
            filter=Attr("level").is_in([level.upper() for level in levels]),
        )

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    def prune(self, *, days: int) -> dict[str, int]:
        """Drop old events, runs and raw payloads; fingerprints are kept forever.

        With ``DIGEST_RETENTION_DAYS`` set, DynamoDB's TTL already deletes
        runs and events on its own; this is for doing it now, or without TTL.
        """
        cutoff = iso(utcnow() - timedelta(days=days))
        events = self._query(Key("gsi1pk").eq("EVENTS") & Key("gsi1sk").lt(cutoff))
        runs = self._query(
            Key("gsi1pk").eq("RUNS") & Key("gsi1sk").lt(cutoff),
            filter=Attr("finished_at").exists() & Attr("finished_at").lt(cutoff),
        )
        doomed = [{"pk": row["pk"], "sk": row["sk"]} for row in events]
        for run in runs:
            # A run's source results share its partition: the CASCADE.
            for row in self._query(Key("pk").eq(run["pk"]), index=None):
                doomed.append({"pk": row["pk"], "sk": row["sk"]})
        with self.table.batch_writer() as batch:
            for key in doomed:
                batch.delete_item(Key=key)

        # Raw payloads have no index of their own; a scan is fine for an
        # occasional maintenance command on a table this size.
        cleared = 0
        scan: dict[str, Any] = {
            "FilterExpression": Attr("type").eq("item") & Attr("raw").exists() & Attr("first_seen_at").lt(cutoff),
            "ProjectionExpression": "pk",
        }
        while True:
            response = self.table.scan(**scan)
            for row in response.get("Items", []):
                self.table.update_item(
                    Key={"pk": row["pk"], "sk": "ITEM"},
                    # RAW is one of DynamoDB's reserved words.
                    UpdateExpression="REMOVE #raw",
                    ExpressionAttributeNames={"#raw": "raw"},
                )
                cleared += 1
            if "LastEvaluatedKey" not in response:
                break
            scan["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return {"raw_payloads_cleared": cleared, "events_deleted": len(events), "runs_deleted": len(runs)}

    def stats(self) -> dict[str, Any]:
        totals = self._get("TOTALS", "TOTALS") or {}
        digests = self._query(Key("gsi1pk").eq("DIGESTS"))
        return {
            "database": self.path,
            "items_total": int(totals.get("items_total", 0)),
            "items_pending": self.count_pending(),
            "items_expired": int(totals.get("items_expired", 0)),
            "items_sent": int(totals.get("items_sent", 0)),
            "digests_total": len(digests),
            "digests_sent": sum(1 for row in digests if row["status"] == "sent"),
            "digests_failed": sum(1 for row in digests if row["status"] == "failed"),
            "runs_total": self._count(Key("gsi1pk").eq("RUNS")),
            "events_total": self._count(Key("gsi1pk").eq("EVENTS")),
        }


__all__ = ["DynamoStore", "INDEX", "PENDING"]
