"""radio's status (paddock#62, R9): where it speaks, whether voice is up and when it
last spoke, in the `live` KV bucket under `radio.status.<src>` as contracts'
RadioStatus, and the same on /healthz.

The key carries the source radio speaks, so a replay's radio writes its own key and
never the live one. The writer runs as a task of its own, off the speaking path: it
looks at radio's state every CHECK seconds and writes the value when it changed, at
most once every MIN_GAP seconds, so a callout never waits for the bucket and a burst
of changes is one write; and unchanged, every HEARTBEAT seconds, so the value is
never older than 5 s while radio runs (contracts' RadioStatus): the bucket has no
TTL, and a reader takes an older updated_at as radio gone. A write that fails, as
when NATS is away or the bucket isn't there yet, is logged once and tried again
until one succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from nats.aio.client import Client
from nats.js.kv import KeyValue
from twr.state.v1 import live_pb2
from twr_contracts import ce

# The bucket the status goes in (contracts' subjects.yaml; paddock#72).
BUCKET = "live"
# How often the writer looks for a change, and the least time between two writes.
CHECK = 0.1
MIN_GAP = 1.0
# The most time between two writes, under RadioStatus's 5 s.
HEARTBEAT = 4.0
# How long the writer waits before it tries a failed write again.
RETRY_WAIT = 2.0

SINKS = {
    "console": live_pb2.RADIO_SINK_CONSOLE,
    "wav": live_pb2.RADIO_SINK_WAV,
    "discord": live_pb2.RADIO_SINK_DISCORD,
}


def key(src: str) -> str:
    """The source's key: radio.status.<src>."""
    return f"radio.status.{src}"


class Status:
    """radio's status, read from what radio holds each time it is asked."""

    def __init__(
        self,
        src: str,
        sink: str,
        voice_connected: Callable[[], bool | None],
        voice_listeners: Callable[[], int | None],
        last_spoken_at: Callable[[], datetime | None],
    ) -> None:
        self.src = src
        self.sink = sink
        self._voice_connected = voice_connected
        self._voice_listeners = voice_listeners
        self._last_spoken_at = last_spoken_at

    def value(self) -> live_pb2.RadioStatus:
        """The value as of now, without updated_at."""
        m = live_pb2.RadioStatus(
            source=self.src, sink=SINKS.get(self.sink, live_pb2.RADIO_SINK_UNSPECIFIED)
        )
        if (connected := self._voice_connected()) is not None:
            m.voice_connected = connected
        if (listeners := self._voice_listeners()) is not None:
            m.voice_listeners = listeners
        if (spoken := self._last_spoken_at()) is not None:
            m.last_spoken_at.FromDatetime(spoken)
        return m

    def body(self) -> dict[str, Any]:
        """The value's fields for /healthz: `src`, `sink`, and the voice's and the
        last callout's where known."""
        body: dict[str, Any] = {"src": self.src, "sink": self.sink}
        if (connected := self._voice_connected()) is not None:
            body["voice_connected"] = connected
        if self.sink == "discord":
            body["voice_listeners"] = self._voice_listeners()
        if (spoken := self._last_spoken_at()) is not None:
            body["last_spoken_at"] = spoken.astimezone(UTC).isoformat(timespec="milliseconds")
        return body


class Writer:
    """Writes status to the `live` bucket on change, at most once every MIN_GAP
    seconds, until cancelled."""

    def __init__(
        self,
        status: Status,
        logger: logging.Logger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.status = status
        self.logger = logger
        self.clock = clock
        self.monotonic = monotonic
        self.key = key(status.src)
        # What was last written, without updated_at, and when.
        self.written: live_pb2.RadioStatus | None = None
        self._last_write: float | None = None
        self._failing = False

    async def run(self, nc: Client) -> None:
        kv: KeyValue | None = None
        while True:
            value = self.status.value()
            gap = MIN_GAP if self._last_write is None else self.monotonic() - self._last_write
            due = value != self.written or gap >= HEARTBEAT
            if not due or gap < MIN_GAP:
                await asyncio.sleep(CHECK if not due else MIN_GAP - gap)
                continue
            try:
                if kv is None:
                    kv = await nc.jetstream().key_value(BUCKET)
                await self.put(kv, value)
            except Exception as err:  # NATS away, or the bucket not there yet.
                if not self._failing:
                    self.logger.warning(
                        "status: write",
                        extra={
                            "bucket": BUCKET,
                            "key": self.key,
                            "err": str(err) or type(err).__name__,
                        },
                    )
                self._failing = True
                await asyncio.sleep(RETRY_WAIT)

    async def put(self, kv: KeyValue, value: live_pb2.RadioStatus) -> None:
        """Write value, stamped with updated_at."""
        stamped = live_pb2.RadioStatus()
        stamped.CopyFrom(value)
        now = Timestamp()
        now.FromDatetime(self.clock())
        stamped.updated_at.CopyFrom(now)
        await kv.put(self.key, ce.marshal(stamped))
        self.written = value
        self._last_write = self.monotonic()
        if self._failing:
            self.logger.info("status: written again", extra={"key": self.key})
            self._failing = False
