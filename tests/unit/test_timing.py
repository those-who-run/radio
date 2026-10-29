"""Instant and simulated timing on a replay's clock, and the timeline (paddock#60
R2 to R5), against a real nats-server -js.

The replay's clock creeps on with the wall clock once nothing comes, so these tests
stop it: `Wall` holds it still until a test has published everything and radio has
phrased it, then lets it run. Nothing here waits on the wall clock to decide what
radio says, so a loaded machine changes how long a test takes, never its result."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import until
from nats.js.client import JetStreamContext
from test_callouts import made, publish
from twr_contracts.callout import Level

from radio.queue import Pending
from radio.timing import INSTANT, SIMULATED, ReplayClock, Timeline, stamp

# Each replay's timeline file.
_runs = itertools.count()

# The recording's own time: long ago, so nothing can depend on the wall clock.
BASE = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


@dataclass
class Wall:
    """The replay clock's wall clock, which stands still until it is let go."""

    t: float = 0.0
    clock: ReplayClock = field(init=False)

    def __post_init__(self) -> None:
        self.clock = ReplayClock(lag=1.0, monotonic=lambda: self.t)

    def let_go(self) -> None:
        self.t += 1e6
        self.clock.poke()


@dataclass
class Speaker:
    """A sink whose callouts take `seconds` each, or what `length` says for the
    text; play returns at once, as the simulated speaker holds the channel itself."""

    seconds_each: float = 3.0
    length: dict[str, float] = field(default_factory=dict)
    said: list[str] = field(default_factory=list)

    async def connect(self) -> bool:
        return True

    async def prepare(self, callout: Pending) -> str | None:
        return callout.text or None

    def seconds(self, clip: str) -> float:
        return self.length.get(clip, self.seconds_each)

    async def play(self, clip: str, callout: Pending) -> None:
        self.said.append(clip)

    def release(self, clip: str) -> None:
        return None

    async def close(self) -> None:
        return None


class Delivery:
    async def ack(self) -> None:
        return None

    async def ack_sync(self, timeout: float = 1.0) -> object:
        return None


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


async def replay(
    start: Callable[..., Any],
    js: JetStreamContext,
    tmp_path: Path,
    timing: str,
    sent: list[Any],
    sink: Speaker | None = None,
    pace: Callable[[int], Any] | None = None,
) -> tuple[Speaker, list[dict[str, Any]]]:
    """Publish sent as a replay would, let the clock go once radio has phrased it
    all, and give the sink and the timeline once every callout is settled."""
    sink = sink or Speaker()
    wall = Wall()
    path = tmp_path / f"timeline-{next(_runs)}.jsonl"
    timeline = Timeline(path)
    r = await start(sink, timing=timing, timeline=timeline, replay=wall.clock)
    await until(lambda: r.callouts._subs)
    for i, m in enumerate(sent):
        await publish(js, m)
        if pace is not None:
            await pace(i)
    await until(lambda: r.callouts.phrased == len(sent), timeout=10)
    wall.let_go()
    await until(lambda: len(path.read_text().splitlines()) == len(sent), timeout=10)
    await r.kill()
    timeline.close()
    return sink, read(path)


def test_the_clock_is_the_latest_callout_then_creeps_lag_behind_the_wall() -> None:
    wall = Wall()
    c = wall.clock
    assert not c.past(at(0))  # Nothing queued yet.
    c.saw(at(10))
    assert c.past(at(9)) and not c.past(at(10))
    c.saw(at(5))  # An older one doesn't take it back.
    assert c.past(at(9))
    wall.t += 1.5  # Half a second past the lag.
    assert c.past(at(10.4)) and not c.past(at(10.5))


async def test_a_wait_wakes_when_a_callout_is_queued_or_the_clock_passes() -> None:
    wall = Wall()
    c = wall.clock
    waiting = asyncio.create_task(c.wait(at(0)))
    await asyncio.sleep(0)
    c.saw(at(1))
    await asyncio.wait_for(waiting, 5)
    # Past already: returns at once.
    await asyncio.wait_for(c.wait(at(0)), 5)
    # On the real wall clock, a short lag passes on its own.
    real = ReplayClock(lag=0.01)
    real.saw(at(0))
    await asyncio.wait_for(real.wait(at(0)), 5)
    assert real.past(at(0))


