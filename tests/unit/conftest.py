"""The tests' fixtures and plugins."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import nats
import pytest
from helpers import CALLOUTS, FakeNATS, Running, roster
from nats.js.client import JetStreamContext
from nats.js.errors import NotFoundError
from natsserver import NATSServer

from radio import logs
from radio.callouts import Callouts
from radio.engineer import Roster
from radio.sink import Sink
from radio.timing import LIVE, ReplayClock, Timeline

# The source the tests' callouts are published on.
SRC = "test"

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
    server = NATSServer(tmp_path_factory.mktemp("nats"))
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


@pytest.fixture
async def start(nats_server: NATSServer, js: JetStreamContext) -> AsyncIterator[Callable[..., Any]]:
    """Starts a Callouts on the test server, speaking through a sink: stopped at
    the end of the test."""
    running: list[Running] = []

    async def start(
        sink: Sink[str],
        engineers: Roster | None = None,
        src: str = SRC,
        timing: str = LIVE,
        timeline: Timeline | None = None,
        replay: ReplayClock | None = None,
    ) -> Running:
        nc = await nats.connect(nats_server.url)
        c: Callouts[str] = Callouts(
            nc,
            src,
            engineers or roster(),
            sink,
            logging.getLogger("radio"),
            timing=timing,
            timeline=timeline,
            replay=replay,
        )
        r = Running(nc, c, asyncio.create_task(c.run()))
        running.append(r)
        return r

    yield start
    for r in running:
        if not r.nc.is_closed:
            await r.kill()
