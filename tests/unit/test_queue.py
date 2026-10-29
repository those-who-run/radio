"""radio.queue on a clock the test moves: v1's tests/test_callout_queue.py cases, with
ecu's absolute times in place of v1's ages."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from twr.events.v1 import callout_pb2
from twr_contracts.callout import Level, level_of

from radio.queue import QUEUED, CalloutQueue, Pending

T0 = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)


class Delivery:
    def __init__(self) -> None:
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def move(self, secs: float) -> None:
        self.now += timedelta(seconds=secs)


_seq = iter(range(1, 1_000_000))


def pending(
    rule: str,
    priority: int,
    created: float = 0.0,
    expires: float | None = 8.0,
    hold: float | None = None,
    level: Level | None = None,
) -> Pending:
    m = callout_pb2.Callout(rule=rule, priority=priority)
    m.created_at.FromDatetime(T0 + timedelta(seconds=created))
    if expires is not None:
        m.expires_at.FromDatetime(T0 + timedelta(seconds=created + expires))
    if hold is not None:
        m.hold_until.FromDatetime(T0 + timedelta(seconds=created + hold))
    return Pending(m, level or level_of(priority), next(_seq), Delivery())


class Dropped:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, p: Pending, stage: str) -> None:
        self.calls.append((p.callout.rule, stage))


def rules(q: CalloutQueue, now: datetime) -> list[str]:
    out = []
    while (p := q.pop(now)[0]) is not None:
        out.append(p.callout.rule)
    return out


def test_priority_then_age() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("lap", 20, created=0))
    q.push(pending("fuel", 5, created=1))
    q.push(pending("pos_b", 12, created=2))
    q.push(pending("pos_a", 12, created=1))
    assert rules(q, T0 + timedelta(seconds=3)) == ["fuel", "pos_a", "pos_b", "lap"]


def test_the_level_goes_before_the_priority() -> None:
    # A publisher's priority that disagrees with its subject: the subject wins.
    q = CalloutQueue(Dropped())
    q.push(pending("fuel", 15))
    q.push(pending("odd", 25, level=Level.P0))
    assert rules(q, T0) == ["odd", "fuel"]


def test_equal_priority_and_age_keep_push_order() -> None:
    q = CalloutQueue(Dropped())
    for name in ("a", "b", "c"):
        q.push(pending(name, 15))
    assert rules(q, T0) == ["a", "b", "c"]


def test_stale_callouts_are_dropped() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("old", 5, created=0, expires=8))
    q.push(pending("new", 20, created=9, expires=8))
    found, stale = q.pop(T0 + timedelta(seconds=10))
    assert found is not None and found.callout.rule == "new"
    assert [p.callout.rule for p in stale] == ["old"]


def test_at_expires_at_exactly_it_is_still_said() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("edge", 5, expires=8))
    assert rules(q, T0 + timedelta(seconds=8)) == ["edge"]


def test_without_expires_at_it_never_goes_stale() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("replay", 20, expires=None))
    assert rules(q, T0 + timedelta(hours=3)) == ["replay"]


def test_a_held_callout_waits_and_due_ones_go_ahead() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("position", 12, hold=1.0))
    q.push(pending("lap", 20))
    assert q.pop(T0)[0].callout.rule == "lap"  # type: ignore[union-attr]
    assert q.pop(T0)[0] is None
    assert len(q) == 1
    assert q.pop(T0 + timedelta(seconds=1))[0].callout.rule == "position"  # type: ignore[union-attr]


def test_a_held_callout_can_go_stale() -> None:
    q = CalloutQueue(Dropped())
    q.push(pending("position", 12, hold=5.0, expires=2.0))
    found, stale = q.pop(T0 + timedelta(seconds=3))
    assert found is None and [p.callout.rule for p in stale] == ["position"]


async def test_get_drops_the_stale_and_wakes_on_a_push() -> None:
    dropped = Dropped()
    q = CalloutQueue(dropped)
    clock = Clock()
    q.push(pending("old", 5, expires=1))
    clock.move(2)
    getting = asyncio.create_task(q.get(clock))
    await asyncio.sleep(0.01)
    assert not getting.done()
    assert dropped.calls == [("old", QUEUED)]
    q.push(pending("fuel", 5, created=2))
    p = await asyncio.wait_for(getting, 1)
    assert p.callout.rule == "fuel"


async def test_get_wakes_when_a_held_callout_falls_due() -> None:
    q = CalloutQueue(Dropped())
    real = datetime.now(UTC)
    m = callout_pb2.Callout(rule="position", priority=12)
    m.created_at.FromDatetime(real)
    m.hold_until.FromDatetime(real + timedelta(seconds=0.2))
    q.push(Pending(m, Level.P1, 1, Delivery()))
    p = await asyncio.wait_for(q.get(), 2)
    assert p.callout.rule == "position"
    assert datetime.now(UTC) >= real + timedelta(seconds=0.2)


def test_left() -> None:
    p = pending("x", 5, expires=8)
    assert p.left(T0) == 8.0
    assert p.left(T0 + timedelta(seconds=9)) == 0.0
    assert pending("y", 5, expires=None).left(T0) is None
