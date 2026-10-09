from __future__ import annotations

import logging
from pathlib import Path

import pytest

from digestbot.config import Config
from digestbot.logging_setup import setup_logging
from digestbot.store import Store
from tests.factories import make_config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def store(config: Config):
    store = Store(config.paths.database)
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
