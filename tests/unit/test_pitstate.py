"""radio reads ecu's pit state (paddock#325): `ecu.pit.<src>` in the `live` bucket,
at start and when the NATS connection comes back, so a radio started in the pit
lane knows it. Through PitLane and the queue on a clock the test moves, through
Callouts against a fake bucket, and through Callouts against a real
nats-server -js."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from helpers import lines, roster, until
from nats.js.client import JetStreamContext
from nats.js.errors import KeyNotFoundError, NotFoundError
from nats.js.kv import KeyValue
from test_callouts import Recorder, made, publish
from test_pitlane import DROP, HOLD, SPEAK, S, event, pending, queue, t
from twr.events.v1 import pit_pb2
from twr.state.v1 import live_pb2
from twr_contracts import ce

from radio.callouts import Callouts
from radio.pitlane import FROM_STATE
from radio.queue import PIT_LANE, QUEUED

# In the PitLane tests, ecu's pit state is read at READ, before the stop's exit.
READ = t(10)


def test_in_the_lane_at_start_a_hold_waits_for_the_exit_and_a_drop_is_dropped() -> None:
    # AC1, R1: queued before the start, as a restarted radio takes them again.
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD, created=5))
    q.push(pending("position", 12, DROP, created=5))
    q.push(pending("fuel", 15, SPEAK, created=5))
    assert pits.from_state(S, 0, READ, pits.events)
    found, dropped = q.pop(t(11))
    assert found is not None and found.callout.rule == "fuel"
    assert [(p.callout.rule, stage) for p, stage in dropped] == [("position", PIT_LANE)]
    assert q.pop(t(11)) == (None, [])
    # The exit of the entry radio never saw ends the lane (R3), and releases the
    # lap call as raised then (paddock#59 R4).
    [p] = pits.left(S, 1, t(30))
    assert (p.callout.rule, p.created_at) == ("lap_time", t(30))
    assert pits.left(S, 1, t(31)) == []


def test_a_withdrawn_entry_ends_the_lane_read_at_start() -> None:
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD, created=5))
    assert pits.from_state(S, 2, READ, pits.events)
    assert q.pop(t(11)) == (None, [])
    [p] = pits.left(S, 3, t(12), exit=False)
    assert p.created_at == t(12)


def test_the_entry_event_takes_over_the_lane_read() -> None:
    # The PitEntered that was on its way: its time is ecu's, and what the read
    # lane held is held for its exit.
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD, created=5))
    assert pits.from_state(S, 0, READ, pits.events)
    assert q.pop(t(11)) == (None, [])
    pits.entered(S, 1, t(9))
    assert FROM_STATE not in pits._sessions[S].lanes
    assert pits._sessions[S].lanes[1].entered == t(9)
    [p] = pits.left(S, 1, t(30))
    assert p.callout.rule == "lap_time"


def test_the_events_win_over_the_read() -> None:
    # R3: an event during the read, an exit the key doesn't count yet, a session
    # over, or a lane radio knows already: the read changes nothing.
    pits, _ = queue()
    before = pits.events
    pits.entered("other", 1, t(1))
    assert not pits.from_state(S, 0, READ, before)
    pits.entered(S, 1, t(1))
    pits.left(S, 1, t(2))
    assert not pits.from_state(S, 0, READ, pits.events)
    pits.ended("ended")
    assert not pits.from_state("ended", 0, READ, pits.events)
    pits.entered(S, 2, t(3))
    assert not pits.from_state(S, 1, READ, pits.events)
    assert FROM_STATE not in pits._sessions[S].lanes


def test_a_key_from_another_session_changes_nothing_for_this_one() -> None:
    # AC2, R3: the lane read is the key's session's, which this one's callouts
    # never look at.
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD, created=5))
    q.push(pending("position", 12, DROP, created=5))
    assert pits.from_state("old", 0, READ, pits.events)
    assert [p.callout.rule for p in iter(lambda: q.pop(t(11))[0], None)] == [
        "position",
        "lap_time",
    ]


def test_an_expired_callout_is_dropped_as_expired_whatever_the_lane() -> None:
    # A lane whose end never comes (a key a crashed ecu left, its session's end
    # missed): a callout past its expires_at is dropped as expired, not held, so
    # it is acked and radio can go idle. One still fresh is held as before.
    pits, q = queue()
    assert pits.from_state("crashed", 0, READ, pits.events)
    q.push(pending("lap_time", 20, HOLD, created=1, session="crashed"))  # Expires at 9.
    q.push(pending("position", 12, DROP, created=1, session="crashed"))
    q.push(pending("final_lap", 11, HOLD, created=5, session="crashed"))  # Expires at 13.
    found, dropped = q.pop(t(11))
    assert found is None
    assert sorted((p.callout.rule, stage) for p, stage in dropped) == [
        ("lap_time", QUEUED),
        ("position", QUEUED),
    ]
    assert [p.callout.rule for p in pits._sessions["crashed"].lanes[FROM_STATE].held] == [
        "final_lap"
    ]
    assert len(q) == 0


async def test_an_expired_callout_in_a_lane_that_never_ends_lets_radio_go_idle(
    log: io.StringIO,
) -> None:
    # Live, through Callouts: the speaker drops it as expired and acks it.
    c = callouts(FakeKV(live_pb2.PitState(session_id="crashed", in_pit_lane=True)), now=t(11))
    await c.read_pit("at start")
    p = pending("lap_time", 20, HOLD, created=1, session="crashed")
    c._held[p.seq] = p
    c.queue.push(p)
    assert not c.idle
    speaking = asyncio.ensure_future(c._speak())
    await until(lambda: c.idle)
    speaking.cancel()
    assert c.dropped == {QUEUED: 1}
    [line] = [x for x in lines(log) if x["msg"] == "callout dropped"]
    assert line["stage"] == QUEUED


# Through Callouts, against a fake bucket.


class FakeKV:
    """A `live` bucket holding at most ecu's pit state, or failing."""

    def __init__(self, state: live_pb2.PitState | None = None, fail: bool = False) -> None:
        self.state = state
        self.fail = fail
        self.keys: list[str] = []

    async def get(self, key: str) -> KeyValue.Entry:
        self.keys.append(key)
        if self.fail:
            raise ConnectionError("nats away")
        if self.state is None:
            raise KeyNotFoundError
        return KeyValue.Entry("live", key, ce.marshal(self.state), 1, None, None, None)


