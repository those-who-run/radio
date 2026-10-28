import asyncio
import io
import logging

import pytest
from helpers import FakeNATS, get, lines, until

from radio import service
from radio.service import Radio, redacted
from radio.settings import Settings


def radio(url: str, log: io.StringIO) -> Radio:
    """A Radio on url, logging to log (the fixture's lines), its /healthz on a free port."""
    return Radio(
        Settings(nats_url=url, health_host="127.0.0.1", health_port=0),
        "1.2.3",
        logging.getLogger("radio"),
    )


def msgs(log: io.StringIO) -> list[object]:
    return [line["msg"] for line in lines(log)]


async def test_connects_logs_ready_and_stops_cleanly(fake_nats: FakeNATS, log: io.StringIO) -> None:
    r = radio(fake_nats.url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "ready" in msgs(log))
    assert (await get(r.health.port)) == (
        200,
        {"status": "ok", "version": "1.2.3", "connected": True},
    )
    stop.set()
    await asyncio.wait_for(task, 10)
    after = msgs(log)[msgs(log).index("ready") :]
    # Draining the connection closes it, which nats-py reports as a disconnect.
    assert after in (
        ["ready", "shutting down", "stopped"],
        ["ready", "shutting down", "nats disconnected", "stopped"],
    )
    assert r.nc is not None and r.nc.is_closed


async def test_retries_until_nats_is_up(log: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "RETRY_WAIT", 0.02)
    nats_server = FakeNATS()
    await nats_server.start()
    url = nats_server.url
    await nats_server.close()  # Nothing listens there now.
    r = radio(url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: msgs(log).count("nats error") >= 2)
    assert (await get(r.health.port))[1]["connected"] is False
    stop.set()
    await asyncio.wait_for(task, 10)
    assert "ready" not in msgs(log)
    assert msgs(log)[-1] == "stopped"


async def test_reconnect_and_a_failed_drain_are_logged(
    fake_nats: FakeNATS, log: io.StringIO
) -> None:
    r = radio(fake_nats.url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "ready" in msgs(log))
    await r._reconnected()
    await r._error(RuntimeError("slow consumer"))

    async def broken_drain() -> None:
        raise ConnectionError

    assert r.nc is not None
    r.nc.drain = broken_drain  # type: ignore[method-assign]
    await fake_nats.close()
    await until(lambda: "nats disconnected" in msgs(log))
    stop.set()
    await asyncio.wait_for(task, 10)
    assert {"nats reconnected", "nats error", "nats drain"} <= set(msgs(log))


def test_redacted() -> None:
    assert redacted("nats://u:p@a:4222, nats://b:4222") == "a:4222,nats://b:4222"
