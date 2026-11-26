from __future__ import annotations

import logging
from pathlib import Path

import pytest

from digestbot.config import Config
from digestbot.logging_setup import setup_logging
from digestbot.store import open_store
from tests.factories import make_config

TEST_TABLE = "digest-test"


@pytest.fixture(params=["sqlite", "dynamodb"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Every test that takes ``config`` or ``store`` runs twice: once on the
    SQLite file and once on DynamoDB (simulated by moto, so no AWS account or
    network is needed). Yields the ``[paths] database`` value to use."""
    if request.param == "sqlite":
        yield None
        return
    if request.node.get_closest_marker("sqlite_only"):
        pytest.skip("reads or edits the SQLite file directly")
    moto = pytest.importorskip("moto", reason="pip install -e '.[aws,dev]' to test the DynamoDB store")
    from digestbot.dynamo import DynamoStore

    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DIGEST_DYNAMODB_ENDPOINT", raising=False)
    with moto.mock_aws():
        DynamoStore.create_table(TEST_TABLE)
        yield f"dynamodb://{TEST_TABLE}"


@pytest.fixture
def config(tmp_path: Path, backend: str | None) -> Config:
    if backend:
        return make_config(tmp_path, paths={"database": backend})
    return make_config(tmp_path)


@pytest.fixture
def store(config: Config):
    store = open_store(config.paths.database)
    yield store
    store.close()


@pytest.fixture(autouse=True)
def _quiet_logging(tmp_path: Path):
    """Keep test logs in tmp_path and off the console."""
    logger = setup_logging(log_file=tmp_path / "test.log", level="DEBUG", console=False)
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logging.getLogger("digestbot").handlers.clear()
