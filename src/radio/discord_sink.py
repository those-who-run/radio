"""Speaking in Discord (paddock#58): v1's `DiscordPlayer` as a sink.

The PS5 joins a voice channel through its Discord integration, and radio's bot
joins the same channel and plays each callout's audio there, which is how it
reaches the headset. Which bot radio speaks as is the deployment's choice: the
token it is given. v2 reuses v1's bot and channel, and the two are never live at
once (the short shift scales v1's engineer to 0 before v2 races).

Joining. Something to say opens voice: an event of a session on radio's source
(ecu's `session.started` and the rest, the v2 equivalent of v1's `on_phase`), or a
callout. The join runs in the background, so nothing waits for it, and a callout
that goes stale while it runs is dropped at `connecting` without cancelling it
(`channel.connect` can take a minute). Joins are at least `backoff` apart. A voice
client discord.py already holds, reconnecting on its own after a gateway hiccup,
is adopted rather than failing with "Already connected", and torn down for a
fresh join only once it has stayed down for STALE_ATTEMPTS attempts.

Leaving. While no session is open, voice is left `idle` seconds after the last
activity, checked on a timer so it also happens when nothing arrives (v1's
`check_idle`).

Playing. A clip plays for at most its length plus PLAY_MARGIN, or PLAY_UNKNOWN when
its length isn't known, then is stopped and logged; an audio source that fails to
start is cleaned up, so it never leaks.

Listening (paddock#61). Each change of a member's voice state in radio's channel,
someone joining, leaving, moving, muting or deafening (by themselves or the
server), is one INFO line with their display name and nothing else of theirs, and
sets the listener count: the members in the channel who are not bots and not
deafened. The count is None until the first voice state, and goes with every
"speaking" line and into radio's status. The first callout played to 0
listeners in a session is one WARNING, so a race talked to an empty channel, as
v1's on 2026-10-19 after a long pause, shows in the log.

The Discord library stays behind `Gateway` (radio.gateway), so this is tested with
fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from radio.piper_voice import Voices
from radio.queue import Pending
from radio.sink import Clip, ConsoleSink, NotConnected

# v1's DiscordPlayer.STALE_ATTEMPTS_BEFORE_RESET: with the 15 s backoff, a voice
# client still down after a minute is replaced.
STALE_ATTEMPTS = 4
# On top of the clip's length before playback is cut off, and the bound when the
# length isn't known (v1's PLAY_MARGIN_SECS, PLAY_UNKNOWN_SECS).
PLAY_MARGIN = 5.0
PLAY_UNKNOWN = 30.0
# How often voice is checked for idleness.
IDLE_CHECK = 10.0
# The events that close a session: anything else of a session opens it.
CLOSING = ("session.ended", "session.discarded")
# How many sessions radio remembers it warned of an empty channel in.
WARNED = 64
# Where a member is, as far as radio's voice channel goes.
HERE = "here"
ELSEWHERE = "elsewhere"
NOWHERE = "nowhere"


@dataclass(frozen=True)
class Seat:
    """A member's voice state, as far as radio's channel goes: where they are, and
    whether they are muted or deafened, by themselves or by the server."""

    where: str = NOWHERE
    self_mute: bool = False
    mute: bool = False
    self_deaf: bool = False
    deaf: bool = False


def changes(before: Seat, after: Seat) -> list[tuple[str, str | None]]:
    """What changed in radio's channel, each as (change, by): `joined`, `left`,
    `moved in`, `moved out`, or, while in the channel, `muted`, `unmuted`,
    `deafened` and `undeafened`, by `self` or `server`."""
    if before.where != HERE and after.where == HERE:
        return [("joined" if before.where == NOWHERE else "moved in", None)]
    if before.where == HERE and after.where != HERE:
        return [("left" if after.where == NOWHERE else "moved out", None)]
    if before.where != HERE:
        return []
    out: list[tuple[str, str | None]] = []
    for name, on, off in (("mute", "muted", "unmuted"), ("deaf", "deafened", "undeafened")):
        for by, attr in (("self", f"self_{name}"), ("server", name)):
            was, now = getattr(before, attr), getattr(after, attr)
            if was != now:
                out.append((on if now else off, by))
    return out


def listeners(count: int | None) -> str:
    """`1 listener`, `0 listeners`."""
    return f"{count} listener" if count == 1 else f"{count} listeners"


class AudioSource(Protocol):
    """discord.py's AudioSource: what a voice client plays."""

    def cleanup(self) -> None: ...


class VoiceClient(Protocol):
    """discord.py's VoiceClient, as far as radio uses it."""

    def is_connected(self) -> bool: ...

    def play(self, source: Any, *, after: Callable[[Exception | None], Any]) -> None: ...

    def stop(self) -> None: ...

    async def disconnect(self, *, force: bool = False) -> None: ...


class VoiceChannel(Protocol):
    """A voice channel, and the voice client its guild already holds."""

    @property
    def name(self) -> str: ...

    def voice_client(self) -> VoiceClient | None: ...

    async def connect(self, *, reconnect: bool, self_deaf: bool) -> VoiceClient: ...


