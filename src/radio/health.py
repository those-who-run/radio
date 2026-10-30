"""GET /healthz, on a small asyncio HTTP server of the standard library's parts.

It answers 200 while radio is healthy (`ok`: connected to NATS, and none of its
tasks has stopped) and 503 otherwise, with the same JSON either way, as logbook
does: `status`, `version`, `connected` and `ok`, then radio's details: `idle`
(nothing queued or playing) and its status as the `live` bucket has it
(radio.status: `src`, `sink`, and with the discord sink `voice_connected`, as v1
had, paddock#58 R7, and `voice_listeners`; `last_spoken_at` once it has spoken),
paddock#62 R9. Any other path is 404 and any other method 405. One request a
connection, then it closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from http import HTTPStatus

# A client that is slow to send its request is cut off rather than holding a
# connection; a request line and headers longer than this are refused.
READ_TIMEOUT = 5.0
MAX_HEADER = 8192


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        version: str,
        connected: Callable[[], bool],
        logger: logging.Logger,
        ok: Callable[[], bool] | None = None,
        details: Callable[[], dict[str, object]] = dict,
    ) -> None:
        self._host = host or None  # None listens on every interface.
        self._port = port
        self._version = version
        self._connected = connected
        self._logger = logger
        self._ok = ok or connected
        self._details = details
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The port it listens on, which is the one picked for port 0."""
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        self._logger.info("serving", extra={"addr": f"{self._host or ''}:{self.port}"})

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def body(self) -> tuple[HTTPStatus, dict[str, object]]:
        connected = self._connected()
        ok = connected and self._ok()
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        body: dict[str, object] = {
            "status": "ok" if ok else "unavailable",
            "version": self._version,
            "connected": connected,
            "ok": ok,
        }
        body.update(self._details())
        return status, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
            status, body = self._answer(head)
        except TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            status, body = HTTPStatus.BAD_REQUEST, {"error": "bad request"}
        except ConnectionError:
            writer.close()
            return
        payload = json.dumps(body).encode() + b"\n"
        writer.write(
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + payload
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass  # The client is gone; there is no one left to tell.
        finally:
            writer.close()

    def _answer(self, head: bytes) -> tuple[HTTPStatus, dict[str, object]]:
        if len(head) > MAX_HEADER:
            return HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, {"error": "headers too large"}
        method, _, rest = head.split(b"\r\n", 1)[0].decode("latin-1").partition(" ")
        path = rest.split(" ", 1)[0].split("?", 1)[0]
        if path != "/healthz":
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if method not in ("GET", "HEAD"):
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
        return self.body()
