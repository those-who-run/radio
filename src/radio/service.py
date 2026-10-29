"""radio's service: serve GET /healthz, connect to NATS, log "ready", and run until
it is told to stop, then drain the connection and close the server.

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

from nats.aio.client import Client

from radio.health import HealthServer
from radio.settings import Settings

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
    def __init__(self, settings: Settings, version: str, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.nc: Client | None = None
        # Whether the first connection was made: before it, a failed attempt is an
        # error, not a disconnect.
        self.was_connected = False
        self.health = HealthServer(
            settings.health_host, settings.health_port, version, self.connected, logger
        )

    def connected(self) -> bool:
        return self.nc is not None and self.nc.is_connected

    async def _disconnected(self) -> None:
        if self.was_connected:
            self.logger.warning("nats disconnected")

    async def _reconnected(self) -> None:
        self.logger.info("nats reconnected")

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

    async def run(self, stop: asyncio.Event) -> None:
        """Serve and connect until stop is set, then shut down cleanly."""
        await self.health.start()
        try:
            if await self.connect(stop):
                self.logger.info("ready", extra={"nats": redacted(self.settings.nats_url)})
                await stop.wait()
        finally:
            self.logger.info("shutting down")
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
