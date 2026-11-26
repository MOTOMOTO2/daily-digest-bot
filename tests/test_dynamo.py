"""DynamoDB-specific guarantees.

The shared tests in test_store.py, test_pipeline.py and test_health.py
already run against DynamoDB (see the ``backend`` fixture in conftest.py).
These cover what only exists on this side: the conditional transactions that
stand in for UNIQUE constraints, the sparse pending index, TTL and the
cascade in ``prune``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("moto")

from boto3.dynamodb.conditions import Key  # noqa: E402
from moto import mock_aws  # noqa: E402

from digestbot.dynamo import INDEX, PENDING, DynamoStore  # noqa: E402
from digestbot.models import Item, SourceOutcome, utcnow  # noqa: E402
from digestbot.store import StoreConflict, open_store  # noqa: E402

TABLE = "digest-dynamo-test"


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        DynamoStore.create_table(TABLE)
        yield DynamoStore(TABLE)


def item(title: str, url: str, **kwargs) -> Item:
    return Item(source=kwargs.pop("source", "feed"), title=title, url=url, **kwargs)


def index_keys(store: DynamoStore, pk: str) -> list[str]:
    response = store.table.query(IndexName=INDEX, KeyConditionExpression=Key("gsi1pk").eq(pk))
    return [row["pk"] for row in response["Items"]]


def test_open_store_picks_dynamodb_from_the_url(dynamo: DynamoStore) -> None:
    store = open_store(f"dynamodb://{TABLE}")
    assert isinstance(store, DynamoStore)
    assert store.stats()["database"] == f"dynamodb://{TABLE}"


def test_racing_writers_record_an_item_once(dynamo: DynamoStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two bots both pass the cheap read check, then both try to write. The
    conditional transaction on the fingerprint marker lets exactly one in."""
    first, second = DynamoStore(TABLE), DynamoStore(TABLE)
    one = item("A post", "https://example.com/a")
    fingerprint = one.fingerprint(["url"])
    monkeypatch.setattr(second, "seen_fingerprints", lambda fingerprints: set())

    assert first.add_item(one, fingerprint) is not None
    assert second.add_item(one, fingerprint) is None
    assert first.total_items() == 1
    assert len(index_keys(first, PENDING)) == 1


def test_racing_runs_claim_a_day_once(dynamo: DynamoStore) -> None:
    first, second = DynamoStore(TABLE), DynamoStore(TABLE)
    winner = first.create_digest(digest_date="2026-11-26", channel="#c", title="T")
    with pytest.raises(StoreConflict):
        second.create_digest(digest_date="2026-11-26", channel="#c", title="T")
    assert first.get_digest_for_date("2026-11-26")["id"] == winner
    assert [row["id"] for row in first.recent_digests()] == [winner]


