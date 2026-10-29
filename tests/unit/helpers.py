"""What the tests share: a stand-in NATS server, a GET for /healthz, a fake voice
with WAV helpers, and a running Callouts. conftest.py makes fixtures of them."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from dataclasses import dataclass
from pathlib import Path

from nats.aio.client import Client
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from radio.callouts import Callouts
from radio.engineer import Roster, from_settings
from radio.phrasing import templates
from radio.settings import Settings

# The CALLOUTS stream as paddock's deploy/nats/streams/callouts.json defines it
# (paddock#73): every source's callouts, a work queue kept 30 s, 64 MiB at most,
# deduplicated on the callout's id for 30 s (the server refuses a duplicate window
# longer than max_age).
CALLOUTS = StreamConfig(
    name="CALLOUTS",
    description=(
        "Callouts from every source for radio, kept 30 s or until acked, "
        "deduplicated on Nats-Msg-Id (paddock#73)."
    ),
    subjects=["twr.v1.*.callout.>"],
    retention=RetentionPolicy.WORK_QUEUE,
    storage=StorageType.FILE,
    discard=DiscardPolicy.OLD,
    max_age=30,
    max_bytes=67108864,
    duplicate_window=30,
    num_replicas=1,
)


class FakeNATS:
    """Enough of a NATS server for a client to connect, ping and drain: it sends
    INFO, answers each PING with PONG and ignores the rest."""

    def __init__(self, port: int = 0) -> None:
        # 0 picks a free port; a port given starts a server where one stopped.
        self.port = port
        self.server: asyncio.Server | None = None
        self.clients: list[asyncio.StreamWriter] = []

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        for w in self.clients:
            w.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.append(writer)
        info = {"server_id": "fake", "version": "2.12.0", "proto": 1, "max_payload": 1048576}
        writer.write(f"INFO {json.dumps(info)}\r\n".encode())
        try:
            while line := await reader.readline():
                if line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


def lines(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


async def request(port: int, raw: bytes) -> tuple[int, dict[str, object]]:
    """Send raw to the health server and return the status code and JSON body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, body = response.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


async def get(port: int, path: str = "/healthz") -> tuple[int, dict[str, object]]:
    return await request(port, f"GET {path} HTTP/1.1\r\nHost: radio\r\n\r\n".encode())


async def until(check: object, timeout: float = 5.0) -> None:
    """Wait until check() is true."""
    assert callable(check)
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


# The fake voice's sample rate: one frame a character makes lengths easy to count.
RATE = 1000


class FakeVoice:
    """A Voice for the tests: one frame of 0x0001 for each character of the sentence,
    mono, 16-bit, at RATE. key names the voice, so two fakes can differ."""

    def __init__(self, key: str = "fake") -> None:
        self.key = key
        self.calls: list[str] = []

    def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
        self.calls.append(text)
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(b"\x01\x00" * len(text))


def write_wav(path: Path, frames: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x01\x00" * frames)
    return path


def frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def roster(script: bool = False) -> Roster:
    """v1's engineer, with the built-in phrases (`script: false`) by default."""
    return Roster([from_settings(Settings(templates=templates(script=script)))])


@dataclass
class Running:
    """A Callouts the `start` fixture runs."""

    nc: Client
    callouts: Callouts[str]
    task: asyncio.Task[None]

    async def kill(self) -> None:
        """Stop it as a dying process would: nothing acked on the way out."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.nc.close()
