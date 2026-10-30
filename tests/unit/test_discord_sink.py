"""The discord sink (paddock#58) against a fake gateway, voice channel and voice
client: no network. The service tests run it against the real nats-server."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import nats
import pytest
from helpers import DISCORD, TOKEN, FakeVoice, get, lines, until, write_wav
from nats.js.api import KeyValueConfig
from nats.js.client import JetStreamContext
from nats.js.errors import KeyNotFoundError
from natsserver import NATSServer
from test_callouts import made, publish
from twr.state.v1 import live_pb2
from twr_contracts import ce

from radio import discord_sink, logs, service
from radio.__main__ import main
from radio.discord_sink import STALE_ATTEMPTS, DiscordSink
from radio.piper_voice import Voices
from radio.queue import Pending
from radio.service import Radio
from radio.settings import Secret, Settings
from radio.sink import Clip, NotConnected
from radio.speech import Utterance


class FakeSource:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cleaned = 0

    def cleanup(self) -> None:
        self.cleaned += 1


class FakeVoiceClient:
    """mode: "finish" calls after at once, "raise" raises as discord.py does when
    voice dropped, "hang" never calls after."""

    def __init__(self, connected: bool = True, mode: str = "finish") -> None:
        self.connected = connected
        self.mode = mode
        self.played: list[Any] = []
        self.stopped = 0
        self.disconnects = 0
        self.channel: FakeChannel | None = None

    def is_connected(self) -> bool:
        return self.connected

    def play(self, source: Any, *, after: Callable[[Exception | None], Any]) -> None:
        if self.mode == "raise":
            raise RuntimeError("Not connected to voice.")
        self.played.append(source)
        if self.mode == "finish":
            after(None)

    def stop(self) -> None:
        self.stopped += 1

    async def disconnect(self, *, force: bool = False) -> None:
        # discord.py drops the client from the guild on disconnect.
        self.disconnects += 1
        self.connected = False
        if self.channel is not None and self.channel.existing is self:
            self.channel.existing = None


class FakeChannel:
    """A voice channel. connect waits for `release` (set by default), and with an
    existing voice client raises as discord.py does."""

    name = "pit-lane"

    def __init__(self, existing: FakeVoiceClient | None = None, fail: str = "") -> None:
        self.existing = existing
        if existing is not None:
            existing.channel = self
        self.fail = fail
        self.release = asyncio.Event()
        self.release.set()
        self.connects: list[dict[str, bool]] = []
        self.made: list[FakeVoiceClient] = []

    def voice_client(self) -> FakeVoiceClient | None:
        return self.existing

    async def connect(self, *, reconnect: bool, self_deaf: bool) -> FakeVoiceClient:
        self.connects.append({"reconnect": reconnect, "self_deaf": self_deaf})
        await self.release.wait()
        if self.fail:
            raise RuntimeError(self.fail)
        if self.existing is not None:
            raise RuntimeError("Already connected to a voice channel.")
        vc = FakeVoiceClient()
        vc.channel = self
        self.existing = vc
        self.made.append(vc)
        return vc


class FakeGateway:
    def __init__(self, channel: FakeChannel | None = None) -> None:
        self.chan = channel or FakeChannel()
        self.started = 0
        self.closed = 0
        self.sources: list[FakeSource] = []

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def channel(self, channel_id: int) -> FakeChannel:
        return self.chan

    def source(self, path: Path) -> FakeSource:
        self.sources.append(FakeSource(path))
        return self.sources[-1]


def voices(tmp_path: Path) -> Voices:
    return Voices(
        Settings(cache_dir=tmp_path / "cache"), logging.getLogger("radio"), lambda m, v: FakeVoice()
    )


def sink(tmp_path: Path, gateway: FakeGateway, **kw: Any) -> DiscordSink:
    return DiscordSink(
        io.StringIO(), voices(tmp_path), gateway, 1, logging.getLogger("radio"), **kw
    )


def msgs(log: io.StringIO) -> list[object]:
    return [x["msg"] for x in lines(log)]


# R1: joined self-deafened.


async def test_joins_self_deafened(tmp_path: Path, log: io.StringIO) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g)
    assert await s.join(now=100.0)
    assert g.chan.connects == [{"reconnect": True, "self_deaf": True}]
    assert "voice: joined" in msgs(log)


async def test_a_failed_join_is_logged_not_raised(tmp_path: Path, log: io.StringIO) -> None:
    g = FakeGateway(FakeChannel(fail="no such channel"))
    s = sink(tmp_path, g)
    assert not await s.join(now=100.0)
    [line] = [x for x in lines(log) if x["msg"] == "voice: join failed"]
    assert line["err"] == "no such channel"


async def test_joins_are_backoff_apart(tmp_path: Path) -> None:
    g = FakeGateway(FakeChannel(fail="down"))
    s = sink(tmp_path, g, backoff=15.0)
    await s.join(now=100.0)
    await s.join(now=110.0)  # within the backoff: no attempt
    assert len(g.chan.connects) == 1
    await s.join(now=115.0)
    assert len(g.chan.connects) == 2


# R4, AC2: adopting a voice client discord.py is reconnecting.


async def test_reuses_a_voice_client_discord_reconnected_itself(tmp_path: Path) -> None:
    # After a gateway stall discord.py reconnects voice on its own; channel.connect
    # then raises "Already connected" for as long as it exists.
    existing = FakeVoiceClient(connected=True)
    g = FakeGateway(FakeChannel(existing))
    s = sink(tmp_path, g)
    assert await s.join(now=100.0)
    assert g.chan.connects == []


async def test_waits_while_discord_reconnects_then_joins_afresh(tmp_path: Path) -> None:
    stale = FakeVoiceClient(connected=False)
    g = FakeGateway(FakeChannel(stale))
    s = sink(tmp_path, g, backoff=15.0)
    for i in range(STALE_ATTEMPTS - 1):
        assert not await s.join(now=100.0 + 15.0 * i)
        assert g.chan.connects == [] and stale.disconnects == 0
    assert await s.join(now=100.0 + 15.0 * STALE_ATTEMPTS)
    assert stale.disconnects == 1
    assert len(g.chan.connects) == 1


# R3, AC2: leaving when idle.


async def test_leaves_after_the_idle_time_with_nothing_on(tmp_path: Path) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g, idle=600.0)
    s.active(100.0)  # the last activity, then nothing
    await until(lambda: s.connected)
    await s.check_idle(500.0)
    assert s.connected
    await s.check_idle(701.0)
    assert g.chan.made[0].disconnects == 1
    assert not s.connected


async def test_the_idle_clock_starts_at_the_first_check(tmp_path: Path) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g, idle=10.0)
    await s.join(now=0.0)
    s._last_active = None  # joined with no activity seen
    await s.check_idle(100.0)
    await s.check_idle(105.0)
    assert s.connected
    await s.check_idle(111.0)
    assert not s.connected


async def test_an_open_session_keeps_voice_until_it_ends(tmp_path: Path) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g, idle=600.0)
    s.event("twr.v1.live.evt.S1.session.started", now=0.0)
    await until(lambda: s.connected)
    await s.check_idle(5000.0)  # a long quiet stretch in the session
    assert s.connected
    s.event("twr.v1.live.evt.S1.session.ended", now=6000.0)
    await s.check_idle(6500.0)
    assert s.connected
    await s.check_idle(6601.0)
    assert not s.connected


async def test_events_outside_a_session_open_nothing(tmp_path: Path) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g)
    s.event("twr.v1.live.evt._.telemetry.lost", now=0.0)
    s.event("twr.v1.live.frame.gt7", now=0.0)
    assert s._joining is None and g.chan.connects == []


async def test_the_idle_check_runs_on_a_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It fires with nothing arriving at all (v1's watchdog).
    monkeypatch.setattr(discord_sink, "IDLE_CHECK", 0.01)
    clock = [0.0]
    g = FakeGateway()
    s = sink(tmp_path, g, idle=10.0, monotonic=lambda: clock[0])
    await s.start()
    s.active()
    await until(lambda: s.connected)
    clock[0] = 11.0
    await until(lambda: not s.connected)
    await s.close()
    assert g.started == 1 and g.closed == 1


# R5, AC3: playback.


def clip(tmp_path: Path, seconds: float = 0.1) -> Clip:
    path = write_wav(tmp_path / "clip.wav", 100)
    return Clip("Pit this lap.", seconds, Utterance(path, seconds, "Pit this lap."))


def pending() -> Pending:
    class Ack:
        async def ack(self) -> None: ...

        async def ack_sync(self, timeout: float = 1.0) -> object:
            return None

    return Pending(made("fuel", "template_pit", 15), "p1", 1, Ack())  # type: ignore[arg-type]


async def playing(tmp_path: Path, vc: FakeVoiceClient) -> tuple[DiscordSink, FakeGateway]:
    g = FakeGateway(FakeChannel(vc))
    s = sink(tmp_path, g)
    assert await s.join(now=0.0)
    return s, g


async def test_plays_and_returns_when_done(tmp_path: Path) -> None:
    vc = FakeVoiceClient()
    s, g = await playing(tmp_path, vc)
    await asyncio.wait_for(s.play(clip(tmp_path), pending()), 1)
    assert vc.played == g.sources and g.sources[0].cleaned == 0


async def test_a_play_that_fails_cleans_its_source_up(tmp_path: Path) -> None:
    vc = FakeVoiceClient(mode="raise")
    s, g = await playing(tmp_path, vc)
    with pytest.raises(RuntimeError, match="Not connected"):
        await s.play(clip(tmp_path), pending())
    assert len(g.sources) == 1 and g.sources[0].cleaned == 1


async def test_a_playback_that_never_calls_back_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log: io.StringIO
) -> None:
    # AC3: stopped after the clip's length plus the margin (0.1 s + 0.2 s here, 5 s
    # in radio), then the next plays.
    monkeypatch.setattr(discord_sink, "PLAY_MARGIN", 0.2)
    vc = FakeVoiceClient(mode="hang")
    s, g = await playing(tmp_path, vc)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(s.play(clip(tmp_path), pending()), 2)
    assert 0.3 <= loop.time() - started < 2.0
    assert vc.stopped == 1 and g.sources[0].cleaned == 1
    [line] = [x for x in lines(log) if x["msg"] == "voice: playback did not finish, stopping"]
    assert line["bound_s"] == pytest.approx(0.3)
    vc.mode = "finish"
    await asyncio.wait_for(s.play(clip(tmp_path), pending()), 1)
    assert len(vc.played) == 2


async def test_an_unknown_length_is_bounded_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_sink, "PLAY_UNKNOWN", 0.1)
    vc = FakeVoiceClient(mode="hang")
    s, g = await playing(tmp_path, vc)
    await asyncio.wait_for(s.play(clip(tmp_path, seconds=0.0), pending()), 2)
    assert vc.stopped == 1


async def test_voice_gone_before_playing_is_not_connected(tmp_path: Path) -> None:
    # R6: voice dropped while the clip was synthesised.
    vc = FakeVoiceClient()
    s, g = await playing(tmp_path, vc)
    vc.connected = False
    with pytest.raises(NotConnected):
        await s.play(clip(tmp_path), pending())
    assert g.sources == []


# Through the service, against the real nats-server: R2, R6, R7, AC1, AC4.


def discord_radio(
    url: str, tmp_path: Path, gateway: FakeGateway, src: str, **settings: Any
) -> Radio:
    (tmp_path / "fake.onnx").write_bytes(b"")
    return Radio(
        Settings(
            nats_url=url,
            health_host="127.0.0.1",
            health_port=0,
            src=src,
            sink="discord",
            discord_token=Secret(TOKEN),
            discord_channel_id=1,
            cache_dir=tmp_path / "cache",
            voice_model=tmp_path / "fake.onnx",
            **settings,
        ),
        "1.2.3",
        logging.getLogger("radio"),
        io.StringIO(),
        lambda s, logger: voices(tmp_path),
        lambda s, logger: gateway,
    )


async def test_a_session_starts_a_join_and_a_callout_waits_for_it(
    nats_server: NATSServer, js: JetStreamContext, log: io.StringIO, tmp_path: Path
) -> None:
    # AC1, R2, R7: session.started starts the join without waiting for it; a
    # callout taken meanwhile is said once voice is up, and /healthz says so.
    g = FakeGateway()
    g.chan.release.clear()
    r = discord_radio(nats_server.url, tmp_path, g, "joining")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "callouts: taking" in msgs(log))
    nc = await nats.connect(nats_server.url)
    await nc.publish("twr.v1.joining.evt.S1.session.started", b"{}")
    await until(lambda: len(g.chan.connects) == 1)
    assert (await get(r.health.port))[1]["voice_connected"] is False
    await publish(js, made("final_lap", "template", 10), src="joining")
    await until(lambda: "callouts: engineer" in msgs(log))
    assert g.sources == []
    g.chan.release.set()
    await until(lambda: len(g.chan.made) == 1 and len(g.chan.made[0].played) == 1)
    assert (await get(r.health.port))[1]["voice_connected"] is True
    assert len(g.chan.connects) == 1
    stop.set()
    await asyncio.wait_for(task, 10)
    await nc.close()
    assert g.closed == 1


async def test_a_callout_that_expires_during_the_join_is_dropped(
    nats_server: NATSServer, js: JetStreamContext, log: io.StringIO, tmp_path: Path
) -> None:
    # AC1, R6: dropped at `connecting` once it expires; the join carries on.
    g = FakeGateway()
    g.chan.release.clear()
    r = discord_radio(nats_server.url, tmp_path, g, "expiring")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "callouts: taking" in msgs(log))
    # Expires 1.5 s on: radio takes it well before, while the join is held.
    await publish(js, made("final_lap", "template", 10, expires_in=1.5), src="expiring")
    await until(lambda: "callout dropped" in msgs(log))
    [drop] = [x for x in lines(log) if x["msg"] == "callout dropped"]
    assert drop["stage"] == "connecting"
    g.chan.release.set()
    await until(lambda: r.voice_connected() is True)
    assert g.sources == []
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_a_failed_join_drops_the_callout_not_connected(
    nats_server: NATSServer, js: JetStreamContext, log: io.StringIO, tmp_path: Path
) -> None:
    # R6: the join failed, so it can't be said.
    g = FakeGateway(FakeChannel(fail="Missing Permissions"))
    r = discord_radio(nats_server.url, tmp_path, g, "refused")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "callouts: taking" in msgs(log))
    await publish(js, made("final_lap", "template", 10), src="refused")
    await until(lambda: "callout dropped" in msgs(log))
    [drop] = [x for x in lines(log) if x["msg"] == "callout dropped"]
    assert drop["stage"] == "not connected"
    stop.set()
    await asyncio.wait_for(task, 10)


class LeakyGateway(FakeGateway):
    """A gateway whose library is careless with the token: it logs it in a message,
    a field and a traceback, then stops radio."""

    def __init__(self, token: str, stop: asyncio.Event) -> None:
        super().__init__()
        self.token = token
        self.stop = stop

    async def start(self) -> None:
        await super().start()
        logging.getLogger("discord.client").info("logging in with %s", self.token)
        logging.getLogger("radio").warning("login", extra={"token": self.token})
        try:
            raise RuntimeError(f"token {self.token} refused")
        except RuntimeError:
            logging.getLogger("radio").exception("traceback")
        self.stop.set()


def test_the_token_is_in_no_log_line(
    nats_server: NATSServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC4, R8: with the token a known string, radio as it runs, from its
    # environment, with a library that leaks it.
    (tmp_path / "fake.onnx").write_bytes(b"")
    stop = asyncio.Event()
    gateways: list[LeakyGateway] = []

    def gateway(s: Settings, logger: logging.Logger) -> LeakyGateway:
        assert s.discord_token is not None
        gateways.append(LeakyGateway(s.discord_token.reveal(), stop))
        return gateways[-1]

    monkeypatch.setattr(service, "open_gateway", gateway)
    monkeypatch.setattr(service, "Voices", lambda s, logger: voices(tmp_path))
    out = io.StringIO()
    env = {
        **DISCORD,
        "HOME": str(tmp_path),
        "TWR_NATS_URL": nats_server.url,
        "TWR_HEALTH_ADDR": "127.0.0.1:0",
        "TWR_LOG_FORMAT": "json",
        "TWR_LOG_LEVEL": "debug",
        "TWR_RADIO_SRC": "secret",
        "TWR_RADIO_VOICE_MODEL": str(tmp_path / "fake.onnx"),
    }
    try:
        assert main([], env, out, stop) == 0
    finally:
        logs.setup(logging.INFO, "json", io.StringIO())
    written = out.getvalue()
    assert gateways and gateways[0].token == TOKEN
    assert "logging in with [redacted]" in written
    assert "token [redacted] refused" in written
    assert TOKEN not in written


async def test_the_token_is_in_no_status_body(
    nats_server: NATSServer, log: io.StringIO, tmp_path: Path
) -> None:
    # AC4, R8, R7: /healthz says whether voice is up, and nothing of the token.
    r = discord_radio(nats_server.url, tmp_path, FakeGateway(), "status")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await until(lambda: "callouts: taking" in msgs(log))
    status, body = await get(r.health.port)
    assert (status, body["voice_connected"]) == (200, False)
    assert TOKEN not in json.dumps(body)
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_an_open_session_retries_a_failed_join_on_the_timer(
    tmp_path: Path, log: io.StringIO
) -> None:
    # A failed join (a Discord 5xx) is tried again by the idle timer while a
    # session is open, backoff apart, without waiting for the next event.
    g = FakeGateway(FakeChannel(fail="503 Service Unavailable"))
    s = sink(tmp_path, g, backoff=15.0)
    s.event("twr.v1.live.evt.S1.session.started", now=0.0)
    await until(lambda: "voice: join failed" in msgs(log))
    g.chan.fail = ""
    await s.check_idle(10.0)  # within the backoff: no attempt
    assert len(g.chan.connects) == 1
    await s.check_idle(20.0)
    await until(lambda: s.connected)
    assert len(g.chan.connects) == 2


async def test_with_no_session_the_timer_joins_nothing(tmp_path: Path) -> None:
    g = FakeGateway()
    s = sink(tmp_path, g)
    await s.check_idle(0.0)
    await s.check_idle(100.0)
    assert g.chan.connects == []


async def test_closed_it_ignores_a_late_event(tmp_path: Path) -> None:
    # At shutdown, an event that arrives after the sink closed starts no join on
    # the closed gateway.
    g = FakeGateway()
    s = sink(tmp_path, g)
    await s.close()
    s.event("twr.v1.live.evt.S1.session.started", now=0.0)
    s.active(0.0)
    await s.check_idle(0.0)
    assert g.chan.connects == [] and s._joining is None


async def test_the_live_bucket_says_voice_dropped_within_a_second(
    nats_server: NATSServer, js: JetStreamContext, log: io.StringIO, tmp_path: Path
) -> None:
    # paddock#62 AC5, R9: radio.status.<src> in the live bucket says
    # voice_connected, and changes within 1 s of a voice drop.
    with contextlib.suppress(Exception):
        await js.delete_key_value("live")
    kv = await js.create_key_value(KeyValueConfig(bucket="live", history=1))
    g = FakeGateway()
    r = discord_radio(nats_server.url, tmp_path, g, "kvlive")
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))

    async def value() -> live_pb2.RadioStatus | None:
        try:
            entry = await kv.get("radio.status.kvlive")
        except KeyNotFoundError:
            return None
        m = live_pb2.RadioStatus()
        ce.unmarshal(entry.value or b"", m)
        return m

    async def says(connected: bool) -> bool:
        v = await value()
        return v is not None and v.HasField("voice_connected") and v.voice_connected is connected

    async def wait(connected: bool, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while not await says(connected):
                await asyncio.sleep(0.02)

    try:
        await until(lambda: "callouts: taking" in msgs(log))
        await wait(False, 5)
        nc = await nats.connect(nats_server.url)
        await nc.publish("twr.v1.kvlive.evt.S1.session.started", b"{}")
        await wait(True, 5)
        v = await value()
        assert v is not None and (v.source, v.sink) == ("kvlive", live_pb2.RADIO_SINK_DISCORD)
        # A second on, the writer may write again at once.
        await asyncio.sleep(1.1)
        g.chan.made[0].connected = False
        await wait(False, 1.0)
        await nc.close()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
        await js.delete_key_value("live")