def test_ids_are_allocated_atomically_and_increase(dynamo: DynamoStore) -> None:
    ids = [dynamo.start_run("test") for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5


def test_the_pending_queue_is_a_sparse_index(dynamo: DynamoStore) -> None:
    old = item("old", "https://example.com/old", published_at=utcnow() - timedelta(days=10))
    new = item("new", "https://example.com/new", published_at=utcnow())
    old_id = dynamo.add_item(old, old.fingerprint(["url"]))
    new_id = dynamo.add_item(new, new.fingerprint(["url"]))
    assert sorted(index_keys(dynamo, PENDING)) == sorted([f"ITEM#{old_id}", f"ITEM#{new_id}"])

    # Expired: out of every list, but the fingerprint is still on record.
    assert dynamo.expire_stale_pending(utcnow() - timedelta(days=1)) == 1
    assert index_keys(dynamo, PENDING) == [f"ITEM#{new_id}"]
    assert dynamo.seen_fingerprints([old.fingerprint(["url"])])

    # Attached: moves from the queue to the digest's own list.
    digest_id = dynamo.create_digest(digest_date="2026-11-26", channel="#c", title="T")
    assert dynamo.attach_items_to_digest([new_id], digest_id) == 1
    assert index_keys(dynamo, PENDING) == []
    assert index_keys(dynamo, f"DIGEST#{digest_id}#ITEMS") == [f"ITEM#{new_id}"]
    # Attaching twice is a no-op, like the SQL `AND digest_id IS NULL`.
    assert dynamo.attach_items_to_digest([new_id], digest_id) == 0

    # Released after a failed send: back in the queue.
    assert dynamo.release_items(digest_id) == 1
    assert index_keys(dynamo, PENDING) == [f"ITEM#{new_id}"]


def test_retention_sets_ttl_on_history_but_never_on_items(monkeypatch: pytest.MonkeyPatch, dynamo: DynamoStore) -> None:
    store = DynamoStore(TABLE, retention_days=90)
    run_id = store.start_run("test")
    store.record_source_result(run_id, SourceOutcome(name="feed", kind="rss", status="ok"))
    store.record_event(level="ERROR", message="boom")
    one = item("A post", "https://example.com/a")
    item_id = store.add_item(one, one.fingerprint(["url"]))

    def record(pk: str, sk: str) -> dict:
        return store.table.get_item(Key={"pk": pk, "sk": sk})["Item"]

    assert "expires_at" in record(f"RUN#{run_id}", "RUN")
    assert "expires_at" in record("EVENT#1", "EVENT")
    # Items and their fingerprints are the deduplication memory: kept forever.
    assert "expires_at" not in record(f"ITEM#{item_id}", "ITEM")

    ttl = store.client.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "expires_at"


def test_prune_cascades_from_a_run_to_its_source_results(dynamo: DynamoStore) -> None:
    run_id = dynamo.start_run("test")
    dynamo.record_source_result(run_id, SourceOutcome(name="feed", kind="rss", status="ok"))
    dynamo.finish_run(run_id, status="ok")
    dynamo.record_event(level="ERROR", message="old")
    one = item("A post", "https://example.com/a", raw={"big": "payload"})
    item_id = dynamo.add_item(one, one.fingerprint(["url"]))

    # Age everything past the cutoff.
    long_ago = "2020-01-01T00:00:00+00:00"
    dynamo.table.update_item(
        Key={"pk": f"RUN#{run_id}", "sk": "RUN"},
        UpdateExpression="SET started_at = :t, finished_at = :t, gsi1sk = :k",
        ExpressionAttributeValues={":t": long_ago, ":k": f"{long_ago}#{run_id:012d}"},
    )
    dynamo.table.update_item(
        Key={"pk": "EVENT#1", "sk": "EVENT"},
        UpdateExpression="SET ts = :t, gsi1sk = :k",
        ExpressionAttributeValues={":t": long_ago, ":k": f"{long_ago}#{1:012d}"},
    )
    dynamo.table.update_item(
        Key={"pk": f"ITEM#{item_id}", "sk": "ITEM"},
        UpdateExpression="SET first_seen_at = :t",
        ExpressionAttributeValues={":t": long_ago},
    )

    result = dynamo.prune(days=30)
    assert result == {"raw_payloads_cleared": 1, "events_deleted": 1, "runs_deleted": 1}
    assert dynamo.source_results_for_run(run_id) == []
    assert dynamo.recent_runs() == []
    # The fingerprint survives, so deduplication memory is intact.
    assert dynamo.seen_fingerprints([one.fingerprint(["url"])])
    assert dynamo.total_items() == 1


def test_source_totals_are_running_counters(dynamo: DynamoStore) -> None:
    run_id = dynamo.start_run("test")
    for status, new in (("ok", 3), ("error", 0), ("ok", 2)):
        dynamo.record_source_result(
            run_id, SourceOutcome(name="feed", kind="rss", status=status, items_new=new, error="x" if status == "error" else None)
        )
    stats = dynamo.source_stats("feed")
    assert (stats["checks"], stats["successful_checks"], stats["items_new_total"]) == (3, 2, 5)
    assert stats["consecutive_failures"] == 0
    assert stats["last_status"] == "ok" and stats["last_success_at"] is not None


def test_a_missing_table_name_is_a_clear_error(dynamo: DynamoStore) -> None:
    from digestbot.store import StoreError

    with pytest.raises(StoreError, match="table name"):
        open_store("dynamodb://")


@pytest.mark.parametrize("typo", ["dynamodb:/digest", "dynamodb:digest", "DynamoDB://digest"])
def test_a_mistyped_dynamodb_url_is_refused_not_turned_into_a_file(typo: str, tmp_path, monkeypatch) -> None:
    from digestbot.store import StoreError

    monkeypatch.chdir(tmp_path)
    with pytest.raises(StoreError, match="dynamodb://"):
        open_store(typo)
    assert not [path for path in tmp_path.iterdir() if path.name.lower().startswith("dynamodb")]
