"""What the tests share: a stand-in NATS server, and a GET for /healthz. conftest.py
makes fixtures of them."""

from __future__ import annotations

import asyncio
import io
import json


class FakeNATS:
    """Enough of a NATS server for a client to connect, ping and drain: it sends
    INFO, answers each PING with PONG and ignores the rest."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.clients: list[asyncio.StreamWriter] = []

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"nats://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

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
