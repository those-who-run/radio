"""radio.gateway against a stub discord.py client: no network."""

from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from helpers import TOKEN, lines

from radio.discord_sink import ELSEWHERE, HERE, Seat
from radio.gateway import Channel, DiscordGateway, count, intents, open_gateway, seat
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


def test_discord_voice_encryption_round_trips_on_this_pynacl() -> None:
    # pyproject.toml overrides discord.py's PyNaCl cap (<1.6) with 1.6.2 for
    # GHSA-mrfv-m5wm-5w6w: discord.py's own encrypt path, decrypted with
    # nacl.secret.Aead, so a PyNaCl bump that breaks voice fails here.
    import nacl.secret

    vc = discord.VoiceClient.__new__(discord.VoiceClient)
    key = bytes(range(32))
    # The key comes from the voice connection's state, as discord.py keeps it.
    vc._connection = type("State", (), {"secret_key": list(key)})()
    vc._incr_nonce = 7
    header = bytes(12)
    audio = b"opus frame"
    packet = vc._encrypt_aead_xchacha20_poly1305_rtpsize(header, audio)
    assert packet[:12] == header
    nonce = packet[-4:] + bytes(20)
    plain = nacl.secret.Aead(key).decrypt(packet[12:-4], header, nonce)
    assert plain == audio
    assert vc._incr_nonce == 8


# paddock#61: who is in radio's channel.


class Member:
    def __init__(self, user_id: int, name: str, bot: bool = False) -> None:
        self.id = user_id
        self.display_name = name
        self.bot = bot


def state(channel: object = None, **flags: bool) -> Any:
    return SimpleNamespace(
        channel=channel,
        **{k: flags.get(k, False) for k in ("self_mute", "mute", "self_deaf", "deaf")},
    )


class ListenedChannel(StubVoiceChannel):
    """A channel with members in it, and the guild's member cache."""

    def __init__(self, channel_id: int, states: dict[int, Any], members: list[Member]) -> None:
        super().__init__()
        self.id = channel_id
        self._states = states
        cache = {m.id: m for m in members}
        self.guild = type(
            "Guild", (), {"get_member": staticmethod(cache.get), "voice_client": None}
        )()

    @property
    def voice_states(self) -> dict[int, Any]:
        return self._states


def test_the_count_leaves_out_bots_radio_and_the_deafened() -> None:
    # R2: members who are not bots and not deafened, by themselves or the server;
    # one the cache doesn't hold still counts.
    ch = ListenedChannel(
        7,
        {
            1: state(),
            2: state(self_mute=True),
            3: state(self_deaf=True),
            4: state(deaf=True),
            5: state(),  # another bot
            9: state(self_deaf=True),  # radio
            6: state(),  # not in the cache
        },
        [Member(1, "a"), Member(2, "b"), Member(3, "c"), Member(4, "d"), Member(5, "m", bot=True)],
    )
    assert count(ch, 9) == 3
    assert count(ch, None) == 3


async def test_voice_states_in_the_channel_reach_the_sink(log: io.StringIO) -> None:
    # R1: only states that touch radio's channel, by display name and no id;
    # radio's own with no name, so it only counts.
    ch = ListenedChannel(7, {}, [Member(1, "Ciarán")])
    ch._states[1] = state(ch)
    other = SimpleNamespace(id=8)
    events: dict[str, Any] = {}
    client = StubClient(ch)
    client.user = SimpleNamespace(id=9)  # type: ignore[attr-defined]
    client.event = lambda fn: events.setdefault(fn.__name__, fn)  # type: ignore[attr-defined]
    seen: list[tuple[Any, ...]] = []
    gateway(client).watch(7, lambda *a: seen.append(a))
    on = events["on_voice_state_update"]
    await on(Member(1, "Ciarán"), state(), state(ch))
    await on(Member(1, "Ciarán"), state(other), state(other, self_mute=True))
    await on(Member(9, "radio"), state(), state(ch, self_deaf=True))
    assert [(a[0], a[1].where, a[2].where, a[3]) for a in seen] == [
        ("Ciarán", "nowhere", "here", 1),
        (None, "nowhere", "here", 1),
    ]
    assert seat(state(ch, deaf=True), 7) == Seat(HERE, deaf=True)
    assert seat(state(other), 7).where == ELSEWHERE