async def test_instant_says_everything_in_the_order_raised(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # R2, instant: every callout is said, expired or not, in the order of
    # their times, the queue's order within one time (v1's replay drains the queue
    # after each frame), and a hold is kept on the replay's clock. Each takes no
    # time.
    sent = [
        made("lap_time", "delta_first", 20, created=at(0), slots={"lap": 2, "lapTimeMs": 64611}),
        made("fuel", "template_critical", 5, created=at(0), expires_in=0.001),
        made("final_lap", "template", 11, created=at(1), hold=5, expires_in=2),
        made("position", "template_lost", 12, created=at(2), slots={"position": 3}),
    ]
    sink, timeline = await replay(start, js, tmp_path, INSTANT, sent)
    assert sink.said == [
        "Fuel critical. Less than one lap.",
        "Lap two. one oh four point six.",
        "Down to third.",
        "Final lap.",
    ]
    assert [(e["outcome"], e["started"], e["finished"]) for e in timeline] == [
        ("spoken", stamp(at(0)), stamp(at(0))),
        ("spoken", stamp(at(0)), stamp(at(0))),
        ("spoken", stamp(at(2)), stamp(at(2))),
        ("spoken", stamp(at(6)), stamp(at(6))),
    ]


def a_race() -> list[Any]:
    """A pit stop's worth of callouts, some close together, some held, at the
    replay's own times."""
    return [
        made("start_brief", "template", 8, created=at(0), slots={"track": "Suzuka", "laps": 5}),
        made("position", "template_gained", 12, created=at(1), slots={"position": 7}),
        made("lap_time", "delta_first", 20, created=at(60), slots={"lap": 2, "lapTimeMs": 64611}),
        made("fuel", "template_pit_next", 5, created=at(61)),
        made("position", "template_lost", 12, created=at(62), slots={"position": 9}),
        made(
            "lap_time", "template_in_lap", 20, created=at(120), slots={"lap": 3, "lapTimeMs": 70000}
        ),
        made("position", "template_lost", 12, created=at(121), slots={"position": 12}),
        made(
            "lap_time",
            "template_out_lap",
            20,
            created=at(150),
            hold=2,
            slots={"lap": 4, "lapTimeMs": 90000},
        ),
        made("position", "template_gained", 12, created=at(150.5), slots={"position": 10}),
        made("final_lap", "template", 11, created=at(151)),
    ]


def spans(timeline: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["started"], e["finished"]) for e in timeline if e["outcome"] == "spoken"]