def callouts(kv: FakeKV, now: datetime = READ) -> Callouts[str]:
    c: Callouts[str] = Callouts(
        None,  # type: ignore[arg-type]
        "test",
        roster(),
        Recorder(),
        logging.getLogger("radio"),
        clock=lambda: now,
    )
    c.kv = kv  # type: ignore[assignment]
    return c


def said(log: io.StringIO) -> list[dict[str, object]]:
    return [x for x in lines(log) if str(x["msg"]).startswith("pit lane ")]


async def test_in_the_lane_at_start_the_lane_opens_and_the_read_is_logged(
    log: io.StringIO,
) -> None:
    # R1, R4.
    kv = FakeKV(live_pb2.PitState(session_id=S, in_pit_lane=True, stint=2, stops=1))
    c = callouts(kv)
    await c.read_pit("at start")
    assert kv.keys == ["ecu.pit.test"]
    assert c.pits._sessions[S].lanes[FROM_STATE].entered == READ
    [line] = said(log)
    assert (line["msg"], line["session_id"], line["applied"]) == ("pit lane at start: in", S, True)
    assert line["level"] == "INFO"


@pytest.mark.parametrize(
    "state",
    [None, live_pb2.PitState(session_id=S, in_pit_lane=False, stint=2, stops=1)],
    ids=["no key", "out"],
)
async def test_no_key_or_out_of_the_lane_changes_nothing(
    log: io.StringIO, state: live_pb2.PitState | None
) -> None:
    # AC2, R2.
    c = callouts(FakeKV(state))
    await c.read_pit("at start")
    assert c.pits._sessions == {}
    [line] = said(log)
    assert line["msg"] == "pit lane at start: out"


async def test_a_read_that_fails_changes_nothing(log: io.StringIO) -> None:
    c = callouts(FakeKV(fail=True))
    await c.read_pit("at start")
    assert c.pits._sessions == {}
    [line] = said(log)
    assert (line["msg"], line["err"]) == ("pit lane at start: not read", "nats away")


async def test_the_connection_back_during_a_stop_picks_up_the_lane(log: io.StringIO) -> None:
    # AC3: the entry came while NATS was away; the lap call queued before it is
    # held when the speaker comes to it, and said at the exit.
    kv = FakeKV()
    c = callouts(kv)
    c.reconnected()
    await asyncio.sleep(0)
    assert kv.keys == []  # Not subscribed yet: the start's read is to come.
    c._events = object()  # type: ignore[assignment]
    hold = pending("lap_time", 20, HOLD, created=5)
    c.queue.push(hold)
    kv.state = live_pb2.PitState(session_id=S, in_pit_lane=True, stint=1)
    c.reconnected()
    assert c._reading is not None
    await c._reading
    assert c.queue.pop(t(11)) == (None, [])
    assert c.pits.left(S, 1, t(30)) == [hold]
    [line] = said(log)
    assert line["msg"] == "pit lane after reconnect: in"


# Through Callouts, against a real nats-server -js.


@pytest.fixture
async def bucket(js: JetStreamContext) -> AsyncIterator[KeyValue]:
    """The `live` bucket, made afresh, and gone after the test: the other tests'
    radios read it at start."""
    with contextlib.suppress(NotFoundError):
        await js.delete_key_value("live")
    yield await js.create_key_value(bucket="live")
    await js.delete_key_value("live")


async def test_a_radio_started_in_the_lane_holds_until_the_exit(
    start: Any, js: JetStreamContext, bucket: KeyValue, log: io.StringIO
) -> None:
    # AC1 live: ecu's pit state says in the lane before radio starts. The lap
    # call and the place lost, raised before the entry, are taken back; the fuel
    # call is said; the exit releases the lap call.
    state = live_pb2.PitState(session_id=S, in_pit_lane=True, stint=1)
    await bucket.put("ecu.pit.test", ce.marshal(state))
    before = datetime.now(UTC) - timedelta(seconds=1)
    sent = []
    for m, lane in [
        (made("lap_time", "template_in_lap", 20, created=before, slots={"lap": 3}), HOLD),
        (made("position", "template_lost", 12, created=before, slots={"position": 8}), DROP),
        (made("fuel", "template_pit", 15, created=before), SPEAK),
    ]:
        m.session_id, m.pit_lane = S, lane
        sent.append(m)
        await publish(js, m)
    sink = Recorder()
    r = await start(sink)
    await until(lambda: "pit lane at start: in" in [x["msg"] for x in lines(log)])
    await until(lambda: r.callouts.dropped[PIT_LANE] == 1 and len(sink.said) == 1)
    await until(lambda: bool(r.callouts.pits._sessions[S].lanes[FROM_STATE].held))
    exited = pit_pb2.PitExited(session_id=S, entry=1)
    exited.exited_at.FromDatetime(datetime.now(UTC))
    await event(r.nc, "pit.exited", exited, datetime.now(UTC))
    await until(lambda: len(sink.said) == 2)
    played = [x["rule"] for x in lines(log) if str(x["msg"]).startswith("played ")]
    assert played == ["fuel", "lap_time"]
