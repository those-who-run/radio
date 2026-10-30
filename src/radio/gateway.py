"""The Discord client, logged in as the bot radio is given the token of
(paddock#58): the one place radio uses discord.py, behind radio.discord_sink's
`Gateway`, and the one client, so later users of Discord share it (R10).

Only voice is sent: the intents are `guilds` and `voice_states`, no message content
and nothing privileged (v1's DiscordPlayer). The token is handed to discord.py
and nowhere else. The voice states say who is in radio's channel (paddock#61),
which radio sees by display name only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

import discord

from radio.discord_sink import (
    ELSEWHERE,
    HERE,
    NOWHERE,
    AudioSource,
    Seat,
    VoiceChannel,
    VoiceClient,
)
from radio.settings import Secret, Settings


def intents() -> discord.Intents:
    """What the bot asks Discord for: its guilds and their voice states."""
    i = discord.Intents.none()
    i.guilds = True
    i.voice_states = True
    return i


def seat(state: discord.VoiceState, channel_id: int) -> Seat:
    """A discord.py voice state as radio.discord_sink's Seat."""
    ch = state.channel
    where = NOWHERE if ch is None else HERE if ch.id == channel_id else ELSEWHERE
    return Seat(where, state.self_mute, state.mute, state.self_deaf, state.deaf)


def count(channel: discord.VoiceChannel, me: int | None) -> int:
    """The members in channel who can hear: not bots, radio's own included, and not
    deafened by themselves or the server. From the channel's voice states, so a
    member the cache doesn't hold still counts unless a bot."""
    n = 0
    for user_id, state in channel.voice_states.items():
        if user_id == me or state.self_deaf or state.deaf:
            continue
        member = channel.guild.get_member(user_id)
        if member is not None and member.bot:
            continue
        n += 1
    return n


class Channel:
    """A discord.py voice channel as radio.discord_sink's VoiceChannel."""

    def __init__(self, channel: discord.VoiceChannel) -> None:
        self._channel = channel

    @property
    def name(self) -> str:
        return self._channel.name

    def voice_client(self) -> VoiceClient | None:
        return cast(VoiceClient | None, self._channel.guild.voice_client)

    async def connect(self, *, reconnect: bool, self_deaf: bool) -> VoiceClient:
        vc: discord.VoiceClient = await self._channel.connect(
            reconnect=reconnect, self_deaf=self_deaf
        )
        return cast(VoiceClient, vc)


class DiscordGateway:
    """discord.py's client. start logs in, which fails at once on a bad token, and
    runs the gateway in the background, so radio takes callouts meanwhile; a join
    waits for it to be ready."""

    def __init__(
        self, token: Secret, logger: logging.Logger, client: discord.Client | None = None
    ) -> None:
        self._token = token
        self.logger = logger
        self.client = client or discord.Client(intents=intents())
        self._running: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # login must come first: it sets up what connect and wait_until_ready use.
        await self.client.login(self._token.reveal())
        self._running = asyncio.create_task(self.client.connect(reconnect=True), name="discord")
        self._running.add_done_callback(self._stopped)
        self.logger.info("discord: logged in")

    def _stopped(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (err := task.exception()) is not None:
            self.logger.error(
                "discord: gateway stopped", extra={"err": str(err) or type(err).__name__}
            )

    async def ready(self) -> None:
        """Wait until the gateway is ready; raise if it stopped instead."""
        assert self._running is not None, "start first"
        ready = asyncio.ensure_future(self.client.wait_until_ready())
        try:
            await asyncio.wait({ready, self._running}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
        if not self.client.is_ready():
            raise RuntimeError("discord gateway stopped")

    async def close(self) -> None:
        await self.client.close()
        if self._running is not None:
            self._running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._running

    async def channel(self, channel_id: int) -> VoiceChannel:
        await self.ready()
        ch = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        if not isinstance(ch, discord.VoiceChannel):
            raise RuntimeError(f"channel {channel_id} is not a voice channel")
        return Channel(ch)

    def watch(
        self, channel_id: int, changed: Callable[[str | None, Seat, Seat, int], None]
    ) -> None:
        client = self.client

        async def on_voice_state_update(
            member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
        ) -> None:
            me = client.user.id if client.user is not None else None
            b, a = seat(before, channel_id), seat(after, channel_id)
            if HERE not in (b.where, a.where):
                return
            ch = client.get_channel(channel_id)
            n = count(ch, me) if isinstance(ch, discord.VoiceChannel) else 0
            # radio's own join or leave says only the count.
            changed(None if member.id == me else member.display_name, b, a, n)

        client.event(on_voice_state_update)

    def listeners(self, channel_id: int) -> int | None:
        client = self.client
        ch = client.get_channel(channel_id)
        if not isinstance(ch, discord.VoiceChannel):
            return None
        return count(ch, client.user.id if client.user is not None else None)

    def source(self, path: Path) -> AudioSource:
        # ffmpeg resamples Piper's WAV to Discord's 48 kHz stereo, as in v1.
        return discord.FFmpegPCMAudio(str(path))


def open_gateway(settings: Settings, logger: logging.Logger) -> DiscordGateway:
    assert settings.discord_token is not None
    return DiscordGateway(settings.discord_token, logger)
