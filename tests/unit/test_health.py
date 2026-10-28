import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from helpers import get, request

from radio import health
from radio.health import HealthServer

state = {"connected": False}


@pytest.fixture
async def server() -> AsyncIterator[HealthServer]:
    state["connected"] = False
    srv = HealthServer("127.0.0.1", 0, "1.2.3", lambda: state["connected"], logging.getLogger("t"))
    await srv.start()
    yield srv
    await srv.close()


async def test_unavailable_until_connected(server: HealthServer) -> None:
    assert await get(server.port) == (
        503,
        {"status": "unavailable", "version": "1.2.3", "connected": False},
    )
    state["connected"] = True
    assert await get(server.port) == (200, {"status": "ok", "version": "1.2.3", "connected": True})
    assert (await get(server.port, "/healthz?verbose=1"))[0] == 200


async def test_other_paths_and_methods(server: HealthServer) -> None:
    assert (await get(server.port, "/metrics"))[0] == 404
    raw = b"POST /healthz HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
    assert (await request(server.port, raw))[0] == 405


async def test_a_bad_request(server: HealthServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # Headers past the limit, and a request that never ends its headers.
    big = b"GET /healthz HTTP/1.1\r\nX: " + b"a" * health.MAX_HEADER + b"\r\n\r\n"
    assert (await request(server.port, big))[0] == 431
    monkeypatch.setattr(health, "READ_TIMEOUT", 0.05)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"GET /healthz HTTP/1.1\r\n")
    response = await reader.read()
    writer.close()
    assert response.startswith(b"HTTP/1.1 400 ")


async def test_a_client_that_hangs_up(server: HealthServer) -> None:
    _, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"GET /hea")
    writer.close()
    await writer.wait_closed()
    # The server is still there for the next one.
    assert (await get(server.port))[0] == 503
