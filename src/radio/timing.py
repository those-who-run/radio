"""A replay's clock and the timeline of what radio said (paddock#60).

`TWR_RADIO_TIMING` says how long a callout takes:

- `live`: the wall clock, and each callout takes as long as the sink takes to play
  it (paddock#56).
- `instant`: v1's replay. Each callout is said at the time it was created, or its
  hold ends, in the queue's order, and takes no time; nothing goes stale. For tests
  that check what is said.
- `simulated`: a speaker as live, on the replay's clock. One callout at a time,
  each holding the channel for as long as it takes to say, with the live
  staleness and hold rules. For tests of timing: late callouts and expiry.

A replay's clock is the callouts' own times (`ReplayClock`), not the wall clock, so
a replay at 10 times speed gives what one at recorded speed gives. ecu publishes in
the order it raised, so once radio has queued a callout created at t, every callout
created before t is queued too, and the speaker may decide anything up to t. When
nothing comes, the clock creeps on with the wall clock, LAG behind it, so the last
callouts of a replay are said too, and it stands still while radio is taking
callouts, so a long burst can't be overtaken. A replay held up for longer than
LAG between two callouts lets the clock creep past that gap: a replay published
slower than its recorded speed, with gaps longer than LAG where it recorded
shorter ones, can then be overtaken.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from radio.queue import Pending

LIVE = "live"
INSTANT = "instant"
SIMULATED = "simulated"

# How far behind its own times a replay's callouts may reach radio, in seconds: the
# clock creeps on after the last callout only this much later.
LAG = 1.0

# A timeline's outcomes.
SPOKEN = "spoken"
EXPIRED = "expired"
DROPPED = "dropped"


class ReplayClock:
    """The time a replay has reached: the latest `created_at` radio has queued, and
    after it, the wall time since, less LAG. monotonic is the wall clock, which
    the tests stop."""

    def __init__(self, lag: float = LAG, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.lag = lag
        self.monotonic = monotonic
        self.reached: datetime | None = None
        self._since = monotonic()
        self._changed = asyncio.Event()

    def saw(self, t: datetime) -> None:
        """A callout created at t was queued: every one created before is queued."""
        if self.reached is None or t > self.reached:
            self.reached = t
            self._since = self.monotonic()
        self._changed.set()

    def busy(self) -> None:
        """radio is taking callouts it hasn't queued yet: the clock doesn't creep
        meanwhile, so it creeps only over a real stall, never past a callout
        taken and still to be queued."""
        self._since = self.monotonic()

    def poke(self) -> None:
        """Wake a wait to look again, as when the wall clock the tests stop moves."""
        self._changed.set()

    def _left(self, t: datetime) -> float | None:
        """Wall seconds until the clock is past t: 0 or less once it is, None while
        nothing has been queued."""
        if self.reached is None:
            return None
        ahead = (t - self.reached).total_seconds()
        if ahead < 0:
            return ahead
        return ahead + self.lag - (self.monotonic() - self._since)

    def past(self, t: datetime) -> bool:
        """Whether the replay is past t, so nothing created at t or before is still
        to come."""
        left = self._left(t)
        return left is not None and left < 0

    async def wait(self, t: datetime) -> None:
        """Wait until the clock is past t, or something is queued meanwhile."""
        self._changed.clear()
        left = self._left(t)
        if left is not None and left < 0:
            return
        # A hair over, so the wake finds the clock past t.
        timeout = None if left is None else left + 0.001
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)


def stamp(t: datetime | None) -> str | None:
    """RFC 3339 in UTC to the millisecond, as the Go components write times."""
    if t is None:
        return None
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Timeline:
    """Each callout's way through radio, a JSON line each as it is settled
    (paddock#60 R5): when it was queued, started and finished, or that it expired
    or was dropped and at which stage. A restarted radio appends to the file, so
    a run that restarts keeps what it said before."""

    def __init__(self, path: Path | None) -> None:
        self._out: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._out = path.open("a", encoding="utf-8")

    def write(
        self,
        p: Pending,
        outcome: str,
        stage: str | None = None,
        started: datetime | None = None,
        finished: datetime | None = None,
        dropped: datetime | None = None,
    ) -> None:
        if self._out is None:
            return
        c = p.callout
        entry: dict[str, Any] = {
            "callout_id": c.id,
            "rule": c.rule,
            "kind": c.kind,
            "text": p.text,
            "engineer": p.engineer.name if p.engineer is not None else None,
            "at": c.at,
            "created_at": stamp(p.created_at),
            "queued": stamp(p.queued),
            "started": stamp(started),
            "finished": stamp(finished),
            "dropped": stamp(dropped),
            "outcome": outcome,
            "stage": stage,
        }
        self._out.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._out.flush()

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


def later(t: datetime, seconds: float) -> datetime:
    return t + timedelta(seconds=seconds)
