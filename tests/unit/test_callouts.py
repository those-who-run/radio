"""radio.callouts against a real nats-server -js with the CALLOUTS stream as the
catalogue defines it (paddock#56): the callouts are published as ecu publishes
them, with contracts' ce headers on contracts' subjects."""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format
from helpers import lines, roster, until
from nats.js.client import JetStreamContext
from natsserver import NATSServer
from twr.events.v1 import callout_pb2
from twr_contracts import callout, ce

from radio import callouts
from radio.callout import from_message
from radio.engineer import Engineer, Roster
from radio.phrases import Phrases
from radio.phrasing import templates
from radio.queue import Pending
from radio.voice import VoiceSettings

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
    session: str = "",
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
    m.session_id = session
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
    # When each was said.
    at: list[datetime] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    released: int = 0
    can_connect: bool = True

    def __post_init__(self) -> None:
        self.connected.set()

    async def connect(self) -> bool:
        await self.connected.wait()
        return self.can_connect

    async def prepare(self, callout: Pending) -> str | None:
        await asyncio.sleep(self.synthesis)
        return callout.text or None

    def seconds(self, clip: str) -> float:
        return self.playing

    async def play(self, clip: str, callout: Pending) -> None:
        self.started.append(clip)
        if clip in self.fail:
            raise RuntimeError("the sink broke")
        await asyncio.sleep(self.playing)
        self.said.append(clip)
        self.at.append(datetime.now(UTC))

    def release(self, clip: str) -> None:
        self.released += 1

    async def close(self) -> None:
        return None


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
    # The hold is 2 s, a margin over how long radio takes to take a callout on a
    # loaded machine, so the p2 is taken while the position call is still held.
    sink = Recorder()
    r = await start(sink)
    await until(lambda: r.callouts._subs)
    held = made("position", "template_lost", 12, slots={"position": 3}, hold=2.0)
    await publish(js, held)
    await publish(js, made("lap_time", "delta_first", 20, slots={"lap": 4, "lapTimeMs": 90000}))
    await until(lambda: len(sink.said) == 2, timeout=10)
    assert sink.said == ["Lap four. one thirty point zero.", "Down to third."]
    # Not before its hold.
    assert sink.at[1] >= held.hold_until.ToDatetime(tzinfo=UTC)


