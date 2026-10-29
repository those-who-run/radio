"""radio.callouts against a real nats-server -js with the CALLOUTS stream as the
catalogue defines it (paddock#56): the callouts are published as ecu publishes
them, with contracts' ce headers on contracts' subjects."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import nats
import pytest
from google.protobuf import json_format
from helpers import lines, until
from nats.aio.client import Client
from nats.js.client import JetStreamContext
from natsserver import NATSServer
from twr.events.v1 import callout_pb2
from twr_contracts import callout, ce

from radio import callouts
from radio.callouts import Callouts
from radio.phrases import Phrases
from radio.phrasing import templates
from radio.queue import Pending

CONTRACTS = Path(__file__).parent / "testdata" / "contracts"
SRC = "test"

_ids = iter(range(1, 1_000_000))


def made(
    rule: str = "fuel",
    kind: str = "template_pit",
    priority: int = 15,
    *,
    at: int = 0,
    run_id: str = "01RUN",
    slots: dict[str, Any] | None = None,
    text: str = "",
    created: datetime | None = None,
    expires_in: float | None = 8.0,
    hold: float | None = None,
) -> callout_pb2.Callout:
    """A callout raised now, as ecu would raise it."""
    created = created or datetime.now(UTC)
    m = callout_pb2.Callout(
        id=f"c{next(_ids)}", rule=rule, kind=kind, text=text, priority=priority, at=at
    )
    m.run_id = run_id
    if slots:
        json_format.ParseDict(slots, m.slots)
    m.created_at.FromDatetime(created)
    if expires_in is not None:
        m.expires_at.FromDatetime(created + timedelta(seconds=expires_in))
    if hold is not None:
        m.hold_until.FromDatetime(created + timedelta(seconds=hold))
    return m


def example(name: str) -> callout_pb2.Callout:
    """One of contracts' examples, its times moved to now, the same lengths apart."""
    m = callout_pb2.Callout()
    json_format.Parse((CONTRACTS / f"callout_{name}.json").read_text(), m)
    shift = datetime.now(UTC) - m.created_at.ToDatetime(tzinfo=UTC)
    for name in ("created_at", "expires_at", "hold_until"):
        if m.HasField(name):
            ts = getattr(m, name)
            ts.FromDatetime(ts.ToDatetime(tzinfo=UTC) + shift)
    m.id = f"{m.id}-{next(_ids)}"
    return m


async def publish(js: JetStreamContext, m: callout_pb2.Callout, src: str = SRC) -> None:
    headers, data = ce.encode(m, ce.Event(id=m.id, source="/twr/ecu/gt7", time=datetime.now(UTC)))
    await js.publish(callout.subject(src, m), data, headers=headers)


@dataclass
class Recorder:
    """A sink that records what it would say. connect waits for `connected`, prepare
    takes `synthesis` seconds, play takes `playing` seconds and raises for a text in
    `fail`."""

    connected: asyncio.Event = field(default_factory=asyncio.Event)
    synthesis: float = 0.0
    playing: float = 0.0
    fail: set[str] = field(default_factory=set)
    said: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    released: int = 0
    can_connect: bool = True

    def __post_init__(self) -> None:
        self.connected.set()

    async def connect(self) -> bool:
        await self.connected.wait()
        return self.can_connect

    async def prepare(self, text: str) -> str | None:
        await asyncio.sleep(self.synthesis)
        return text or None

    async def play(self, clip: str, callout: Pending) -> None:
        self.started.append(clip)
        if clip in self.fail:
            raise RuntimeError("the sink broke")
        await asyncio.sleep(self.playing)
        self.said.append(clip)

    def release(self, clip: str) -> None:
        self.released += 1


