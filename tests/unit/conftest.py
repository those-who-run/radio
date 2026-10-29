"""The tests' fixtures and plugins."""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from helpers import FakeNATS

from radio import logs

# noskip: a skipped test fails the run. pytester: runs pytest inside a test, for
# noskip's own tests.
pytest_plugins = ["noskip", "pytester"]


@pytest.fixture
async def fake_nats() -> AsyncIterator[FakeNATS]:
    server = FakeNATS()
    await server.start()
    yield server
    await server.close()


@pytest.fixture
def log() -> Iterator[io.StringIO]:
    """The `radio` logger's JSON lines, at debug."""
    out = io.StringIO()
    logs.setup(logging.DEBUG, "json", out)
    yield out
    logging.getLogger("radio").handlers.clear()
