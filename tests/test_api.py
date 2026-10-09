from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from digestbot.models import Item, SourceOutcome
from digestbot.store import Store

CONFIG_TOML = """
[bot]
timezone = "UTC"
send_after = "00:00"
title = "Test Digest"

[paths]
database = "{database}"
log_file = "{log_file}"

[delivery]
kind = "file"
channel = "#test"
path = "{out}"

[[sources]]
name = "feed"
kind = "rss"
url = "https://rss.example.com/feed.xml"
label = "Example feed"

[[sources]]
name = "api"
kind = "api"
url = "https://api.example.com/releases"
items_path = "results"
fields = {{ title = "name", url = "html_url" }}
headers = {{ Authorization = "Bearer hunter2" }}
"""


def _posix(path: Path) -> str:
    # TOML strings take forward slashes on every platform.
    return str(path).replace("\\", "/")


@pytest.fixture
def api(tmp_path: Path):
    from digestbot.api import create_app

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML.format(
            database=_posix(tmp_path / "digest.db"),
            log_file=_posix(tmp_path / "digest-bot.log"),
            out=_posix(tmp_path / "out"),
        ),
        encoding="utf-8",
    )

    store = Store(tmp_path / "digest.db")
    # A delivered digest with two items, plus a failing source and an error.
    digest_id = store.create_digest(digest_date="2025-10-08", channel="#test", title="Test Digest")
    store.update_digest_body(digest_id, body="### feed\n- [A post](https://example.com/1)", item_count=2, title="Test Digest - 2025-10-08")
    for index in range(2):
        one = Item(source="feed", title=f"post {index}", url=f"https://example.com/{index}")
        store.add_item(one, one.fingerprint(["url"]))
    store.attach_items_to_digest([item.id for item in store.pending_items()], digest_id)
    store.mark_digest_sent(digest_id)

    pending = Item(source="feed", title="still waiting", url="https://example.com/pending")
    store.add_item(pending, pending.fingerprint(["url"]))

    run_id = store.start_run("cron")
    store.record_source_result(run_id, SourceOutcome(name="feed", kind="rss", status="ok", items_fetched=2, items_new=2))
    store.record_source_result(
        run_id,
        SourceOutcome(name="api", kind="api", status="error", error="HTTP 500", error_type="http_500"),
    )
    store.finish_run(run_id, status="partial", items_fetched=2, items_new=2, digest_id=digest_id, sent=True)
    store.record_event(level="ERROR", message="source 'api' failed", source="api", event="collect.failed")
    store.record_event(level="INFO", message="run started", event="run.start")
    store.close()

    (tmp_path / "digest-bot.log").write_text(
        "2025-10-08 09:00:00 ERROR digestbot source 'api' failed\n", encoding="utf-8"
    )

    with TestClient(create_app(str(config_path))) as client:
        yield client, tmp_path


def test_health_endpoint(api) -> None:
    client, _ = api
    body = client.get("/api/health").json()

    assert body["status"] in {"ok", "degraded", "down"}
    assert "checks" in body and body["checks"]
    assert body["config"]["channel"] == "#test"
    assert body["stats"]["items_total"] == 3
    assert body["version"]


def test_sources_endpoint_reports_the_broken_source(api) -> None:
    client, _ = api
    sources = {source["source"]: source for source in client.get("/api/sources").json()["sources"]}

    assert sources["feed"]["status"] == "ok"
    assert sources["api"]["status"] == "degraded"  # one failure, not yet the threshold
    assert sources["api"]["last_error"] == "HTTP 500"
    assert sources["feed"]["label"] == "Example feed"


def test_digests_list_and_detail(api) -> None:
    client, _ = api
    listing = client.get("/api/digests").json()["digests"]
    assert len(listing) == 1
    assert listing[0]["status"] == "sent"

    detail = client.get(f"/api/digests/{listing[0]['id']}").json()
    assert detail["digest"]["item_count"] == 2
    assert len(detail["items"]) == 2
    assert detail["items"][0]["url"].startswith("https://example.com/")


def test_unknown_digest_is_404(api) -> None:
    client, _ = api
    assert client.get("/api/digests/9999").status_code == 404


def test_runs_endpoint_includes_source_results(api) -> None:
    client, _ = api
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "partial"
    statuses = {source["source"]: source["status"] for source in runs[0]["sources"]}
    assert statuses == {"feed": "ok", "api": "error"}


def test_events_endpoint_filters_by_level(api) -> None:
    client, _ = api
    assert len(client.get("/api/events").json()["events"]) == 2
    errors = client.get("/api/events", params={"level": "ERROR"}).json()["events"]
    assert len(errors) == 1
    assert errors[0]["source"] == "api"


def test_pending_endpoint(api) -> None:
    client, _ = api
    body = client.get("/api/pending").json()
    assert body["count"] == 1
    assert body["items"][0]["title"] == "still waiting"


def test_config_endpoint_redacts_secrets(api) -> None:
    client, _ = api
    body = client.get("/api/config").json()
    assert "hunter2" not in json.dumps(body)
    api_source = next(s for s in body["config"]["sources"] if s["name"] == "api")
    assert api_source["headers"]["Authorization"] == "***"


def test_logfile_endpoint_tails_the_real_log(api) -> None:
    client, _ = api
    text = client.get("/api/logfile").text
    assert "source 'api' failed" in text


def test_root_explains_how_to_build_the_dashboard(api) -> None:
    client, _ = api
    response = client.get("/")
    assert response.status_code == 200
    # Either the built dashboard or the hint; both are valid.
    assert "daily-digest-bot" in response.text or "<!doctype html>" in response.text.lower()


def test_api_is_read_only(api) -> None:
    client, _ = api
    for method, path in [
        ("post", "/api/digests"),
        ("delete", "/api/events"),
        ("post", "/api/runs"),
        ("put", "/api/config"),
    ]:
        assert getattr(client, method)(path).status_code in {404, 405}


def test_bad_config_surfaces_as_a_500(tmp_path: Path) -> None:
    from digestbot.api import create_app

    broken = tmp_path / "broken.toml"
    broken.write_text('[delivery]\nkind = "slack"\n', encoding="utf-8")  # no webhook_url
    with TestClient(create_app(str(broken)), raise_server_exceptions=False) as client:
        response = client.get("/api/health")
        assert response.status_code == 500
        assert "configuration error" in response.json()["detail"]