@dataclass
class Running:
    nc: Client
    callouts: Callouts[str]
    task: asyncio.Task[None]

    async def kill(self) -> None:
        """Stop it as a dying process would: nothing acked on the way out."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.nc.close()


@pytest.fixture
async def start(nats_server: NATSServer, js: JetStreamContext) -> AsyncIterator[Callable[..., Any]]:
    running: list[Running] = []

    async def start(sink: Recorder, phrases: Phrases | None = None, src: str = SRC) -> Running:
        nc = await nats.connect(nats_server.url)
        c = Callouts(
            nc, src, phrases or Phrases(templates(script=False)), sink, logging.getLogger("radio")
        )
        r = Running(nc, c, asyncio.create_task(c.run()))
        running.append(r)
        return r

    yield start
    for r in running:
        if not r.nc.is_closed:
            await r.kill()


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(callouts, "ACK_WAIT", 1.0)
    monkeypatch.setattr(callouts, "REFETCH", 0.2)
    monkeypatch.setattr(callouts, "RETRY_WAIT", 0.1)


async def stream_empty(js: JetStreamContext) -> bool:
    return (await js.stream_info("CALLOUTS")).state.messages == 0


async def wait_empty(js: JetStreamContext, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await stream_empty(js):
            await asyncio.sleep(0.05)


def drops(log: io.StringIO) -> list[tuple[object, object]]:
    return [(x["stage"], x["text"]) for x in lines(log) if x["msg"] == "callout dropped"]


async def test_the_consumers_are_one_per_level(start: Any, js: JetStreamContext) -> None:
    # R1: durable pull consumers, one per level subject, named radio-<src>-p<n>.
    r = await start(Recorder())
    await until(lambda: r.callouts._subs)
    for level in callout.LEVELS:
        info = await js.consumer_info("CALLOUTS", f"radio-{SRC}-{level}")
        assert info.config.durable_name == f"radio-{SRC}-{level}"
        assert info.config.filter_subject == f"twr.v1.{SRC}.callout.{level}"
        assert info.config.ack_wait == 1.0


async def test_p0_first_then_priority(start: Any, js: JetStreamContext, log: io.StringIO) -> None:
    # AC1: a p2 lap call and a p0 fuel call published in that order, both due: p0
    # first. Two p1 callouts with priorities 12 and 11: 11 first.
    lap = made("lap_time", "delta_first", 20, at=1, slots={"lap": 2, "lapTimeMs": 64611})
    fuel = made("fuel", "template_critical", 5, at=2)
    twelve = made("position", "template_lost", 12, at=3, slots={"position": 12})
    eleven = made("final_lap", "template", 11, at=4)
    for m in (lap, fuel, twelve, eleven):
        await publish(js, m)
    sink = Recorder()
    await start(sink)
    await until(lambda: len(sink.said) == 4)
    assert sink.said == [
        "Fuel critical. Less than one lap.",
        "Final lap.",
        "Down to twelfth.",
        "Lap two. one oh four point six.",
    ]
    await wait_empty(js)


async def test_a_held_callout_waits_and_a_due_one_goes_ahead(
    start: Any, js: JetStreamContext
) -> None:
    # AC2: hold_until 1 s ahead isn't spoken before then; a p2 due meanwhile goes
    # ahead of it.
    sink = Recorder()
    await start(sink)
    held = made("position", "template_lost", 12, slots={"position": 3}, hold=1.0)
    t0 = datetime.now(UTC)
    await publish(js, held)
    await asyncio.sleep(0.2)
    await publish(js, made("lap_time", "delta_first", 20, slots={"lap": 4, "lapTimeMs": 90000}))
    await until(lambda: len(sink.said) == 2)
    assert sink.said == ["Lap four. one thirty point zero.", "Down to third."]
    # Not before its hold, and not much after.
    held_at = datetime.now(UTC) - t0
    assert timedelta(seconds=1) <= held_at < timedelta(seconds=2)


async def test_dropped_at_each_stage_once_and_acked(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # AC3: expires while radio waits to connect, during synthesis and while another
    # plays: dropped at that stage, once each, and acked.
    sink = Recorder()
    sink.connected.clear()
    r = await start(sink)
    await publish(js, made("fuel", "template_pit", 5, expires_in=0.3))
    await until(lambda: r.callouts.dropped["connecting"] == 1)
    sink.connected.set()

    sink.synthesis = 0.5
    await publish(js, made("fuel", "template_pit_next", 5, expires_in=0.3))
    await until(lambda: r.callouts.dropped["synthesis"] == 1)
    sink.synthesis = 0

    sink.playing = 0.6
    await publish(js, made("final_lap", "template", 10, expires_in=5))
    await until(lambda: sink.started == ["Final lap."])
    await publish(js, made("fuel", "template_tight", 15, expires_in=0.2))
    await until(lambda: r.callouts.dropped["queued"] == 1)

    assert sink.said == ["Final lap."]
    assert drops(log) == [
        ("connecting", "Pit this lap."),
        ("synthesis", "Box next lap."),
        ("queued", "Fuel to the flag, just. Save a little."),
    ]
    # Each once, and the clip made for the one dropped after synthesis let go.
    await asyncio.sleep(callouts.ACK_WAIT + 0.5)
    assert sum(r.callouts.dropped.values()) == 3
    assert sink.released == 2
    assert await stream_empty(js)


async def test_a_sink_that_cannot_connect_drops_it(start: Any, js: JetStreamContext) -> None:
    sink = Recorder(can_connect=False)
    r = await start(sink)
    await publish(js, made())
    await until(lambda: r.callouts.dropped["not connected"] == 1)
    await wait_empty(js)


async def test_a_restart_says_nothing_twice_and_drops_what_expired(
    start: Any, js: JetStreamContext
) -> None:
    # AC4: radio dies with callouts taken and not said; the next one says each
    # once and drops the ones that expired meanwhile.
    first = Recorder(playing=3.0)
    a = await start(first)
    await publish(js, made("final_lap", "template", 10, expires_in=10))
    await until(lambda: first.started == ["Final lap."])
    # Taken while the final lap plays, never said.
    await publish(js, made("fuel", "template_pit", 15, expires_in=10))
    await publish(js, made("fuel", "template_tight", 16, expires_in=0.5))
    await until(lambda: len(a.callouts._held) == 2)
    await a.kill()
    assert first.said == []

    second = Recorder()
    b = await start(second)
    await until(lambda: second.said == ["Pit this lap."], timeout=10)
    await until(lambda: b.callouts.dropped["queued"] == 1)
    await wait_empty(js)
    # The final lap was acked when it began, so it isn't said again.
    assert second.said == ["Pit this lap."]


async def test_a_redelivery_is_never_said_twice(start: Any, js: JetStreamContext) -> None:
    # R6: held longer than the ack wait, it is delivered again, and said once.
    sink = Recorder()
    await start(sink)
    await publish(js, made("final_lap", "template", 10, hold=2.5, expires_in=5))
    await asyncio.sleep(3.2)
    assert sink.said == ["Final lap."]
    await asyncio.sleep(callouts.ACK_WAIT + 0.3)
    assert sink.said == ["Final lap."]
    assert await stream_empty(js)


async def test_one_that_fails_to_play_is_logged_and_the_next_is_said(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # R7.
    sink = Recorder(fail={"Final lap."})
    await start(sink)
    await publish(js, made("final_lap", "template", 10))
    await publish(js, made("fuel", "template_pit", 15))
    await until(lambda: sink.said == ["Pit this lap."])
    assert "callout failed to play" in [x["msg"] for x in lines(log)]
    await wait_empty(js)


async def test_phrased_in_the_order_raised_before_any_drop(
    start: Any, js: JetStreamContext
) -> None:
    # The maintainer's default (radio#16): each callout is phrased in the order it
    # was raised, run_id then at, before radio speaks or drops it, so the seeded
    # picks are the same whatever the levels, the holds and expiry do.
    raised = [
        made("lap_time", "delta_first", 20, at=10, slots={"lap": 2, "lapTimeMs": 64611}),
        made(
            "lap_time", "delta_equal", 5, at=11, slots={"lap": 3, "lapTimeMs": 64611, "deltaMs": 0}
        ),
        made(
            "lap_time",
            "delta_first",
            20,
            at=12,
            slots={"lap": 4, "lapTimeMs": 64000},
            expires_in=-1,
        ),
        made(
            "lap_time",
            "template_new_best",
            11,
            at=13,
            slots={"lap": 5, "lapTimeMs": 63000, "deltaMs": -1000},
        ),
        made(
            "lap_time",
            "delta_first",
            20,
            at=5,
            run_id="01RUM",
            slots={"lap": 1, "lapTimeMs": 70000},
        ),
    ]
    # Published out of order: the seq of a later frame first, the last run last.
    for m in reversed(raised):
        await publish(js, m)
    expected = Phrases(templates(), seed="replay")
    from radio.callout import from_message

    order = [raised[4], *raised[:4]]
    texts = {m.id: expected.say(from_message(m)) for m in order}
    sink = Recorder()
    r = await start(sink, Phrases(templates(), seed="replay"))
    await until(lambda: len(sink.said) == 4)
    assert r.callouts.dropped["queued"] == 1
    # Spoken by level and priority, worded as phrased in the order raised.
    assert sink.said == [texts[m.id] for m in (raised[1], raised[3], raised[0], raised[4])]


async def test_a_missing_slot_is_warned_and_still_said(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    sink = Recorder()
    await start(sink)
    await publish(js, made("fuel", "template", 15))
    await until(lambda: len(sink.said) == 1)
    warned = [x for x in lines(log) if x["msg"] == "callout slot missing"]
    assert [(w["rule"], w["kind"], w["missing"]) for w in warned] == [
        ("fuel", "template", ["laps"])
    ]


async def test_unreadable_and_unphrasable_callouts_are_acked_not_said(
    start: Any, js: JetStreamContext, nats_server: NATSServer, log: io.StringIO
) -> None:
    sink = Recorder()
    r = await start(sink)
    await until(lambda: r.callouts._subs)
    # Not a callout's ce-type.
    await js.publish(f"twr.v1.{SRC}.callout.p1", b"{}", headers={"ce-type": "nope"})
    # A rule radio doesn't know, with no text.
    await publish(js, made("pit_wall", "template", 15))
    # Nothing to say: a brief with no slots.
    await publish(js, made("start_brief", "template", 8))
    # Another publisher's text, said as it is.
    await publish(js, made("", "", 30, text="Radio check."))
    await until(lambda: sink.said == ["Radio check."])
    await wait_empty(js)
    msgs = [x["msg"] for x in lines(log)]
    assert "callout unreadable" in msgs
    assert "callout not phrased" in msgs
    assert "nothing to say" in msgs
    assert r.callouts.dropped["phrasing"] == 1


async def test_contracts_suzuka_callouts_are_said_as_v1_said_them(
    start: Any, js: JetStreamContext
) -> None:
    # contracts' five real v1 callouts, published one at a time as ecu sends them,
    # with v1's built-in phrases (`script: false`, as the recording was made).
    real = json.loads((CONTRACTS / "suzuka_no_stop_callouts.json").read_text())["callouts"]
    lap2 = made("lap_time", "delta_first", 20, slots={"lap": 2, "lapTimeMs": 64611})
    sent = [
        example("start_brief"),
        example("position"),
        lap2,
        example("new_best"),
        example("race_summary"),
    ]
    sink = Recorder()
    await start(sink)
    for i, m in enumerate(sent):
        await publish(js, m)
        await until(lambda i=i: len(sink.said) == i + 1)
    assert sink.said == [c["callout"]["text"] for c in real]


async def test_waits_for_the_stream(start: Any, js: JetStreamContext, log: io.StringIO) -> None:
    await js.delete_stream("CALLOUTS")
    sink = Recorder()
    r = await start(sink)
    await until(lambda: "callouts: consumers" in [x["msg"] for x in lines(log)])
    from helpers import CALLOUTS

    await js.add_stream(CALLOUTS)
    await until(lambda: r.callouts._subs)
    await publish(js, made())
    await until(lambda: sink.said == ["Pit this lap."])


async def test_another_source_is_not_taken(start: Any, js: JetStreamContext) -> None:
    sink = Recorder()
    await start(sink)
    await publish(js, made("final_lap", "template", 10), src="live")
    await publish(js, made())
    await until(lambda: sink.said == ["Pit this lap."])
    await asyncio.sleep(0.3)
    assert sink.said == ["Pit this lap."]