async def test_dropped_at_each_stage_once_and_acked(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # AC3: expires while radio waits to connect, during synthesis and while another
    # plays: dropped at that stage, once each, and acked.
    # Each expiry is 1.5 s, a margin over how long radio takes to take a callout on
    # a loaded machine, and each stage outlasts it: the sink stays unconnected until
    # the drop, synthesis takes 3 s, and the other callout plays for 4 s.
    sink = Recorder()
    sink.connected.clear()
    r = await start(sink)
    await until(lambda: r.callouts._subs)
    await publish(js, made("fuel", "template_pit", 5, expires_in=1.5))
    await until(lambda: r.callouts.dropped["connecting"] == 1)
    sink.connected.set()

    sink.synthesis = 3.0
    await publish(js, made("fuel", "template_pit_next", 5, expires_in=1.5))
    await until(lambda: r.callouts.dropped["synthesis"] == 1)
    sink.synthesis = 0

    sink.playing = 4.0
    await publish(js, made("final_lap", "template", 10, expires_in=10))
    await until(lambda: sink.started == ["Final lap."])
    await publish(js, made("fuel", "template_tight", 15, expires_in=1.5))
    await until(lambda: r.callouts.dropped["queued"] == 1, timeout=10)

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
    await until(lambda: sink.said == ["Final lap."], timeout=10)
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


async def test_phrased_in_the_stream_order_before_any_drop(
    start: Any, js: JetStreamContext
) -> None:
    # Each callout is phrased in the order ecu published it, the stream's, across
    # the three levels, before radio speaks or drops it, so the seeded picks are
    # ecu's order whatever the levels, the holds and expiry do. The last one is a
    # callout ecu held in the pit lane and published at the exit: it keeps the
    # earlier `at` it was raised on, and is phrased where it was published. The
    # phrasebook is seeded by the session's id (radio.engineer).
    published = [
        made(
            "lap_time",
            "delta_first",
            20,
            at=10,
            session="replay",
            slots={"lap": 2, "lapTimeMs": 64611},
        ),
        made(
            "lap_time",
            "delta_equal",
            5,
            at=11,
            session="replay",
            slots={"lap": 3, "lapTimeMs": 64611, "deltaMs": 0},
        ),
        made(
            "lap_time",
            "delta_first",
            20,
            at=12,
            session="replay",
            slots={"lap": 4, "lapTimeMs": 64000},
            expires_in=-1,
        ),
        made(
            "lap_time",
            "template_new_best",
            11,
            at=13,
            session="replay",
            slots={"lap": 5, "lapTimeMs": 63000, "deltaMs": -1000},
        ),
        made(
            "lap_time",
            "template_in_lap",
            20,
            at=5,
            session="replay",
            slots={"lap": 1, "lapTimeMs": 70000},
        ),
    ]
    for m in published:
        await publish(js, m)
    expected = Phrases(templates(), seed="replay")
    texts = {m.id: expected.say(from_message(m)) for m in published}
    by_at = Phrases(templates(), seed="replay")
    assert {m.id: by_at.say(from_message(m)) for m in sorted(published, key=lambda m: m.at)} != (
        texts
    ), "the test tells the two orders apart"
    sink = Recorder()
    r = await start(sink, roster(script=True))
    await until(lambda: len(sink.said) == 4)
    assert r.callouts.dropped["queued"] == 1
    # Spoken by level and priority, worded as phrased in the stream's order.
    assert sink.said == [
        texts[m.id] for m in (published[1], published[3], published[0], published[4])
    ]


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


async def test_the_ack_before_speaking_is_taken_by_the_server(
    start: Any, js: JetStreamContext
) -> None:
    # Double ack: by the time the sink plays, the server has the ack, so the stream
    # no longer holds the callout even if the connection drops right then.
    sink = Recorder(playing=3.0)
    await start(sink)
    await publish(js, made("final_lap", "template", 10))
    await until(lambda: sink.started == ["Final lap."])
    assert await stream_empty(js)


async def test_each_session_has_its_engineer_and_seeded_words(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # The engineer is chosen at a session's first callout, once, and its phrasebook
    # is seeded by the session's id: two sessions' callouts, interleaved, are each
    # worded as that session's own seeded phrasebook words them.
    def lap(n: int, session: str) -> callout_pb2.Callout:
        return made(
            "lap_time",
            "delta_slower",
            20,
            session=session,
            slots={"lap": n, "lapTimeMs": 64611, "deltaMs": 700},
        )

    sent = [lap(n, s) for n in range(2, 6) for s in ("01A", "01B")]
    sink = Recorder()
    await start(sink, roster(script=True))
    for m in sent:
        await publish(js, m)
    await until(lambda: len(sink.said) == len(sent))
    for session in ("01A", "01B"):
        own = Phrases(templates(), seed=session)
        expected = [own.say(from_message(m)) for m in sent if m.session_id == session]
        assert [t for t, m in zip(sink.said, sent, strict=True) if m.session_id == session] == (
            expected
        )
    chosen = [
        (x["session_id"], x["engineer"]) for x in lines(log) if x["msg"] == "callouts: engineer"
    ]
    assert chosen == [("01A", "v1"), ("01B", "v1")]


async def test_another_engineer_words_the_same_callouts_differently(
    start: Any, js: JetStreamContext
) -> None:
    # ecu is the brain: an engineer changes how a callout is said, never which.
    other = Engineer("other", Path("other.onnx"), VoiceSettings(), templates(script=True))
    v1 = roster().default
    sent = [
        made("final_lap", "template", 11),
        made("fuel", "template_pit", 15),
        made("position", "template_lost", 12, slots={"position": 3}),
    ]
    said = {}
    for name in ("v1", "other"):
        sink = Recorder()
        r = await start(sink, Roster([v1, other], configured=name))
        for m in sent:
            m.id = f"{m.id}-{name}"
            await publish(js, m)
        await until(lambda sink=sink: len(sink.said) == len(sent))
        said[name] = sink.said
        await r.kill()
    assert said["v1"] == ["Final lap.", "Down to third.", "Pit this lap."]
    assert len(said["other"]) == 3 and said["other"] != said["v1"]


async def test_a_session_let_go_comes_back_with_its_engineer_and_seed(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # radio keeps the phrasebooks of the last SESSIONS sessions: a callout of one
    # let go starts its phrasebook again, from the same engineer and seed, so it
    # gets the seed's first pick again.
    def lap(session: str) -> callout_pb2.Callout:
        return made(
            "lap_time",
            "delta_slower",
            20,
            session=session,
            slots={"lap": 3, "lapTimeMs": 64611, "deltaMs": 700},
        )

    sessions = [f"01S{i:02d}" for i in range(callouts.SESSIONS + 1)]
    sink = Recorder()
    r = await start(sink, roster(script=True))
    for s in sessions:
        await publish(js, lap(s))
    await until(lambda: len(sink.said) == len(sessions))
    assert sessions[0] not in r.callouts._sessions
    assert len(r.callouts._sessions) == callouts.SESSIONS
    # The oldest again: chosen afresh, the same engineer, its seed's first pick.
    await publish(js, lap(sessions[0]))
    await until(lambda: len(sink.said) == len(sessions) + 1)
    first = Phrases(templates(), seed=sessions[0]).say(from_message(lap(sessions[0])))
    assert sink.said[0] == sink.said[-1] == first
    chosen = [x for x in lines(log) if x["msg"] == "callouts: engineer"]
    assert [(x["session_id"], x["engineer"]) for x in chosen if x["session_id"] == sessions[0]] == [
        (sessions[0], "v1"),
        (sessions[0], "v1"),
    ]


async def test_a_fetch_past_its_deadline_is_not_a_fetch_failure(
    start: Any, js: JetStreamContext, log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Found by the offline stage-1 run: during a burst, nats-py's fetch raises the
    # builtin TimeoutError, not its own, once its deadline passes. That is nothing
    # more on that level this round, not NATS down: no warning, no RETRY_WAIT.
    monkeypatch.setattr(callouts, "RETRY_WAIT", 30.0)
    sink = Recorder()
    r = await start(sink)
    await until(lambda: r.callouts._subs)
    level, sub = r.callouts._subs[0]
    real = sub.fetch
    raised: list[int] = []

    async def late(*args: Any, **kwargs: Any) -> Any:
        if not raised:
            raised.append(1)
            raise TimeoutError  # nats-py's plain `raise asyncio.TimeoutError`
        return await real(*args, **kwargs)

    monkeypatch.setattr(sub, "fetch", late)
    burst = [made("final_lap", "template", 10, at=i) for i in range(20)]
    for m in burst:
        await publish(js, m)
    # Well within RETRY_WAIT: the round carried on.
    await until(lambda: len(sink.said) == 20, timeout=10)
    assert raised
    assert "callouts: fetch" not in [x["msg"] for x in lines(log)]


async def test_cancelled_it_lets_go_of_its_subscriptions(start: Any, js: JetStreamContext) -> None:
    # Found by the offline stage-1 run: a pull subscription left behind made the
    # drain at shutdown wait out its timeout. Stopped, Callouts unsubscribes, so a
    # drain after it is prompt.
    r = await start(Recorder())
    await until(lambda: r.callouts._subs)
    r.task.cancel()
    await asyncio.gather(r.task, return_exceptions=True)
    async with asyncio.timeout(2):
        await r.nc.drain()
    assert r.callouts._subs == []
    # Only nats-py's own inbox for requests is left.
    assert all(s.subject.startswith("_INBOX.") for s in r.nc._subs.values())


async def test_played_says_when_the_audio_finished(
    start: Any, js: JetStreamContext, log: io.StringIO
) -> None:
    # paddock#62 R8: a "played (rule) in N.N s" line at playback's end, with the
    # callout's id; radio is idle again and knows when it last spoke.
    sink = Recorder(playing=0.3)
    r = await start(sink)
    await until(lambda: r.callouts._subs)
    before = r.callouts.last_spoken_at
    assert r.callouts.idle and before is None
    m = made("final_lap", "template", 10)
    await publish(js, m)
    # Not idle while it plays, though it was acked as it started (R9).
    await until(lambda: bool(sink.started))
    assert not sink.said and not r.callouts.idle
    await until(lambda: any(str(x["msg"]).startswith("played") for x in lines(log)))
    [played] = [x for x in lines(log) if str(x["msg"]).startswith("played")]
    prefix, secs = str(played["msg"]).removesuffix(" s").rsplit(" in ", 1)
    assert prefix == "played (final_lap)" and 0.3 <= float(secs) < 2.0
    assert (played["id"], played["rule"]) == (m.id, "final_lap")
    await until(lambda: r.callouts.idle)
    spoken = r.callouts.last_spoken_at
    assert spoken is not None and spoken >= sink.at[0] - timedelta(seconds=0.1)