class Gateway(Protocol):
    """The Discord client, logged in as the bot (radio.gateway)."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def channel(self, channel_id: int) -> VoiceChannel: ...

    def source(self, path: Path) -> AudioSource: ...

    def watch(
        self, channel_id: int, changed: Callable[[str | None, Seat, Seat, int], None]
    ) -> None:
        """Call changed(display name, before, after, listeners) on each voice state
        update that touches the channel, with the channel's listener count after
        it; the name is None for the bot's own."""
        ...

    def listeners(self, channel_id: int) -> int | None:
        """The channel's listener count now, from the gateway's cache; None when the
        channel isn't known yet."""
        ...


class DiscordSink(ConsoleSink):
    """Each callout synthesised in its engineer's voice and played in the voice
    channel. monotonic is the clock joins, backoff and idleness are timed on."""

    def __init__(
        self,
        out: IO[str],
        voices: Voices,
        gateway: Gateway,
        channel_id: int,
        logger: logging.Logger,
        idle: float = 600.0,
        backoff: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(out, voices)
        self.gateway = gateway
        self.channel_id = channel_id
        self.logger = logger
        self.idle = idle
        self.backoff = backoff
        self.monotonic = monotonic
        self._vc: VoiceClient | None = None
        self._lock = asyncio.Lock()
        self._last_attempt: float | None = None
        self._stale_attempts = 0
        self._last_active: float | None = None
        # The sessions open on radio's source, by id.
        self._sessions: set[str] = set()
        self._joining: asyncio.Task[None] | None = None
        self._watching: asyncio.Task[None] | None = None
        # Once closed, nothing joins again: a late event at shutdown is ignored.
        self._closed = False
        # Who can hear radio in its channel (paddock#61): None until the first
        # voice state.
        self.listeners: int | None = None
        # The sessions warned of an empty channel, oldest first.
        self._warned: dict[str, None] = {}

    # Lifecycle.

    async def start(self) -> None:
        """Log in, and check for idleness until closed."""
        await self.gateway.start()
        self.gateway.watch(self.channel_id, self.voice_state)
        self._watching = asyncio.create_task(self._watch(), name="voice-idle")

    async def close(self) -> None:
        self._closed = True
        for task in (self._watching, self._joining):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._disconnect()
        await self.gateway.close()
        await super().close()

    @property
    def connected(self) -> bool:
        return self._vc is not None and self._vc.is_connected()

    # Joining and leaving.

    def event(self, subject: str, now: float | None = None) -> None:
        """An event on `twr.v1.<src>.evt.<sid>.<area>.<what>`: one of a session is
        activity and opens voice, without waiting for the join; `session.ended`
        and `session.discarded` close the session. Events outside a session (`_`)
        are neither."""
        parts = subject.split(".")
        if self._closed or len(parts) < 7 or parts[3] != "evt" or parts[4] == "_":
            return
        sid, what = parts[4], ".".join(parts[5:])
        now = self.monotonic() if now is None else now
        if what in CLOSING:
            self._sessions.discard(sid)
            self._last_active = now
            return
        if sid not in self._sessions:
            self._sessions.add(sid)
            self.logger.info("voice: session open", extra={"session_id": sid})
        self.active(now)

    def active(self, now: float | None = None) -> None:
        """Something to say: voice stays up, and is joined if it isn't."""
        if self._closed:
            return
        now = self.monotonic() if now is None else now
        self._last_active = now
        if not self.connected:
            self._start_join(now)

    async def check_idle(self, now: float | None = None) -> None:
        """Leave once nothing has been on for idle seconds. An open session is
        activity, as v1's at_track, in_race and paused were, and voice down
        while one is open is joined again, backoff apart, as v1 retried while on
        track: a failed join doesn't wait for the next event."""
        now = self.monotonic() if now is None else now
        if self._sessions:
            self._last_active = now
            if not self.connected and not self._closed:
                self._start_join(now)
            return
        if self._last_active is None:
            self._last_active = now
            return
        if self.connected and now - self._last_active > self.idle:
            self.logger.info(
                "voice: idle, leaving", extra={"idle_s": round(now - self._last_active)}
            )
            await self._disconnect()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(IDLE_CHECK)
            await self.check_idle()

    async def join(self, now: float | None = None) -> bool:
        """Join, if voice isn't up, and wait for the outcome: whether voice is up."""
        task = self._start_join(self.monotonic() if now is None else now)
        if task is not None:
            # A caller that gives up must not cancel the join itself.
            await asyncio.shield(task)
        return self.connected

    def _start_join(self, now: float) -> asyncio.Task[None] | None:
        """The running join, or a new one in the background; None when voice is up
        or the last attempt is within the backoff."""
        if self._joining is not None and not self._joining.done():
            return self._joining
        if self.connected or (
            self._last_attempt is not None and now - self._last_attempt < self.backoff
        ):
            return None
        self._joining = asyncio.create_task(self._join(now), name="voice-join")
        return self._joining

    async def _join(self, now: float) -> None:
        async with self._lock:
            if self.connected:
                return
            self._last_attempt = now
            try:
                channel = await self.gateway.channel(self.channel_id)
                if await self._adopt(channel):
                    return
                self._vc = await channel.connect(reconnect=True, self_deaf=True)
                self._stale_attempts = 0
                self.logger.info("voice: joined", extra={"channel": channel.name})
            except Exception as err:
                self.logger.warning(
                    "voice: join failed",
                    extra={"channel_id": self.channel_id, "err": str(err) or type(err).__name__},
                )
                self._vc = None

    async def _adopt(self, channel: VoiceChannel) -> bool:
        """Handle a voice client discord.py already holds for the guild: True when
        there is nothing to join.

        After a gateway hiccup discord.py reconnects voice on its own, and
        `channel.connect` raises "Already connected to a voice channel" for as long
        as that client exists. It is used when it is up, waited for while it is
        reconnecting, and torn down only once it has stayed down for STALE_ATTEMPTS
        attempts."""
        existing = channel.voice_client()
        if existing is None:
            self._stale_attempts = 0
            return False
        if existing.is_connected():
            self._vc = existing
            self._stale_attempts = 0
            self.logger.info("voice: reusing the connection", extra={"channel": channel.name})
            return True
        self._stale_attempts += 1
        if self._stale_attempts < STALE_ATTEMPTS:
            self.logger.info(
                "voice: reconnecting, waiting",
                extra={"channel": channel.name, "attempt": self._stale_attempts},
            )
            self._vc = existing
            return True
        self.logger.warning(
            "voice: stuck reconnecting, joining afresh", extra={"channel": channel.name}
        )
        with contextlib.suppress(Exception):
            await existing.disconnect(force=True)
        self._vc = None
        self._stale_attempts = 0
        return False

    async def _disconnect(self) -> None:
        vc, self._vc = self._vc, None
        if vc is None:
            return
        try:
            await vc.disconnect(force=True)
        except Exception as err:
            self.logger.warning("voice: leaving", extra={"err": str(err) or type(err).__name__})
        self.logger.info("voice: left")

    # Listening.

    def voice_state(self, name: str | None, before: Seat, after: Seat, count: int) -> None:
        """A member's voice state changed: a line for each change in the channel,
        and the listener count. radio's own (name None) only counts."""
        self.listeners = count
        if name is None:
            return
        for change, by in changes(before, after):
            extra: dict[str, object] = {"member": name, "change": change, "listeners": count}
            if by is not None:
                extra["by"] = by
            self.logger.info(f"voice: {name} {change}", extra=extra)

    def current_listeners(self) -> int | None:
        """The count from the channel's voice states now. The events alone can miss
        a change: after a gateway outage discord.py rebuilds its cache without
        sending them, so a listener who left meanwhile would still count."""
        n = self.gateway.listeners(self.channel_id)
        if n is not None:
            self.listeners = n
        return self.listeners

    def _heard(self, callout: Pending) -> None:
        """The first callout played to no one in a session warns, once."""
        if self.listeners != 0:
            return
        sid = callout.callout.session_id
        if sid in self._warned:
            return
        self._warned[sid] = None
        while len(self._warned) > WARNED:
            del self._warned[next(iter(self._warned))]
        self.logger.warning(
            "voice: talking to an empty channel",
            extra={"session_id": sid, "rule": callout.callout.rule, "id": callout.callout.id},
        )

    # The sink's steps.

    async def connect(self) -> bool:
        """A callout to say: voice up, or joined. Callouts cancels this when the
        callout goes stale; the join carries on."""
        now = self.monotonic()
        self._last_active = now
        return await self.join(now)

    async def play(self, clip: Clip, callout: Pending) -> None:
        if clip.utterance is None:
            return  # No audio: prepare gives none of these with a voice.
        vc = self._vc
        if vc is None or not vc.is_connected():
            raise NotConnected("voice dropped before playing")
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def after(err: Exception | None) -> None:
            # On discord.py's player thread.
            if err is not None:
                self.logger.error("voice: playback", extra={"err": str(err)})
            loop.call_soon_threadsafe(done.set)

        bound = clip.seconds + PLAY_MARGIN if clip.seconds > 0 else PLAY_UNKNOWN
        source = self.gateway.source(clip.utterance.path)
        try:
            c = callout.callout
            n = self.current_listeners()
            to = "" if n is None else f" to {listeners(n)}"
            self.logger.info(
                f"speaking ({c.rule}){to}: {callout.text}",
                extra={"rule": c.rule, "id": c.id, "listeners": n},
            )
            vc.play(source, after=after)
            # Only once it plays: a callout that never started warns no one.
            self._heard(callout)
        except BaseException:
            # play never started: nothing else will clean the source up.
            source.cleanup()
            raise
        try:
            await asyncio.wait_for(done.wait(), bound)
        except TimeoutError:
            self.logger.warning(
                "voice: playback did not finish, stopping",
                extra={"rule": callout.callout.rule, "id": callout.callout.id, "bound_s": bound},
            )
            with contextlib.suppress(Exception):
                vc.stop()
            source.cleanup()