async def test_the_simulated_speaker_never_overlaps(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # AC3 (synthetic; the Suzuka pit-stop replay needs ecu's callouts, #89): one
    # callout at a time, each starting at or after the one before finishes.
    sink, timeline = await replay(start, js, tmp_path, SIMULATED, a_race())
    said = sorted(spans(timeline))
    assert len(said) == len(a_race())
    for (_, finished), (started, _) in zip(said, said[1:], strict=False):
        assert started >= finished
    # Each holds the channel for its 3 s.
    for started, finished in said:
        assert datetime.fromisoformat(finished) - datetime.fromisoformat(started) == timedelta(
            seconds=3
        )
    # The held out-lap call waits out its hold, and the fuel call, p0, went first.
    first_at_60 = next(e for e in timeline if e["started"] == stamp(at(60)))
    assert first_at_60["rule"] == "lap_time"
    assert [e["rule"] for e in timeline[2:5]] == ["lap_time", "fuel", "position"]


async def test_a_replay_says_the_same_whatever_its_speed(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # R4: published in a burst, or one at a time with radio catching up between
    # them, the timeline is the same: radio goes by the callouts' times.
    _, burst = await replay(start, js, tmp_path, SIMULATED, a_race())

    async def slowly(i: int) -> None:
        await asyncio.sleep(0.05)

    _, paced = await replay(start, js, tmp_path, SIMULATED, a_race(), pace=slowly)

    def strip(t: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        keys = ("rule", "text", "queued", "started", "finished", "outcome", "stage")
        return [tuple(e[k] for k in keys) for e in t]

    assert strip(paced) == strip(burst)


async def test_a_burst_at_the_line_expires_as_it_would_live(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # AC4: an 8 s staleness and a burst of callouts at the line, each 3 s to say:
    # the first three are said (0, 3 and 6 s), and the rest are stale by the time
    # the speaker is free (9 s), so they expire in the queue.
    burst = [
        made("race_summary", "template_position", 1, created=at(0), slots={"position": 3}),
        made("position", "template_gained", 12, created=at(0), slots={"position": 3}),
        made("lap_time", "delta_first", 20, created=at(0), slots={"lap": 5, "lapTimeMs": 64611}),
        made("fuel", "template_last_lap_enough", 21, created=at(0)),
        made("temps", "template_water", 22, created=at(0)),
    ]
    _, timeline = await replay(start, js, tmp_path, SIMULATED, burst)
    outcome = {e["rule"]: (e["outcome"], e["stage"], e["started"]) for e in timeline}
    assert outcome == {
        "race_summary": ("spoken", None, stamp(at(0))),
        "position": ("spoken", None, stamp(at(3))),
        "lap_time": ("spoken", None, stamp(at(6))),
        "fuel": ("expired", "queued", None),
        "temps": ("expired", "queued", None),
    }
    assert {e["dropped"] for e in timeline if e["outcome"] == "expired"} == {stamp(at(9))}


@pytest.mark.parametrize("timing", [INSTANT, SIMULATED])
async def test_the_timeline_has_every_field(
    start: Any, js: JetStreamContext, tmp_path: Path, timing: str
) -> None:
    # R5's fields, with the kind, the engineer and the callout's created_at.
    m = made("final_lap", "template", 11, at=42, created=at(0))
    _, [entry] = await replay(start, js, tmp_path, timing, [m], Speaker(seconds_each=1.25))
    assert entry == {
        "callout_id": m.id,
        "rule": "final_lap",
        "kind": "template",
        "text": "Final lap.",
        "engineer": "v1",
        "at": 42,
        "created_at": stamp(at(0)),
        "queued": stamp(at(0)),
        "started": stamp(at(0)),
        "finished": stamp(at(1.25 if timing == SIMULATED else 0)),
        "dropped": None,
        "outcome": "spoken",
        "stage": None,
    }


def test_a_restart_appends_to_the_timeline(tmp_path: Path) -> None:
    path = tmp_path / "timeline.jsonl"
    path.write_text('{"callout_id":"before"}\n')
    m = made("final_lap", "template", 11, created=at(0))
    p = Pending(m, Level.P1, 1, Delivery())
    t = Timeline(path)
    t.write(p, "spoken")
    t.close()
    assert [e["callout_id"] for e in read(path)] == ["before", m.id]


async def test_a_long_burst_is_not_overtaken_by_the_creep(
    start: Any, js: JetStreamContext, tmp_path: Path
) -> None:
    # On the real wall clock: an out-lap call at 0 s held 2 s, then 199 callouts
    # created at 0.01 s to 1.99 s, published one at a time for about 4 s, longer
    # than the lag plus the hold. The clock stands still while radio is taking
    # them, so the out-lap call is said last, at 2 s, after all 199.
    sink = Speaker()
    path = tmp_path / "timeline.jsonl"
    timeline = Timeline(path)
    r = await start(sink, timing=INSTANT, timeline=timeline, replay=ReplayClock())
    await until(lambda: r.callouts._subs)
    out_lap = made(
        "lap_time",
        "template_out_lap",
        20,
        created=at(0),
        hold=2,
        slots={"lap": 4, "lapTimeMs": 90000},
    )
    await publish(js, out_lap)
    await until(lambda: r.callouts.phrased == 1)
    for i in range(1, 200):
        await publish(js, made("final_lap", "template", 11, created=at(i / 100)))
        await asyncio.sleep(0.02)
    await until(lambda: len(path.read_text().splitlines()) == 200, timeout=20)
    timeline.close()
    entries = read(path)
    assert entries[-1]["callout_id"] == out_lap.id
    assert entries[-1]["started"] == stamp(at(2))
