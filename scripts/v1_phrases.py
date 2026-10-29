"""Write tests/unit/testdata/v1_phrases.json: what v1.0.0's own rules said for each
kind ecu sends, on representative values, with the kind and slots ecu would send
for the same moment (paddock#90). tests/unit/test_phrases.py checks radio against it.

Run by hand against a v1.0.0 tree, never in CI (radio does not depend on v1):

    git -C <v1 clone> archive v1.0.0 | tar -x -C /tmp/v1
    PYTHONPATH=/tmp/v1/src uv run --no-sync python scripts/v1_phrases.py \
        > tests/unit/testdata/v1_phrases.json

Each rule is driven through its own code (evaluate, on_finish, on_end, observe) on
made-up frames, with its phrasebook seeded as radio seeds it,
random.Random(f"{seed}:{rule}"), so the variants match pick for pick. Each case
runs once with the script and once with `script: false`, v1's built-in phrases.
"""

from __future__ import annotations

import json
import random
import sys
from collections import deque
from types import SimpleNamespace
from typing import Any

from those_who_run.config import DEFAULT_RULES, TEMPLATE_PREFIXES, load_rules_config
from those_who_run.phrasing import Phrasebook
from those_who_run.pitstop import NO_STOPS
from those_who_run.rules.final_lap import FinalLapRule
from those_who_run.rules.fuel import FuelRule
from those_who_run.rules.lap_time import LapTimeRule
from those_who_run.rules.position import PositionRule
from those_who_run.rules.race_summary import RaceSummaryRule
from those_who_run.rules.start_brief import StartBriefRule
from those_who_run.rules.telemetry import TelemetryVoice
from those_who_run.rules.temps import TempsRule
from those_who_run.state import Phase, RaceState, derive_phase
from those_who_run.verbalise import spoken_delta_ms

V1 = "v1.0.0 (d2cd3739b0237283ed74ab908c71a1d4a2c12455)"
SEED = 90
CORNERS = ("CORNER_FRONT_LEFT", "CORNER_FRONT_RIGHT", "CORNER_REAR_LEFT", "CORNER_REAR_RIGHT")


def state(**over: Any) -> RaceState:
    """A frame, as v1's tests/conftest.py make_state builds one."""
    base: dict[str, Any] = dict(
        t=0.0,
        packet_id=0,
        current_lap=1,
        total_laps=5,
        best_lap_ms=0,
        last_lap_ms=0,
        fuel_level=100.0,
        fuel_capacity=100.0,
        speed_kph=150.0,
        engine_rpm=6000.0,
        tire_temp=(60.0, 60.0, 60.0, 60.0),
        water_temp=90.0,
        oil_temp=95.0,
        track_id=7,
        race_start_pos=5,
        total_cars=16,
        cars_on_track=True,
    )
    base.update(over)
    base.setdefault("phase", derive_phase(base["current_lap"], base["total_laps"], False, False))
    return RaceState(**base)


class Pits:
    """The pit detector's verdict on one lap."""

    in_pit_lane = False

    def __init__(self, role: str | None) -> None:
        self.role = role

    def lap_role(self, lap: int) -> str | None:
        return self.role

    def is_pit_lap(self, lap: int) -> bool:
        return self.role is not None


def slots(**values: Any) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None and v != "" and v is not False}


