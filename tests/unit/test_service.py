import asyncio
import io
import logging

import pytest
from helpers import FakeNATS, get, lines, until
from nats.js.client import JetStreamContext
from natsserver import NATSServer
from test_callouts import made, publish

from radio import callouts, service
from radio.phrasing import templates
from radio.service import Radio, redacted
from radio.settings import Settings


def radio(url: str, log: io.StringIO, src: str = "live") -> Radio:
    """A Radio on url, logging to log (the fixture's lines), its /healthz on a free port."""
    return Radio(
        Settings(nats_url=url, health_host="127.0.0.1", health_port=0, src=src),
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
    # AC2: NATS that starts after radio. Until it does, /healthz says not connected.
    monkeypatch.setattr(service, "RETRY_WAIT", 0.02)
    gone = FakeNATS()
    await gone.start()
    await gone.close()  # Nothing listens on its port now.
    r = radio(gone.url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: msgs(log).count("nats error") >= 2)
    assert "ready" not in msgs(log)
    assert (await get(r.health.port)) == (
        503,
        {"status": "unavailable", "version": "1.2.3", "connected": False},
    )
    late = FakeNATS(gone.port)
    await late.start()
    try:
        await until(lambda: "ready" in msgs(log))
        assert (await get(r.health.port)) == (
            200,
            {"status": "ok", "version": "1.2.3", "connected": True},
        )
        stop.set()
        await asyncio.wait_for(task, 10)
    finally:
        await late.close()
    assert msgs(log)[-1] == "stopped"


async def test_stops_while_still_retrying(
    log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "RETRY_WAIT", 0.02)
    gone = FakeNATS()
    await gone.start()
    await gone.close()
    r = radio(gone.url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "nats error" in msgs(log))
    stop.set()
    await asyncio.wait_for(task, 10)
    assert "ready" not in msgs(log)
    assert "nats disconnected" not in msgs(log)
    assert msgs(log)[-2:] == ["shutting down", "stopped"]


async def test_reconnects_after_nats_restarts(
    fake_nats: FakeNATS, log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "RETRY_WAIT", 0.02)
    r = radio(fake_nats.url, log)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "ready" in msgs(log))
    await fake_nats.close()
    await until(lambda: "nats disconnected" in msgs(log))
    assert (await get(r.health.port))[0] == 503
    again = FakeNATS(fake_nats.port)
    await again.start()
    try:
        await until(lambda: "nats reconnected" in msgs(log))
        assert (await get(r.health.port)) == (
            200,
            {"status": "ok", "version": "1.2.3", "connected": True},
        )

        # A drain that fails at shutdown is logged, and the connection closed anyway.
        async def broken_drain() -> None:
            raise ConnectionError

        assert r.nc is not None
        r.nc.drain = broken_drain  # type: ignore[method-assign]
        stop.set()
        await asyncio.wait_for(task, 10)
    finally:
        await again.close()
    assert "nats drain" in msgs(log)
    assert r.nc.is_closed
    assert msgs(log)[-1] == "stopped"


async def test_an_error_is_logged_without_credentials(log: io.StringIO) -> None:
    r = radio("nats://u:p@nats:4222", log)
    await r._error(RuntimeError("slow consumer"))
    [line] = lines(log)
    assert line["msg"] == "nats error"
    assert line["nats"] == "nats:4222"
    assert line["err"] == "slow consumer"


def test_redacted() -> None:
    assert redacted("nats://u:p@a:4222, nats://b:4222") == "a:4222,nats://b:4222"


async def test_speaks_the_sources_callouts(
    nats_server: NATSServer, js: JetStreamContext, log: io.StringIO
) -> None:
    r = radio(nats_server.url, log, src="svc")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "callouts: taking" in msgs(log))
    await publish(js, made("final_lap", "template", 10), src="svc")
    await until(lambda: "speaking" in msgs(log))
    [said] = [x for x in lines(log) if x["msg"] == "speaking"]
    # Phrased with the script, which has variants.
    assert (said["rule"], said["kind"]) == ("final_lap", "template")
    assert said["text"] in templates()["final_lap"]["template"]
    stop.set()
    await asyncio.wait_for(task, 10)
    assert msgs(log)[-1] == "stopped"


async def test_callouts_that_fail_stop_radio(
    fake_nats: FakeNATS, log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: object) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(callouts.Callouts, "run", broken)
    r = radio(fake_nats.url, log)
    with pytest.raises(RuntimeError, match="broken"):
        await asyncio.wait_for(r.run(asyncio.Event()), 10)
    assert msgs(log)[-1] == "stopped"
