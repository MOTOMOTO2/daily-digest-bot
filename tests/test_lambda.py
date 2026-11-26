"""The Lambda handler, end to end against simulated AWS (moto) and mocked sources."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("moto")

import boto3  # noqa: E402
import respx  # noqa: E402
from moto import mock_aws  # noqa: E402

from digestbot import aws_lambda  # noqa: E402
from digestbot.dynamo import DynamoStore  # noqa: E402
from tests.test_cli import CONFIG_TOML, _posix  # noqa: E402
from tests.test_pipeline import mock_all_sources_ok  # noqa: E402

TABLE = "digest-lambda-test"


@pytest.fixture
def lambda_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML.format(
            database=f"dynamodb://{TABLE}",
            log_file="",
            out=_posix(tmp_path / "out"),
            delivery_kind="file",
            webhook="",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DIGEST_CONFIG", str(config_path))
    monkeypatch.setattr(aws_lambda, "_secrets_loaded", False)
    with mock_aws():
        DynamoStore.create_table(TABLE)
        yield tmp_path


@respx.mock
def test_scheduled_run_collects_and_delivers(lambda_env: Path) -> None:
    mock_all_sources_ok()
    summary = aws_lambda.handler({"mode": "run"})
    assert summary["status"] == "ok" and summary["sent"] is True
    assert summary["items_new"] == 6 and summary["items_in_digest"] == 6
    assert len(list((lambda_env / "out").glob("*.md"))) == 1

    # The next hourly run the same day collects but does not post again.
    again = aws_lambda.handler({"mode": "run"})
    assert again["sent"] is False and "already sent" in again["skipped_reason"]

    # The record lives in DynamoDB.
    stats = DynamoStore(TABLE).stats()
    assert stats["digests_sent"] == 1 and stats["items_sent"] == 6 and stats["runs_total"] == 2


@respx.mock
def test_healthcheck_after_a_good_run(lambda_env: Path) -> None:
    mock_all_sources_ok()
    aws_lambda.handler({"mode": "run"})
    report = aws_lambda.handler({"mode": "healthcheck"})
    assert report["status"] == "ok"


def test_a_failed_digest_fails_the_healthcheck_invocation(lambda_env: Path) -> None:
    store = DynamoStore(TABLE)
    digest_id = store.create_digest(digest_date="2026-11-26", channel="#test", title="T")
    store.mark_digest_failed(digest_id, "webhook returned HTTP 500")
    # A failed invocation is what the CloudWatch alarm counts.
    with pytest.raises(RuntimeError, match="DOWN"):
        aws_lambda.handler({"mode": "healthcheck"})


def test_a_bot_that_has_not_run_yet_is_not_an_outage(lambda_env: Path) -> None:
    # Right after a deploy there is no digest yet; that must not page anyone.
    report = aws_lambda.handler({"mode": "healthcheck"})
    assert report["status"] != "down"


def test_secrets_come_from_ssm_parameters(lambda_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boto3.client("ssm").put_parameter(
        Name="/daily-digest-bot/webhook-url", Value="https://hooks.example.com/secret", Type="SecureString"
    )
    monkeypatch.setenv("SSM_DIGEST_WEBHOOK_URL", "/daily-digest-bot/webhook-url")
    monkeypatch.delenv("DIGEST_WEBHOOK_URL", raising=False)
    aws_lambda.load_secrets()
    import os

    assert os.environ["DIGEST_WEBHOOK_URL"] == "https://hooks.example.com/secret"


def test_a_missing_ssm_parameter_is_a_clear_error(lambda_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSM_DIGEST_WEBHOOK_URL", "/daily-digest-bot/nope")
    with pytest.raises(RuntimeError, match="/daily-digest-bot/nope"):
        aws_lambda.load_secrets()


def test_an_unknown_mode_is_refused(lambda_env: Path) -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        aws_lambda.handler({"mode": "dance"})
