"""The callouts radio has taken and not yet spoken, most urgent first (paddock#56).

v1's `CalloutQueue` (callouts.py), with the times ecu sets in place of v1's
in-process ones: a callout waits until its `hold_until`, and one past its
`expires_at` is dropped, not spoken. The order is v1's heap's, `(priority,
created_at, seq)`, with the subject's level in front: p0 before p1 before p2, then
the numeric priority, then the age, then the order the stream holds them in.

The times are compared with `clock()`, UTC now live (paddock#56 R8), so a replay
on a virtual clock (paddock#60) passes its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from twr.events.v1 import callout_pb2
from twr_contracts.callout import LEVELS, Level

Clock = Callable[[], datetime]

# The stage a callout is dropped at, as the log says it (v1's DiscordPlayer._stale).
QUEUED = "queued"
CONNECTING = "connecting"
SYNTHESIS = "synthesis"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Delivery(Protocol):
    """A message's delivery, which radio acks once the callout is spoken or dropped:
    nats-py's Msg."""

    async def ack(self) -> None: ...

    async def ack_sync(self, timeout: float = 1.0) -> object: ...


def _time(m: callout_pb2.Callout, name: str) -> datetime | None:
    if not m.HasField(name):
        return None
    ts = getattr(m, name)
    return ts.ToDatetime(tzinfo=UTC)  # type: ignore[no-any-return]


@dataclass(eq=False)
class Pending:
    """A callout radio has taken: the message, the level it came on, its place in the
    stream and the delivery to ack, with the sentence radio phrased for it."""

    callout: callout_pb2.Callout
    level: Level
    # The stream's sequence: the order it was published in, and the same on every
    # redelivery of it.
    seq: int
    # The latest delivery: a redelivery replaces it.
    delivery: Delivery
    text: str = ""
    created_at: datetime = field(init=False)
    expires_at: datetime | None = field(init=False)
    hold_until: datetime | None = field(init=False)

    def __post_init__(self) -> None:
        self.created_at = _time(self.callout, "created_at") or datetime.fromtimestamp(0, UTC)
        self.expires_at = _time(self.callout, "expires_at")
        self.hold_until = _time(self.callout, "hold_until")

    def stale(self, now: datetime) -> bool:
        """Past its expires_at. Without one it never goes stale (callout.proto)."""
        return self.expires_at is not None and now > self.expires_at

    def due(self, now: datetime) -> bool:
        return self.hold_until is None or self.hold_until <= now

    def left(self, now: datetime) -> float | None:
        """Seconds until it goes stale, None for never."""
        if self.expires_at is None:
            return None
        return max((self.expires_at - now).total_seconds(), 0.0)


class CalloutQueue:
    """The taken callouts, most urgent first. A held one waits in the queue until it
    is due, and more urgent callouts that are due go ahead of it; one past its
    expires_at is handed to drop with the stage QUEUED when the queue comes to it.

    The speaker awaits `get` rather than polling `pop`. Push from the event loop's
    thread only: a push wakes the waiting `get`."""

    def __init__(self, drop: Callable[[Pending, str], Awaitable[None]]) -> None:
        self._drop = drop
        self._heap: list[tuple[int, int, datetime, int, Pending]] = []
        self._order = itertools.count()
        self._wake = asyncio.Event()

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, p: Pending) -> None:
        key = (LEVELS.index(p.level), p.callout.priority, p.created_at, next(self._order))
        heapq.heappush(self._heap, (*key, p))
        self._wake.set()

    def pop(self, now: datetime) -> tuple[Pending | None, list[Pending]]:
        """The most urgent callout that is due and not stale, and the stale ones it
        took out on the way. Callouts still held stay queued."""
        waiting, stale = [], []
        found = None
        while self._heap:
            item = heapq.heappop(self._heap)
            p = item[-1]
            if p.stale(now):
                stale.append(p)
            elif not p.due(now):
                waiting.append(item)
            else:
                found = p
                break
        for item in waiting:
            heapq.heappush(self._heap, item)
        return found, stale

    async def get(self, clock: Clock = utc_now) -> Pending:
        """Wait for the callout `pop` returns next, dropping the stale ones on the
        way. Wakes on a push, and when the soonest held callout falls due, so a held
        callout still comes out on time."""
        while True:
            self._wake.clear()
            now = clock()
            found, stale = self.pop(now)
            for p in stale:
                await self._drop(p, QUEUED)
            if found is not None:
                return found
            # Nothing is due: what is left, if anything, is held.
            due = min(
                (item[-1].hold_until for item in self._heap if item[-1].hold_until), default=None
            )
            timeout = None if due is None else max((due - now).total_seconds(), 0.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
