"""radio.gateway against a stub discord.py client: no network."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import discord
import pytest
from helpers import TOKEN, lines

from radio.gateway import Channel, DiscordGateway, intents, open_gateway
from radio.settings import Secret, Settings


class StubClient:
    """discord.py's ordering: connect and wait_until_ready need login first."""

    def __init__(self, channel: object = None, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.channel = channel
        self.fail = fail
        self._logged_in = False
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()

    async def login(self, token: str) -> None:
        self.calls.append(("login", token))
        self._logged_in = True

    async def connect(self, reconnect: bool = True) -> None:
        self.calls.append(("connect", reconnect))
        assert self._logged_in, "connect before login"
        if self.fail is not None:
            raise self.fail
        self._ready.set()
        await self._closed.wait()

    async def wait_until_ready(self) -> None:
        assert self._logged_in, "wait_until_ready before login"
        await self._ready.wait()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def close(self) -> None:
        self.calls.append(("close", None))
        self._closed.set()

    def get_channel(self, channel_id: int) -> object:
        return self.channel

    async def fetch_channel(self, channel_id: int) -> object:
        return self.channel


class StubVoiceChannel(discord.VoiceChannel):
    """Just enough of a VoiceChannel: no discord state behind it."""

    def __init__(self, voice_client: object = None) -> None:
        self.name = "pit-lane"
        self.guild = type("Guild", (), {"voice_client": voice_client})()
        self.connects: list[dict[str, Any]] = []

    async def connect(self, **kw: Any) -> Any:
        self.connects.append(kw)
        return "vc"


def gateway(client: StubClient) -> DiscordGateway:
    return DiscordGateway(Secret(TOKEN), logging.getLogger("radio"), client)  # type: ignore[arg-type]


def test_the_intents_are_guilds_and_voice_states_only() -> None:
    # R1: no message content, nothing privileged.
    i = intents()
    assert i == discord.Intents(guilds=True, voice_states=True)
    assert not i.message_content and not i.members and not i.presences


def test_the_real_client_asks_for_those_intents() -> None:
    g = open_gateway(Settings(discord_token=Secret(TOKEN)), logging.getLogger("radio"))
    assert g.client.intents == intents()


async def test_logs_in_then_runs_the_gateway(log: io.StringIO) -> None:
    channel = StubVoiceChannel()
    client = StubClient(channel)
    g = gateway(client)
    await g.start()
    got = await asyncio.wait_for(g.channel(1), 2)
    assert client.calls[:2] == [("login", TOKEN), ("connect", True)]
    assert isinstance(got, Channel) and got.name == "pit-lane"
    assert got.voice_client() is None
    vc: object = await got.connect(reconnect=True, self_deaf=True)
    assert vc == "vc"
    assert channel.connects == [{"reconnect": True, "self_deaf": True}]
    await g.close()
    assert ("close", None) in client.calls
    assert TOKEN not in log.getvalue()


async def test_a_channel_that_is_not_voice_is_refused() -> None:
    g = gateway(StubClient(channel=object()))
    await g.start()
    with pytest.raises(RuntimeError, match="not a voice channel"):
        await g.channel(1)
    await g.close()


async def test_a_gateway_that_stops_fails_the_join(log: io.StringIO) -> None:
    g = gateway(StubClient(fail=RuntimeError("privileged intents")))
    await g.start()
    with pytest.raises(RuntimeError, match="gateway stopped"):
        await asyncio.wait_for(g.channel(1), 2)
    await until_logged(log, "discord: gateway stopped")
    await g.close()


async def until_logged(log: io.StringIO, msg: str) -> None:
    async with asyncio.timeout(2):
        while msg not in [x["msg"] for x in lines(log)]:
            await asyncio.sleep(0)
