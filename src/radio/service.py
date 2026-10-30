"""radio's service: serve GET /healthz, connect to NATS, log "ready", take and speak
the source's callouts (radio.callouts) through the sink the settings name until it
is told to stop, then drain the connection and close the server. With the discord
sink, the source's events open voice (radio.discord_sink). Once connected, radio
keeps its status in the `live` bucket (radio.status).

NATS may not be up when radio starts, as in a pod whose server starts beside it, so
the first connection is retried until it succeeds or radio stops, with a `nats
error` line for each failed attempt; /healthz says `connected: false` meanwhile.
Once connected, nats-py reconnects on its own, without a limit, and the log says
when the connection drops and comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from typing import IO, Any

from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from radio.callouts import Callouts
from radio.discord_sink import DiscordSink, Gateway
from radio.engineer import roster
from radio.gateway import open_gateway
from radio.health import HealthServer
from radio.piper_voice import Voices
from radio.settings import Settings
from radio.sink import ConsoleSink, WavSink
from radio.status import Status, Writer
from radio.timing import SIMULATED, Timeline
from radio.voice import VoiceError

# How long nats-py waits between attempts at a server, at the first connection and
# after a drop.
RETRY_WAIT = 2
# How long draining the connection may take at shutdown.
DRAIN_TIMEOUT = 5


def redacted(urls: str) -> str:
    """The server list without credentials, for the log: a log is read by more
    people than hold them."""
    return ",".join(
        u.strip().rpartition("@")[2] if "@" in u else u.strip() for u in urls.split(",")
    )


class Radio:
    def __init__(
        self,
        settings: Settings,
        version: str,
        logger: logging.Logger,
        out: IO[str] = sys.stdout,
        voices: Callable[[Settings, logging.Logger], Voices] | None = None,
        gateway: Callable[[Settings, logging.Logger], Gateway] | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        # Where the console line goes, and how the voices are opened (a fake voice
        # in the tests), and Discord (a fake in the tests).
        self.out = out
        self.voices = voices or Voices
        self.gateway = gateway or open_gateway
        self.sink: ConsoleSink | None = None
        self.nc: Client | None = None
        # Whether the first connection was made: before it, a failed attempt is an
        # error, not a disconnect.
        self.was_connected = False
        # What speaks the callouts, once radio does.
        self.callouts: Callouts[Any] | None = None
        # radio's own tasks: /healthz is not ok once one has stopped.
        self.tasks: list[asyncio.Future[None]] = []
        self.status = Status(
            settings.src,
            settings.sink,
            self.voice_connected,
            self.voice_listeners,
            self.last_spoken_at,
        )
        self.health = HealthServer(
            settings.health_host,
            settings.health_port,
            version,
            self.connected,
            logger,
            self.ok,
            self.details,
        )

    def connected(self) -> bool:
        return self.nc is not None and self.nc.is_connected

    def ok(self) -> bool:
        """None of radio's tasks has stopped (connected to NATS is /healthz's)."""
        return not any(t.done() for t in self.tasks)

    def idle(self) -> bool:
        """Nothing queued or playing."""
        return self.callouts is None or self.callouts.idle

    def details(self) -> dict[str, object]:
        """/healthz's part beyond NATS: idle, and radio's status."""
        return {"idle": self.idle(), **self.status.body()}

    def last_spoken_at(self) -> datetime | None:
        return None if self.callouts is None else self.callouts.last_spoken_at

    def voice_listeners(self) -> int | None:
        """How many can hear radio in the voice channel (paddock#61); None without
        the discord sink, and before the first voice state."""
        return self.sink.listeners if isinstance(self.sink, DiscordSink) else None

    def voice_connected(self) -> bool | None:
        """Whether Discord voice is up; None without the discord sink."""
        if self.settings.sink != "discord":
            return None
        return isinstance(self.sink, DiscordSink) and self.sink.connected

    async def _disconnected(self) -> None:
        if self.was_connected:
            self.logger.warning("nats disconnected")

    async def _reconnected(self) -> None:
        self.logger.info("nats reconnected")
        if self.callouts is not None:
            # The pit entry may have come while NATS was away (paddock#325).
            self.callouts.reconnected()

    async def _error(self, err: Exception) -> None:
        # Among them each failed attempt to connect, while radio retries.
        self.logger.warning(
            "nats error",
            extra={"nats": redacted(self.settings.nats_url), "err": str(err) or type(err).__name__},
        )

    async def connect(self, stop: asyncio.Event) -> bool:
        """Connect to NATS, retrying until it succeeds (True) or stop is set (False).

        nats-py retries the first connection itself, without a limit, and reports
        each failed attempt to the error callback."""
        servers = [u.strip() for u in self.settings.nats_url.split(",") if u.strip()]
        self.nc = Client()
        connecting = asyncio.ensure_future(
            self.nc.connect(
                servers,
                name="radio",
                allow_reconnect=True,
                max_reconnect_attempts=-1,
                reconnect_time_wait=RETRY_WAIT,
                disconnected_cb=self._disconnected,
                reconnected_cb=self._reconnected,
                error_cb=self._error,
                drain_timeout=DRAIN_TIMEOUT,
            )
        )
        stopping = asyncio.ensure_future(stop.wait())
        await asyncio.wait({connecting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if connecting.done():
            connecting.result()  # A connect that gave up raises here.
            self.was_connected = True
            return True
        connecting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connecting
        return False

    async def speak(self, stop: asyncio.Event) -> None:
        """Take and speak callouts until stop is set. Callouts that fail stop radio."""
        assert self.nc is not None
        s = self.settings
        engineers = roster(s)
        models = [e.voice_model for e in engineers.all()]
        sink: ConsoleSink
        if s.sink in ("wav", "discord"):
            missing = [str(m) for m in models if not m.exists()]
            if missing:
                raise VoiceError(f"voice model missing: {', '.join(missing)} (run `make voice`)")
        if s.sink == "wav":
            assert s.wav_dir is not None
            sink = WavSink(self.out, self.voices(s, self.logger), s.wav_dir)
        elif s.sink == "discord":
            assert s.discord_channel_id is not None
            sink = DiscordSink(
                self.out,
                self.voices(s, self.logger),
                self.gateway(s, self.logger),
                s.discord_channel_id,
                self.logger,
                idle=s.idle_disconnect_secs,
                backoff=s.reconnect_backoff_secs,
            )
        elif s.timing == SIMULATED and all(m.exists() for m in models):
            # The simulated speaker measures each callout with the voices it has.
            sink = ConsoleSink(self.out, self.voices(s, self.logger))
        else:
            sink = ConsoleSink(self.out)
        if sink.voices is not None:
            # Every engineer's voice, loaded off the event loop and warm before the
            # first callout (v1's App._startup).
            for e in engineers.all():
                await sink.voices.load(e)
        self.sink = sink
        timeline = Timeline(s.timeline_file)
        events: Subscription | None = None
        try:
            if isinstance(sink, DiscordSink):
                await sink.start()
                events = await self._follow(sink)
        except BaseException:
            timeline.close()
            await sink.close()
            raise
        callouts: Callouts[Any] = Callouts(
            self.nc, s.src, engineers, sink, self.logger, timing=s.timing, timeline=timeline
        )
        self.callouts = callouts
        speaking = asyncio.ensure_future(callouts.run())
        self.tasks.append(speaking)
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({speaking, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if speaking.done():
                speaking.result()  # It runs until cancelled, so this raises.
            speaking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await speaking
        finally:
            timeline.close()
            if events is not None:
                # Before the sink closes: a late event must not start a join.
                with contextlib.suppress(Exception):
                    await events.unsubscribe()
            await sink.close()

    async def _follow(self, sink: DiscordSink) -> Subscription:
        """The source's events open voice and close sessions: a plain subscription,
        since only what happens from now on matters."""
        assert self.nc is not None

        async def on_event(msg: Msg) -> None:
            sink.event(msg.subject)

        return await self.nc.subscribe(f"twr.v1.{self.settings.src}.evt.>", cb=on_event)

    async def run(self, stop: asyncio.Event) -> None:
        """Serve and connect until stop is set, then shut down cleanly."""
        await self.health.start()
        try:
            if await self.connect(stop):
                assert self.nc is not None
                self.logger.info("ready", extra={"nats": redacted(self.settings.nats_url)})
                writing = asyncio.ensure_future(Writer(self.status, self.logger).run(self.nc))
                self.tasks.append(writing)
                await self.speak(stop)
        finally:
            self.logger.info("shutting down")
            for t in self.tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if self.nc is not None and not self.was_connected:
                await self.nc.close()
            elif self.nc is not None and not self.nc.is_closed:
                try:
                    await self.nc.drain()
                except Exception as err:  # The server may be gone already.
                    self.logger.warning("nats drain", extra={"err": str(err) or type(err).__name__})
                    await self.nc.close()
            await self.health.close()
            self.logger.info("stopped")