def lap_time(rule: LapTimeRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    situations: list[tuple[int, int, int, str | None]] = []
    for _ in range(4):
        lap = rng.randint(3, 60)
        lap_ms = rng.randint(30_000, 599_999)
        situations += [
            (lap, lap_ms, 0, None),  # no best yet: delta_first
            (lap, lap_ms, lap_ms + rng.randint(-49, 49), None),  # equal, spoken
            (lap, lap_ms, lap_ms - rng.randint(350, 1900), None),  # slower
            (lap, lap_ms, lap_ms - rng.randint(60, 249), None),  # slower, small
            (lap, lap_ms, lap_ms - rng.randint(2100, 70_000), None),  # slower, large
            (lap, lap_ms, lap_ms + rng.randint(260, 20_000), None),  # new best
            (lap, lap_ms, lap_ms + rng.randint(60, 149), None),  # new best, small
            (lap, lap_ms, lap_ms - 1000, "in"),
            (lap, lap_ms, lap_ms - 1000, "out"),
            (lap, lap_ms, lap_ms - 1000, "in_out"),
        ]
    roles = {"in": "template_in_lap", "out": "template_out_lap", "in_out": "template_pit_lap"}
    for lap, lap_ms, best, role in situations:
        rule.reset()
        rule.pits = Pits(role)  # type: ignore[assignment]
        prev = state(t=99.0, current_lap=lap, total_laps=99, last_lap_ms=1, best_lap_ms=best)
        now = state(t=100.0, current_lap=lap + 1, total_laps=99, last_lap_ms=lap_ms)
        [callout] = rule.evaluate(prev, now)
        diff = spoken_delta_ms(lap_ms, best)
        delta: int | None = lap_ms - best
        if role is not None:
            kind, delta = roles[role], None
        elif best <= 0:
            kind, delta = "delta_first", None
        elif diff < 0:
            kind = "template_new_best_small" if -diff < 200 else "template_new_best"
        elif diff == 0:
            kind = "delta_equal"
        elif diff < 300:
            kind = "delta_slower_small"
        elif diff > 2000:
            kind = "delta_slower_large"
        else:
            kind = "delta_slower"
        out.append(
            {
                "kind": kind,
                "slots": slots(lap=lap, lap_time_ms=lap_ms, delta_ms=delta),
                "text": callout.text,
            }
        )
    return out


def fuel(rule: FuelRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    # 5 litres a lap. (fuel left, laps in the race or 0 for timed, lap crossed into,
    # what was said before, and the kinds v1 says, with their laps.)
    situations: list[tuple[float, int, int, set[str], list[tuple[str, int | None]]]] = [
        (35.0, 0, 6, set(), [("template", 7)]),
        (34.0, 0, 6, set(), [("template_short", 7)]),  # 6.8 laps, said as seven
        (10.0, 0, 6, set(), [("template_pit_next", None), ("template", 2)]),
        (7.5, 0, 6, set(), [("template_pit", None), ("template", 1)]),
        (4.0, 0, 6, {"pit"}, [("template_critical", None)]),
        (26.0, 10, 6, set(), [("template_tight", None)]),  # 5.2 laps for five
        (6.0, 10, 10, {"tight", "warned"}, [("template_last_lap_enough", None)]),
    ]
    for _ in range(3):
        for level, total, lap, said, kinds in situations:
            rule.reset()
            rule.pits = NO_STOPS
            rule.consumption = deque([5.0], maxlen=rule.rolling)
            rule.lap_start_fuel = level + 5.0
            rule.bridged = True
            rule.pit_announced = rule.pit_next_announced = "pit" in said
            rule.tight_announced = "tight" in said
            if "warned" in said:
                rule.announced = {5}
            prev = state(t=99.0, current_lap=lap - 1, total_laps=total, fuel_level=level + 0.1)
            now = state(t=100.0, current_lap=lap, total_laps=total, fuel_level=level)
            callouts = rule.evaluate(prev, now)
            assert len(callouts) == len(kinds), (level, [c.text for c in callouts])
            for callout, (kind, laps) in zip(callouts, kinds, strict=True):
                out.append({"kind": kind, "slots": slots(laps=laps), "text": callout.text})
    return out


def final_lap(rule: FinalLapRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    for _ in range(8):
        rule.reset()
        prev = state(t=99.0, current_lap=4, total_laps=5)
        now = state(t=100.0, current_lap=5, total_laps=5)
        [callout] = rule.evaluate(prev, now)
        out.append({"kind": "template", "slots": {}, "text": callout.text})
    return out


def position(rule: PositionRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    pairs = [(5, 1), (2, 1), (6, 3), (4, 2), (9, 5), (12, 7), (1, 4), (1, 2), (3, 5), (10, 12)]
    pairs += [(rng.randint(1, 40), rng.randint(1, 40)) for _ in range(30)]
    for was, pos in pairs:
        if was == pos:
            continue
        rule.reset()
        rule.started_at = 0.0
        rule.announced = was
        rule.pending_since = 50.0
        now = state(t=100.0, race_start_pos=pos, total_cars=40)
        [callout] = rule.evaluate(None, now)
        if pos < was:
            kind = "template_gained"
            if pos == 1:
                kind = "template_gained_lead"
            elif pos <= 3:
                kind = "template_gained_podium"
        else:
            kind = "template_lost_lead" if was == 1 else "template_lost"
        out.append(
            {
                "kind": kind,
                "slots": slots(position=pos, previous_position=was),
                "text": callout.text,
            }
        )
    return out


TRACKS = [
    "Suzuka Circuit East Course",
    "Nurburgring 24h",
    "Alsace - Village Reverse",
    "Willow Springs International Raceway: Big Willow",
    "Kyoto Driving Park - Yamagiwa+Miyabi",
    "24 Heures du Mans Racing Circuit No Chicane",
    "Fuji International Speedway (Short)",
    "Circuit de Spa-Francorchamps",
    "Sportsland SUGO",
]


def start_brief(rule: StartBriefRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    for i in range(24):
        track = TRACKS[i % len(TRACKS)] if i % 4 else None
        shape = i % 3  # 0: a lap race from the start, 1: joined after lap 1, 2: timed
        total = 0 if shape == 2 else rng.randint(2, 50)
        lap = rng.randint(2, total) if shape == 1 else 1
        field = shape == 2 or i % 5 != 0
        cars = rng.randint(2, 40) if field else 1
        pos = rng.randint(1, cars) if field else -1
        rule.reset()
        rule.track = track
        rule.started_at = 0.0
        rule.start_lap = lap
        now = state(
            t=10.0,
            current_lap=lap,
            total_laps=total,
            race_start_pos=pos,
            total_cars=cars,
            cars_on_track=field,
        )
        [callout] = rule.evaluate(None, now)
        out.append(
            {
                "kind": "template",
                "slots": slots(
                    track=track,
                    laps=total if total and lap <= 1 else None,
                    laps_left=(now.laps_left or None) if total and lap > 1 else None,
                    timed=not total and field,
                    position=pos if field else None,
                    total_cars=cars if field else None,
                ),
                "text": callout.text,
            }
        )
    for name in TRACKS:  # the track named after the brief
        rule.reset()
        rule.started_at = 0.0
        rule.start_lap = 1
        rule.briefed = True
        rule.pending_track = name
        [callout] = rule.evaluate(None, state(t=10.0, current_lap=1))
        out.append({"kind": "template_track", "slots": {"track": name}, "text": callout.text})
    for i in range(12):  # joined part way
        total = rng.randint(2, 50) if i % 3 else 0
        field = i % 4 != 0
        cars = rng.randint(2, 40) if field else 1
        pos = rng.randint(1, cars) if field else -1
        lap = rng.randint(1, total) if total else rng.randint(1, 30)
        rule.reset()
        rule.joined = True
        rule.started_at = 0.0
        rule.start_lap = lap
        now = state(
            t=10.0,
            current_lap=lap,
            total_laps=total,
            race_start_pos=pos,
            total_cars=cars,
            cars_on_track=field,
        )
        [callout] = rule.evaluate(None, now)
        out.append(
            {
                "kind": "template_joined",
                "slots": slots(
                    lap=lap,
                    laps=total or None,
                    position=pos if field else None,
                    total_cars=cars if field else None,
                ),
                "text": callout.text,
            }
        )
    rule.joined = False
    return out


def race_summary(rule: RaceSummaryRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    finishes = [(1, 61186), (2, 61186), (3, 0), (7, 99_950), (None, 72_300), (None, 0)]
    finishes += [(rng.randint(1, 20), rng.randint(30_000, 599_999)) for _ in range(14)]
    for pos, best in finishes:
        rule.reset()
        field = pos is not None
        now = state(
            t=100.0,
            current_lap=7,
            total_laps=6,
            best_lap_ms=best,
            race_start_pos=pos or -1,
            total_cars=20 if field else 1,
            cars_on_track=field,
        )
        [callout] = rule.on_finish(now)
        kind = "template"
        if pos == 1:
            kind = "template_position_win"
        elif pos is not None and pos <= 3:
            kind = "template_position_podium"
        elif pos is not None:
            kind = "template_position"
        out.append(
            {
                "kind": kind,
                "slots": slots(position=pos, best_lap_ms=best or None),
                "text": callout.text,
            }
        )
    for laps, best in [(1, 0), (6, 61186), (12, 101_000)] + [
        (rng.randint(1, 80), rng.randint(30_000, 599_999)) for _ in range(5)
    ]:
        rule.reset()
        rule.session = SimpleNamespace(kind="solo_timed")  # type: ignore[assignment]
        rule.laps_done = laps
        last = state(t=99.0, current_lap=1, total_laps=0, best_lap_ms=best, cars_on_track=False)
        [callout] = rule.on_end(last, state(t=100.0, current_lap=-1, phase=Phase.MENU))
        out.append(
            {
                "kind": "template_quit",
                "slots": slots(laps=laps, best_lap_ms=best or None),
                "text": callout.text,
            }
        )
    return out


def temps(rule: TempsRule, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    hot_sets = [(0,), (1,), (2,), (3,), (2, 3), (0, 1), (0, 1, 2, 3), (0, 2), (1, 3), (0, 1, 3)]
    for i in range(2):
        for hot in hot_sets:
            rule.reset()
            rule.cold_checked = True
            temps_c = tuple(130.0 if c in hot else 80.0 for c in range(4))
            said = []
            for t in range(7):
                said += rule.evaluate(None, state(t=float(t), tire_temp=temps_c))
            [callout] = said
            out.append(
                {
                    "kind": "template_tyre_hot",
                    "slots": {"corners": [CORNERS[c] for c in hot]},
                    "text": callout.text,
                }
            )
        for kind, over in (
            ("template_tyres_cold", {"tire_temp": (30.0, 60.0, 60.0, 60.0)}),
            ("template_water", {"water_temp": 120.0}),
            ("template_oil", {"oil_temp": 140.0}),
        ):
            for _ in range(3):
                rule.reset()
                rule.cold_checked = kind != "template_tyres_cold"
                said = []
                for t in range(7):
                    said += rule.evaluate(None, state(t=float(t), **over))
                [callout] = said
                out.append({"kind": kind, "slots": {}, "text": callout.text})
    return out


def telemetry(voice: TelemetryVoice, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    last = state(t=1.0)
    voice.observe(True, last, 1.0)
    for i in range(8):
        [lost] = voice.observe(False, last, 2.0 + i)
        [back] = voice.observe(True, last, 2.5 + i)
        out.append({"kind": "template_lost", "slots": {}, "text": lost.text})
        out.append({"kind": "template_back", "slots": {}, "text": back.text})
    return out


DRIVERS = {
    "lap_time": (LapTimeRule, lap_time),
    "fuel": (FuelRule, fuel),
    "final_lap": (FinalLapRule, final_lap),
    "position": (PositionRule, position),
    "start_brief": (StartBriefRule, start_brief),
    "race_summary": (RaceSummaryRule, race_summary),
    "temps": (TempsRule, temps),
    "telemetry": (TelemetryVoice, telemetry),
}


def main() -> None:
    cases = []
    for script in (True, False):
        cfg = load_rules_config(None) if script else _no_script()
        for name, (cls, drive) in DRIVERS.items():
            rule = cls(cfg["rules"][name])
            rule.phrases = Phrasebook(rule.cfg, random.Random(f"{SEED}:{name}"))
            callouts = drive(rule, random.Random(f"slots:{name}"))  # type: ignore[arg-type]
            cases.append({"rule": name, "script": script, "seed": SEED, "callouts": callouts})
    defaults = {
        rule: {k: v for k, v in keys.items() if str(k).startswith(TEMPLATE_PREFIXES)}
        for rule, keys in DEFAULT_RULES["rules"].items()
    }
    json.dump({"v1": V1, "defaults": defaults, "cases": cases}, sys.stdout, indent=1)
    sys.stdout.write("\n")


def _no_script() -> dict[str, Any]:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.yaml"
        path.write_text("script: false\n")
        return load_rules_config(path)


if __name__ == "__main__":
    main()
