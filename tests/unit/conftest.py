"""The tests' fixtures and plugins."""

from __future__ import annotations

import contextlib
import io
import logging
from collections.abc import AsyncIterator, Iterator

import nats
import pytest
from helpers import CALLOUTS, FakeNATS
from nats.js.client import JetStreamContext
from nats.js.errors import NotFoundError
from natsserver import NATSServer

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


@pytest.fixture(scope="session")
def nats_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NATSServer]:
    """A real nats-server -js, shared by the tests."""
    server = NATSServer(tmp_path_factory.mktemp("jetstream"))
    server.start()
    yield server
    server.stop()


@pytest.fixture
async def js(nats_server: NATSServer) -> AsyncIterator[JetStreamContext]:
    """A JetStream context on the server, with the CALLOUTS stream made afresh as
    the catalogue defines it (paddock#73)."""
    nc = await nats.connect(nats_server.url)
    js = nc.jetstream()
    with contextlib.suppress(NotFoundError):
        await js.delete_stream(CALLOUTS.name or "")
    await js.add_stream(CALLOUTS)
    yield js
    await nc.close()
