"""radio.pitlane (paddock#59): what radio takes back at a pit entry, through the
queue on a clock the test moves, and through Callouts on a replay against a real
nats-server -js. ecu applies the pit lane to what it raises in the lane (its
goldens, internal/rules/testdata/callouts), so every callout here was raised
before the entry, or released by ecu at the exit."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.protobuf.message import Message
from helpers import until
from nats.js.client import JetStreamContext
from test_callouts import made, publish
from test_timing import Speaker, Wall, read
from twr.events.v1 import callout_pb2, pit_pb2, race_pb2, session_pb2
from twr_contracts import ce
from twr_contracts.callout import level_of

from radio.pitlane import PitLane
from radio.queue import PIT_LANE, CalloutQueue, Pending
from radio.timing import INSTANT, Timeline, stamp

T0 = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)
S = "s1"

SPEAK = callout_pb2.PIT_LANE_SPEAK
HOLD = callout_pb2.PIT_LANE_HOLD
DROP = callout_pb2.PIT_LANE_DROP

_seq = iter(range(1, 1_000_000))


def t(secs: float) -> datetime:
    return T0 + timedelta(seconds=secs)


class Delivery:
    async def ack(self) -> None:
        return None

    async def ack_sync(self, timeout: float = 1.0) -> object:
        return None


def pending(
    rule: str,
    priority: int,
    lane: callout_pb2.PitLane,
    created: float = 0.0,
    hold: float | None = None,
    session: str = S,
) -> Pending:
    m = callout_pb2.Callout(rule=rule, priority=priority, pit_lane=lane, session_id=session)
    m.id = f"{rule}-{next(_seq)}"
    m.created_at.FromDatetime(t(created))
    m.expires_at.FromDatetime(t(created + 8))
    if hold is not None:
        m.hold_until.FromDatetime(t(created + hold))
    return Pending(m, level_of(priority), next(_seq), Delivery())


async def _never(p: Pending, stage: str) -> None:
    raise AssertionError("the queue's own drop is not called by pop")


def queue() -> tuple[PitLane, CalloutQueue]:
    pits = PitLane(logging.getLogger("radio"))
    return pits, CalloutQueue(_never, pits)


def test_a_position_call_held_over_the_entry_is_dropped() -> None:
    # R2, SUGO 2026-10-18: a place lost 0.7 s before the entry, held 1 s, is still
    # queued at the entry and dropped there.
    pits, q = queue()
    q.push(pending("position", 12, DROP, hold=1))
    assert q.pop(t(0.5)) == (None, [])
    pits.entered(S, 1, t(0.7))
    found, dropped = q.pop(t(1))
    assert found is None
    assert [(p.callout.rule, stage) for p, stage in dropped] == [("position", PIT_LANE)]
    assert len(q) == 0


def test_said_before_the_entry_it_is_not_taken_back() -> None:
    pits, q = queue()
    q.push(pending("position", 12, DROP, hold=1))
    found, _ = q.pop(t(1))
    pits.entered(S, 1, t(1.5))
    assert found is not None and found.callout.rule == "position"


def test_in_the_lane_position_is_dropped_and_fuel_passes() -> None:
    # AC6 (those-who-run/those-who-run#179 AC1), for what radio still had queued.
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD))  # Behind the other two.
    q.push(pending("position", 12, DROP))
    q.push(pending("fuel", 15, SPEAK))
    pits.entered(S, 1, t(0.1))
    found, dropped = q.pop(t(1))
    assert found is not None and found.callout.rule == "fuel"
    assert [p.callout.rule for p, _ in dropped] == ["position"]
    # The lap call is held outside the queue for the exit.
    assert q.pop(t(1)) == (None, [])
    assert len(q) == 0


def test_a_held_callout_is_released_at_the_exit_as_raised_then() -> None:
    # R4: the time in the lane doesn't age it, and its hold no longer applies.
    pits, q = queue()
    q.push(pending("final_lap", 11, HOLD, hold=2))
    pits.entered(S, 1, t(1))
    assert q.pop(t(2)) == (None, [])
    [p] = pits.left(S, 1, t(40))
    assert (p.created_at, p.expires_at, p.hold_until) == (t(40), t(48), None)
    q.push(p)
    found, _ = q.pop(t(40))
    assert found is p
    # Released once: the exit sent again releases nothing.
    assert pits.left(S, 1, t(40)) == []


def test_a_withdrawn_entry_and_the_flag_release_what_is_held() -> None:
    # AC4: a PitTeleport withdrawing the entry releases the held callouts (R6),
    # and so does a race that finishes in the lane (R5).
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD))
    pits.entered(S, 1, t(1))
    q.pop(t(1))
    [p] = pits.left(S, 1, t(3))  # The teleport.
    assert p.created_at == t(3)
    q.push(pending("temps", 14, HOLD, created=4))
    pits.entered(S, 2, t(5))
    q.pop(t(5))
    [p] = pits.finished(S, t(9))
    assert (p.callout.rule, p.created_at) == ("temps", t(9))


def test_after_the_flag_an_entry_releases_at_once() -> None:
    pits, q = queue()
    pits.finished(S, t(0))
    q.push(pending("lap_time", 20, HOLD, created=1))
    pits.entered(S, 1, t(2))
    found, _ = q.pop(t(2))
    assert found is not None and found.created_at == t(2)


def test_a_session_ended_in_the_lane_drops_what_is_held() -> None:
    # AC4, R7: no flag, no exit: what was held is dropped, as ecu drops its own.
    # Everything queued at the entry is taken, held or not yet due.
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD))
    q.push(pending("final_lap", 11, HOLD, hold=5))
    pits.entered(S, 1, t(1))
    assert q.pop(t(1)) == (None, [])
    assert sorted(p.callout.rule for p in pits.ended(S)) == ["final_lap", "lap_time"]
    # One raised before the entry that comes to the speaker after the end is
    # dropped too, and the lane can't be left any more.
    q.push(pending("temps", 14, HOLD, created=0.5))
    _, dropped = q.pop(t(6))
    assert [(p.callout.rule, stage) for p, stage in dropped] == [("temps", PIT_LANE)]
    assert pits.left(S, 1, t(7)) == []


def test_the_entry_and_exit_known_before_the_speaker_comes_to_it() -> None:
    # R8 by the times, not the arrival: both events reached radio before the
    # replay's speaker came to the callout. Behind the exit it waits for it; past
    # it, it is said as raised at the exit.
    pits, q = queue()
    q.push(pending("final_lap", 11, HOLD, hold=2))
    pits.entered(S, 1, t(1))
    pits.left(S, 1, t(30))
    assert q.pop(t(2), replay=True) == (None, [])
    assert len(q) == 1
    found, _ = q.pop(t(30), replay=True)
    assert found is not None and (found.created_at, found.expires_at) == (t(30), t(38))


def test_a_callout_that_reaches_radio_after_the_entry_is_still_taken_back() -> None:
    # AC5, reworded: raised before entered_at, it reaches radio after the
    # PitEntered (CALLOUTS and EVENTS aren't ordered), and is still taken back by
    # its pit_lane.
    pits, q = queue()
    pits.entered(S, 1, t(0.7))
    q.push(pending("position", 12, DROP, created=0.5))
    q.push(pending("lap_time", 20, HOLD, created=0.6))
    q.push(pending("fuel", 15, SPEAK, created=0.6))
    found, dropped = q.pop(t(1))
    assert found is not None and found.callout.rule == "fuel"
    assert [(p.callout.rule, stage) for p, stage in dropped] == [("position", PIT_LANE)]
    assert q.pop(t(1)) == (None, [])
    [p] = pits.left(S, 1, t(30))
    assert (p.callout.rule, p.created_at) == ("lap_time", t(30))


def test_what_ecu_raised_in_the_lane_or_released_is_not_taken_back() -> None:
    # Created at or after the entry: ecu applied the lane to it already.
    pits, q = queue()
    pits.entered(S, 1, t(1))
    q.push(pending("fuel", 15, SPEAK, created=1))
    q.push(pending("lap_time", 20, HOLD, created=1))  # Released by ecu on the entry frame.
    assert [p.callout.rule for p in iter(lambda: q.pop(t(2))[0], None)] == ["fuel", "lap_time"]


def test_another_session_and_a_later_entry() -> None:
    pits, q = queue()
    q.push(pending("lap_time", 20, HOLD, session="other"))
    pits.entered(S, 1, t(1))
    pits.entered(S, 1, t(9))  # Sent again: the same entry.
    found, _ = q.pop(t(2))
    assert found is not None and found.callout.session_id == "other"
    # Released at one exit and caught again by the next entry, as v1's queue.
    q.push(pending("final_lap", 11, HOLD, created=2, hold=10))
    pits.entered(S, 2, t(5))
    pits.left(S, 2, t(6))
    pits.entered(S, 3, t(7))
    assert q.pop(t(8)) == (None, [])
    [p] = pits.left(S, 3, t(20))
    assert p.created_at == t(20)


# Through Callouts, on a replay.


async def event(nc: Any, what: str, m: Message, at: datetime) -> None:
    headers, data = ce.encode(m, ce.Event(id=f"e{next(_seq)}", source="/twr/ecu/gt7", time=at))
    await nc.publish(f"twr.v1.test.evt.{S}.{what}", data, headers=headers)


async def test_a_stop_on_a_replay_says_what_v1_said(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # AC1's part that is radio's, synthetic: a place lost 0.7 s before the entry
    # is dropped; the final lap call still held at the entry is said at the exit,
    # before the in-lap call ecu held and released there; a fuel call in the lane
    # is said in it. The events reach radio before the callouts, as a replay
    # delivers them.
    wall = Wall()
    path = tmp_path / "timeline.jsonl"
    timeline = Timeline(path)
    sink = Speaker()
    r = await start(sink, timing=INSTANT, timeline=timeline, replay=wall.clock)
    await until(lambda: r.callouts._events is not None)
    entered = pit_pb2.PitEntered(session_id=S, entry=1)
    entered.entered_at.FromDatetime(t(0.7))
    await event(r.nc, "pit.entered", entered, t(0.7))
    exited = pit_pb2.PitExited(session_id=S, entry=1)
    exited.exited_at.FromDatetime(t(31))
    await event(r.nc, "pit.exited", exited, t(31))
    await r.nc.flush()
    await until(lambda: S in r.callouts.pits._sessions)
    await until(lambda: r.callouts.pits._sessions[S].lanes[1].left is not None)
    sent = []
    for m, lane in [
        (made("position", "template_lost", 12, created=t(0), hold=1, slots={"position": 8}), DROP),
        (made("final_lap", "template", 11, created=t(0.5), hold=2), HOLD),
        (made("fuel", "template_pit", 15, created=t(5)), SPEAK),
        (
            made(
                "lap_time",
                "template_in_lap",
                20,
                created=t(31),
                slots={"lap": 3, "lapTimeMs": 70000},
            ),
            HOLD,
        ),
    ]:
        m.session_id, m.pit_lane = S, lane
        sent.append(m)
        await publish(js, m)
    await until(lambda: r.callouts.phrased == len(sent), timeout=10)
    wall.let_go()
    await until(lambda: len(path.read_text().splitlines()) == len(sent), timeout=10)
    await r.kill()
    timeline.close()
    out = read(path)
    assert [(e["rule"], e["outcome"], e["stage"], e["started"] or e["dropped"]) for e in out] == [
        ("position", "dropped", PIT_LANE, stamp(t(1))),
        ("fuel", "spoken", None, stamp(t(5))),
        ("final_lap", "spoken", None, stamp(t(31))),
        ("lap_time", "spoken", None, stamp(t(31))),
    ]
    assert sink.said == [e["text"] for e in out if e["outcome"] == "spoken"]


async def test_a_session_ended_in_the_lane_drops_through_callouts(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # AC4 through the events: the flag releases, an end without one drops.
    wall = Wall()
    path = tmp_path / "timeline.jsonl"
    timeline = Timeline(path)
    r = await start(Speaker(), timing=INSTANT, timeline=timeline, replay=wall.clock)
    await until(lambda: r.callouts._events is not None)
    m = made("lap_time", "template_in_lap", 20, created=t(0), hold=5, slots={"lap": 3})
    m.session_id, m.pit_lane = S, HOLD
    await publish(js, m)
    await until(lambda: r.callouts.phrased == 1)
    entered = pit_pb2.PitEntered(session_id=S, entry=1)
    entered.entered_at.FromDatetime(t(1))
    await event(r.nc, "pit.entered", entered, t(1))
    await r.nc.flush()
    await until(lambda: S in r.callouts.pits._sessions)
    wall.let_go()
    await until(lambda: r.callouts.pits._sessions[S].lanes[1].held)
    await event(r.nc, "session.ended", session_pb2.SessionEnded(session_id=S), t(9))
    await until(lambda: path.read_text().strip() != "")
    await r.kill()
    timeline.close()
    [e] = read(path)
    assert (e["outcome"], e["stage"], e["dropped"]) == ("dropped", PIT_LANE, stamp(t(9)))


async def test_the_flag_in_the_lane_releases_through_callouts(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # AC4, R5: a race that finishes in the lane, with no exit, says what was held
    # at the flag.
    wall = Wall()
    path = tmp_path / "timeline.jsonl"
    timeline = Timeline(path)
    r = await start(Speaker(), timing=INSTANT, timeline=timeline, replay=wall.clock)
    await until(lambda: r.callouts._events is not None)
    m = made("final_lap", "template", 11, created=t(0), hold=5)
    m.session_id, m.pit_lane = S, HOLD
    await publish(js, m)
    await until(lambda: r.callouts.phrased == 1)
    entered = pit_pb2.PitEntered(session_id=S, entry=1)
    entered.entered_at.FromDatetime(t(1))
    await event(r.nc, "pit.entered", entered, t(1))
    await until(lambda: S in r.callouts.pits._sessions)
    wall.let_go()
    await until(lambda: r.callouts.pits._sessions[S].lanes[1].held)
    await event(r.nc, "race.finished", race_pb2.RaceFinished(session_id=S), t(12))
    await until(lambda: path.read_text().strip() != "")
    await r.kill()
    timeline.close()
    [e] = read(path)
    assert (e["outcome"], e["created_at"], e["started"]) == ("spoken", stamp(t(12)), stamp(t(12)))


async def test_an_unreadable_event_is_let_be(start: Any, js: JetStreamContext, log: Any) -> None:
    r = await start(Speaker())
    await until(lambda: r.callouts._events is not None)
    await r.nc.publish(f"twr.v1.test.evt.{S}.pit.entered", b"{}", headers={})
    await r.nc.publish(f"twr.v1.test.evt.{S}.lap.completed", b"{}", headers={})
    await until(lambda: "event unreadable" in log.getvalue())
    assert S not in r.callouts.pits._sessions
