"""A callout's sentence equals v1.0.0's for the same values (paddock#90).

testdata/v1_phrases.json is v1's own rules run on representative values for every
kind ecu sends, with and without the script, written by scripts/v1_phrases.py.
testdata/contracts/ holds, unchanged, contracts v0.2.2's callout examples
(examples/events/v1/callout_*.json, protojson) and its
testdata/v1/suzuka_no_stop_callouts.json, five real v1 callouts with their text.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format
from twr.events.v1 import callout_pb2, car_pb2

from radio.callout import Callout, CalloutSlots, Corner, from_message
from radio.phrases import FALLBACK, NEEDS, PhraseError, Phrases, corners_phrase, missing
from radio.phrasing import DEFAULTS, load_rules_file, load_script, templates
from radio.speech import split_sentences

TESTDATA = Path(__file__).parent / "testdata"
GOLDEN = json.loads((TESTDATA / "v1_phrases.json").read_text(encoding="utf-8"))
CONTRACTS = TESTDATA / "contracts"

# The kinds ecu sends, by rule (callout.proto's list).
KINDS = {
    "lap_time": {
        "delta_first",
        "delta_equal",
        "delta_slower",
        "delta_slower_small",
        "delta_slower_large",
        "template_new_best",
        "template_new_best_small",
        "template_in_lap",
        "template_out_lap",
        "template_pit_lap",
    },
    "fuel": {
        "template",
        "template_short",
        "template_critical",
        "template_pit_next",
        "template_pit",
        "template_last_lap_enough",
        "template_tight",
    },
    "final_lap": {"template"},
    "position": {
        "template_gained",
        "template_gained_lead",
        "template_gained_podium",
        "template_lost",
        "template_lost_lead",
    },
    "start_brief": {"template", "template_track", "template_joined"},
    "race_summary": {
        "template",
        "template_position",
        "template_position_win",
        "template_position_podium",
        "template_quit",
    },
    "temps": {"template_tyre_hot", "template_tyres_cold", "template_water", "template_oil"},
    "telemetry": {"template_lost", "template_back"},
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def slots_of(values: dict[str, Any]) -> CalloutSlots:
    """CalloutSlots from JSON, the golden's names or protojson's camelCase."""
    out = {_snake(k): v for k, v in values.items()}
    if "corners" in out:
        out["corners"] = tuple(Corner[c.removeprefix("CORNER_")] for c in out["corners"])
    return CalloutSlots(**out)


def callout_of(doc: dict[str, Any]) -> Callout:
    return Callout(
        rule=doc.get("rule", ""),
        kind=doc.get("kind", ""),
        slots=slots_of(doc.get("slots", {})),
        text=doc.get("text", ""),
    )


def _case_id(case: dict[str, Any]) -> str:
    return f"{case['rule']}-{'script' if case['script'] else 'built-in'}"


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_every_kind_says_what_v1_said(case: dict[str, Any]) -> None:
    """Pick for pick with the same seed: the same variants, in the same order, so the
    never-twice memory and the fallbacks are v1's too."""
    phrases = Phrases(templates(script=case["script"]), seed=case["seed"])
    for i, c in enumerate(case["callouts"]):
        callout = Callout(rule=case["rule"], kind=c["kind"], slots=slots_of(c["slots"]))
        assert phrases.say(callout) == c["text"], (i, c)


def test_the_goldens_cover_every_kind_ecu_sends() -> None:
    for script in (True, False):
        seen: dict[str, set[str]] = {}
        for case in GOLDEN["cases"]:
            if case["script"] is script:
                seen.setdefault(case["rule"], set()).update(c["kind"] for c in case["callouts"])
        assert seen == KINDS


def test_the_built_in_phrases_are_v1s() -> None:
    assert GOLDEN["defaults"] == DEFAULTS


def test_the_v1_callouts_from_suzuka() -> None:
    """contracts' five real v1 callouts (replayed with v1's default rules, `script:
    false`), with the kind and slots ecu sends for each (contracts' goldens)."""
    real = json.loads((CONTRACTS / "suzuka_no_stop_callouts.json").read_text())["callouts"]
    sent = [
        callout_of(json.loads((CONTRACTS / "callout_start_brief.json").read_text())),
        callout_of(json.loads((CONTRACTS / "callout_position.json").read_text())),
        # Lap 2 of a lap race, measured against lap 1 only: no delta.
        Callout("lap_time", "delta_first", CalloutSlots(lap=2, lap_time_ms=64611)),
        callout_of(json.loads((CONTRACTS / "callout_new_best.json").read_text())),
        callout_of(json.loads((CONTRACTS / "callout_race_summary.json").read_text())),
    ]
    phrases = Phrases(templates(script=False))
    assert [phrases.say(c) for c in sent] == [r["callout"]["text"] for r in real]


def test_the_other_contract_examples() -> None:
    phrases = Phrases(templates(script=False))
    lap = callout_of(json.loads((CONTRACTS / "callout_lap_time.json").read_text()))
    assert phrases.say(lap) == "Lap three. one thirty-nine point zero. one point zero over best."
    lost = callout_of(json.loads((CONTRACTS / "callout_telemetry.json").read_text()))
    assert phrases.say(lost) == "Lost telemetry."


def test_delta_equal_says_the_spoken_difference_not_the_exact_one() -> None:
    """63120 against a best of 63140 is -20 exact but nothing spoken; and the gap is
    the difference of the spoken times (callout.proto's delta_ms)."""
    phrases = Phrases(templates(script=False))
    equal = CalloutSlots(lap=4, lap_time_ms=63120, delta_ms=-20)
    assert phrases.say(Callout("lap_time", "delta_equal", equal)) == (
        "Lap four. one oh three point one. equal to best."
    )
    best = CalloutSlots(lap=3, lap_time_ms=102_250, delta_ms=-100)  # 102.3 against 102.4
    assert phrases.say(Callout("lap_time", "template_new_best_small", best)) == (
        "Lap three. one forty-two point three. New best by zero point one."
    )


@pytest.mark.parametrize(("rule", "kind"), sorted(FALLBACK))
def test_a_situational_key_falls_back_to_its_general_one(rule: str, kind: str) -> None:
    general = FALLBACK[(rule, kind)]
    table = templates()
    table[rule].pop(kind)
    table[rule][general] = "GENERAL"
    slots = CalloutSlots(lap=3, lap_time_ms=60_000, delta_ms=500, position=2, best_lap_ms=60_000)
    said = Phrases(table).say(Callout(rule, kind, slots))
    assert "GENERAL" in said, said
    # Configured, the situational pool wins.
    table[rule][kind] = "SITUATIONAL"
    said = Phrases(table).say(Callout(rule, kind, slots))
    assert "SITUATIONAL" in said, said


def test_a_situational_key_with_no_template_at_all_takes_the_built_in() -> None:
    table = templates(script=False)
    lead = Callout("position", "template_gained_lead", CalloutSlots(position=1))
    assert Phrases(table).say(lead) == "Up to first."
    small = CalloutSlots(lap=5, lap_time_ms=60_100, delta_ms=100)
    assert Phrases(table).say(Callout("lap_time", "delta_slower_small", small)) == (
        "Lap five. one oh zero point one. zero point one over best."
    )


def test_a_key_never_gives_the_same_variant_twice_running() -> None:
    phrases = Phrases()
    for rule, kinds in KINDS.items():
        for kind in kinds:
            said = [phrases.say(Callout(rule, kind, _worst_slots())) for _ in range(30)]
            if len(set(said)) > 1:
                assert all(a != b for a, b in zip(said, said[1:], strict=False)), (rule, kind)


def test_the_memory_is_per_rule_and_key() -> None:
    """Two rules with the same key each remember their own last variant."""
    table = {"fuel": {"template_pit": ["a", "b"]}, "final_lap": {"template": ["a", "b"]}}
    phrases = Phrases(table, seed=1)
    fuel = [phrases.say(Callout("fuel", "template_pit")) for _ in range(6)]
    final = [phrases.say(Callout("final_lap", "template")) for _ in range(6)]
    assert fuel in (["a.", "b."] * 3, ["b.", "a."] * 3)
    assert final in (["a.", "b."] * 3, ["b.", "a."] * 3)


def test_the_same_seed_says_the_same_words() -> None:
    def run(seed: int | str | None) -> list[str]:
        phrases = Phrases(seed=seed)
        return [
            phrases.say(Callout(case["rule"], c["kind"], slots_of(c["slots"])))
            for case in GOLDEN["cases"]
            if case["script"]
            for c in case["callouts"]
        ]

    assert run("session-1") == run("session-1")
    assert run("session-1") != run("session-2")
    unseeded = run(None)
    assert len(unseeded) == len(run(7))


def test_text_is_spoken_as_it_is() -> None:
    phrases = Phrases()
    assert phrases.say(Callout(text="Box, box.  Now")) == "Box, box.  Now"
    assert phrases.say(Callout(rule="pit_wall", text="Rain in five minutes.")) == (
        "Rain in five minutes."
    )
    # A kind radio doesn't know, with text beside it: the text.
    assert phrases.say(Callout("fuel", "template_new", text="Fuel.")) == "Fuel."


@pytest.mark.parametrize(
    "callout",
    [
        Callout(),
        Callout("pit_wall", "template"),
        Callout("fuel", "template_new"),
        Callout("lap_time", "template"),
        Callout("final_lap", "template_x"),
        Callout("position", "template_x"),
        Callout("start_brief", "template_laps"),
        Callout("race_summary", "template_best"),
        Callout("temps", "template_x"),
        Callout("telemetry", "template_x"),
    ],
)
def test_a_kind_radio_does_not_know_with_no_text_is_an_error(callout: Callout) -> None:
    with pytest.raises(PhraseError, match="no phrase"):
        Phrases().say(callout)


@pytest.mark.parametrize(
    "template", ["Down to {position.x}.", "Down to {position[x]}.", "Down to {p:d}."]
)
def test_a_template_with_a_bad_field_is_an_error(template: str) -> None:
    """A table built by hand skips the load-time check: say still raises PhraseError."""
    table = templates()
    table["position"]["template_lost"] = template
    with pytest.raises(PhraseError, match=r"position\.template_lost"):
        Phrases(table).say(Callout("position", "template_lost", CalloutSlots(position=4)))


def test_a_template_with_a_placeholder_its_kind_does_not_fill_is_an_error() -> None:
    table = templates()
    table["position"]["template_lost"] = "Down to {position} on lap {lap}."
    with pytest.raises(PhraseError, match=r"position\.template_lost"):
        Phrases(table).say(Callout("position", "template_lost", CalloutSlots(position=4)))


def test_slots_not_set_are_not_said() -> None:
    phrases = Phrases(templates(script=False))
    assert phrases.say(Callout("start_brief", "template")) == ""
    assert phrases.say(Callout("start_brief", "template_track")) == ""
    assert phrases.say(Callout("race_summary", "template")) == "Chequered flag."
    assert phrases.say(Callout("race_summary", "template_quit")) == "Session done."
    assert phrases.say(Callout("fuel", "template")) == "Fuel. remaining."
    assert phrases.say(Callout("lap_time", "delta_slower", CalloutSlots(lap=2))) == (
        "Lap two. over best."
    )
    joined = Callout("start_brief", "template_joined", CalloutSlots(lap=7, position=1))
    assert phrases.say(joined) == "Back with you. Lap seven. P one."


def test_the_summary_names_the_track_ecu_sends() -> None:
    """The maintainer's decision of 2026-10-31, a departure from v1: ecu names no
    track at the start, and sends the summary's track slot only when it is sure of
    it; radio says it in the summary's opening, and nothing without it."""
    phrases = Phrases(templates(script=False))
    win = CalloutSlots(position=1, best_lap_ms=83_610, track="Sportsland SUGO")
    assert phrases.say(Callout("race_summary", "template_position_win", win)) == (
        "Chequered flag at Sportsland SUGO. Finished first. Best lap one twenty-three point six."
    )
    flag = Callout("race_summary", "template", CalloutSlots(track="Suzuka Circuit East Course"))
    assert phrases.say(flag) == "Chequered flag at Suzuka Circuit East Course."
    quit_ = CalloutSlots(laps=3, best_lap_ms=97_278, track="Sportsland SUGO")
    assert phrases.say(Callout("race_summary", "template_quit", quit_)) == (
        "Session done at Sportsland SUGO. Three laps. Best lap one thirty-seven point three."
    )
    scripted = Phrases(seed=1)
    for _ in range(8):
        text = scripted.say(Callout("race_summary", "template_position_win", win))
        assert " at Sportsland SUGO. " in text, text
        text = scripted.say(Callout("race_summary", "template_quit", quit_))
        assert " at Sportsland SUGO. " in text, text


def test_a_rules_files_own_summary_is_kept_with_a_track(tmp_path: Path) -> None:
    """A rules file's own race_summary template (or template_quit), with no
    _at_track key of its own, is said when ecu sends a track, without the track;
    one that sets the _at_track key gets it, and a null template is no override."""
    sixth = CalloutSlots(position=6, track="Sportsland SUGO")
    quit_ = CalloutSlots(laps=3, track="Sportsland SUGO")
    f = tmp_path / "rules.yaml"
    f.write_text(
        "script: false\nrules:\n  race_summary:\n"
        '    template: "Flag. {position}"\n'
        '    template_quit: ["Done. {laps}."]\n'
    )
    phrases = Phrases(load_rules_file(f))
    assert phrases.say(Callout("race_summary", "template_position", sixth)) == (
        "Flag. Finished sixth."
    )
    assert phrases.say(Callout("race_summary", "template_quit", quit_)) == "Done. Three laps."
    # With the script on too: the script's _at_track variants don't replace it.
    f.write_text('rules:\n  race_summary:\n    template: "Flag. {position}"\n')
    flag = Callout("race_summary", "template", CalloutSlots(track="Sportsland SUGO"))
    assert {Phrases(load_rules_file(f), seed=n).say(flag) for n in range(5)} == {"Flag."}

    f.write_text(
        "script: false\nrules:\n  race_summary:\n"
        '    template: "Flag. {position}"\n'
        '    template_at_track: "Flag at {track}. {position}"\n'
        "    template_quit: null\n"
    )
    phrases = Phrases(load_rules_file(f))
    assert phrases.say(Callout("race_summary", "template_position", sixth)) == (
        "Flag at Sportsland SUGO. Finished sixth."
    )
    assert phrases.say(Callout("race_summary", "template_quit", quit_)) == (
        "Session done at Sportsland SUGO. Three laps."
    )


def test_the_corners_as_v1_said_them() -> None:
    fl, fr, rl, rr = Corner.FRONT_LEFT, Corner.FRONT_RIGHT, Corner.REAR_LEFT, Corner.REAR_RIGHT
    assert corners_phrase((rr, rl)) == "Rear tyres"
    assert corners_phrase((fl, fr)) == "Front tyres"
    assert corners_phrase((rr, fr, rl, fl)) == "All tyres"
    assert corners_phrase((rl,)) == "Rear left tyre"
    assert corners_phrase((rr, fl, rl)) == "Front left, rear left and rear right tyres"
    assert corners_phrase((Corner.UNSPECIFIED,)) == ""


def _worst_slots() -> CalloutSlots:
    return CalloutSlots(
        lap=999,
        lap_time_ms=599_999,
        delta_ms=-59_999,
        laps=999,
        laps_left=999,
        position=40,
        previous_position=39,
        total_cars=40,
        track="Willow Springs International Raceway: Big Willow",
        corners=(Corner.FRONT_LEFT, Corner.REAR_LEFT, Corner.REAR_RIGHT),
        best_lap_ms=599_999,
        timed=True,
    )


# Words a speech splitter would take for the end of a sentence (paddock#90, from
# the review of radio#14): split_sentences splits after any full stop and a space.
ABBREVIATION = re.compile(
    r"\b(?:[A-Za-z]|approx|vs|etc|e\.g|i\.e|no|nr|st|mr|mrs|dr|min|mins|sec|secs|temp|deg"
    r"|kph|mph|km|pos|lap\s?no)\.\s",
    re.IGNORECASE,
)


def test_no_phrase_holds_an_abbreviation() -> None:
    """Every variant of every kind, said with worst-case slots, splits into whole
    sentences: nothing like "Approx. 3 s" that would pause mid-sentence."""
    table = templates()
    rng = random.Random(0)
    for rule, kinds in KINDS.items():
        for kind in kinds:
            phrases = Phrases(table, seed=rng.randrange(1 << 30))
            for _ in range(40):
                text = phrases.say(Callout(rule, kind, _worst_slots()))
                assert not ABBREVIATION.search(text), (rule, kind, text)
                assert not re.search(r"\d", text), (rule, kind, text)
                assert all(len(s) > 1 for s in split_sentences(text)), (rule, kind, text)
    for rule, keys in load_script().items():
        for key, variants in keys.items():
            for variant in variants:
                assert not ABBREVIATION.search(variant), (rule, key, variant)


FL, FR, RL, RR = Corner.FRONT_LEFT, Corner.FRONT_RIGHT, Corner.REAR_LEFT, Corner.REAR_RIGHT

# The sentences the rule issues quote (paddock#91 to #98), with the kind and slots
# ecu sends for each, said with v1's built-in phrases (`script: false`), as their
# recordings were checked in v1. ecu's rules (#89 on) take these as the words their
# callouts must give. #91's summary capitalises "One forty-one point zero" and "One
# point five"; v1 says them in lower case, as here. #95's AC5 names the circuit as
# GT7 writes it; v1 says "Spa Francorchamps", without the hyphen.
ISSUE_QUOTES: list[tuple[str, Callout, str]] = [
    # #91 lap_time
    (
        "#91",
        Callout(
            "lap_time", "delta_slower", CalloutSlots(lap=3, lap_time_ms=101_000, delta_ms=1500)
        ),
        "Lap three. one forty-one point zero. one point five over best.",
    ),
    (
        "#91",
        Callout("lap_time", "delta_first", CalloutSlots(lap=1, lap_time_ms=57_100)),
        "Lap one. fifty-seven point one.",
    ),
    (
        "#91",
        Callout("lap_time", "template_in_lap", CalloutSlots(lap=3, lap_time_ms=82_500)),
        "In lap, one twenty-two point five.",
    ),
    (
        "#91",
        Callout("lap_time", "template_out_lap", CalloutSlots(lap=4, lap_time_ms=69_900)),
        "Out lap, one oh nine point nine.",
    ),
    (
        "#91",
        Callout(
            "lap_time", "template_new_best", CalloutSlots(lap=5, lap_time_ms=59_500, delta_ms=-1000)
        ),
        "Lap five. fifty-nine point five. New best by one point zero.",
    ),
    (
        "#91",
        Callout("lap_time", "delta_first", CalloutSlots(lap=1, lap_time_ms=51_000)),
        "Lap one. fifty-one point zero.",
    ),
    (
        "#91",
        Callout("lap_time", "delta_first", CalloutSlots(lap=2, lap_time_ms=53_100)),
        "Lap two. fifty-three point one.",
    ),
    (
        "#91",  # AC5: 0.04 s slower in the raw times, equal in tenths
        Callout("lap_time", "delta_equal", CalloutSlots(lap=4, lap_time_ms=60_040, delta_ms=40)),
        "Lap four. one oh zero point zero. equal to best.",
    ),
    # #92 fuel
    ("#92", Callout("fuel", "template", CalloutSlots(laps=3)), "Fuel. three laps remaining."),
    ("#92", Callout("fuel", "template_pit_next"), "Box next lap."),
    ("#92", Callout("fuel", "template", CalloutSlots(laps=2)), "Fuel. two laps remaining."),
    ("#92", Callout("fuel", "template_pit"), "Pit this lap."),
    ("#92", Callout("fuel", "template_critical"), "Fuel critical. Less than one lap."),
    (
        "#92",
        Callout("fuel", "template_short", CalloutSlots(laps=3)),
        "Fuel. Just under three laps.",
    ),
    ("#92", Callout("fuel", "template_tight"), "Fuel to the flag, just. Save a little."),
    ("#92", Callout("fuel", "template_last_lap_enough"), "Fuel is fine to the end."),
    # #93 final_lap
    ("#93", Callout("final_lap", "template"), "Final lap."),
    # #94 position
    *[
        (
            "#94",
            Callout(
                "position",
                "template_gained" if pos < was else "template_lost",
                CalloutSlots(position=pos, previous_position=was),
            ),
            text,
        )
        for was, pos, text in [
            (7, 6, "Up to sixth."),
            (12, 18, "Down to eighteenth."),
            (10, 9, "Up to ninth."),
            (9, 8, "Up to eighth."),
            (8, 7, "Up to seventh."),
            (18, 20, "Down to twentieth."),
            (5, 10, "Down to tenth."),
        ]
    ],
    # #95 start_brief
    (
        "#95",
        Callout(
            "start_brief",
            "template",
            CalloutSlots(track="Sportsland SUGO", laps=2, position=10, total_cars=20),
        ),
        "Sportsland SUGO. Two laps. P ten of twenty.",
    ),
    (
        "#95",
        Callout("start_brief", "template", CalloutSlots(laps=2, position=10, total_cars=20)),
        "Two laps. P ten of twenty.",
    ),
    (
        "#95",
        Callout("start_brief", "template_joined", CalloutSlots(lap=7)),
        "Back with you. Lap seven.",
    ),
    (
        "#95",
        Callout("start_brief", "template_joined", CalloutSlots(lap=7, position=1, total_cars=16)),
        "Back with you. Lap seven. P one.",
    ),
    (
        "#95",
        Callout("start_brief", "template", CalloutSlots(timed=True, position=1, total_cars=20)),
        "Timed race. P one of twenty.",
    ),
    (
        "#95",
        Callout("start_brief", "template_track", CalloutSlots(track="Nurburgring 24h")),
        "Nurburgring twenty-four hour.",
    ),
    (
        "#95",
        Callout(
            "start_brief",
            "template_joined",
            CalloutSlots(lap=5, laps=12, position=7, total_cars=20),
        ),
        "Back with you. Lap five of twelve. P seven.",
    ),
    (
        "#95",
        Callout(
            "start_brief", "template_track", CalloutSlots(track="Circuit de Spa-Francorchamps")
        ),
        "Circuit de Spa Francorchamps.",
    ),
    # #96 race_summary
    (
        "#96",
        Callout("race_summary", "template_position", CalloutSlots(position=6, best_lap_ms=53_100)),
        "Chequered flag. Finished sixth. Best lap fifty-three point one.",
    ),
    (
        "#96",
        Callout("race_summary", "template_quit", CalloutSlots(laps=7, best_lap_ms=49_700)),
        "Session done. Seven laps. Best lap forty-nine point seven.",
    ),
    (
        "#96",
        Callout(
            "race_summary", "template_position_win", CalloutSlots(position=1, best_lap_ms=59_500)
        ),
        "Chequered flag. Finished first. Best lap fifty-nine point five.",
    ),
    # #97 temps
    (
        "#97",
        Callout("temps", "template_tyre_hot", CalloutSlots(corners=(RL, RR))),
        "Rear tyres hot.",
    ),
    (
        "#97",
        Callout("temps", "template_tyre_hot", CalloutSlots(corners=(FL, RL))),
        "Front left and rear left tyres hot.",
    ),
    (
        "#97",
        Callout("temps", "template_tyre_hot", CalloutSlots(corners=(FL, FR, RL, RR))),
        "All tyres hot.",
    ),
    # #98 telemetry
    ("#98", Callout("telemetry", "template_lost"), "Lost telemetry."),
    ("#98", Callout("telemetry", "template_back"), "Telemetry back."),
]


@pytest.mark.parametrize(
    ("issue", "callout", "text"), ISSUE_QUOTES, ids=[f"{i}-{t}" for i, _, t in ISSUE_QUOTES]
)
def test_the_rule_issues_quotes(issue: str, callout: Callout, text: str) -> None:
    """paddock#90's triage: the spoken text #91 to #98 quote is checked here."""
    assert Phrases(templates(script=False)).say(callout) == text


def test_from_message_reads_unset_slots_as_none() -> None:
    m = callout_pb2.Callout()
    json_format.Parse((CONTRACTS / "callout_position.json").read_text(), m)
    m.slots.corners.extend([car_pb2.CORNER_REAR_LEFT, car_pb2.CORNER_REAR_RIGHT])
    m.slots.corners.append(9)  # type: ignore[arg-type]
    view = from_message(m)
    assert view == Callout(
        "position",
        "template_lost",
        CalloutSlots(
            position=12, previous_position=10, corners=(Corner.REAR_LEFT, Corner.REAR_RIGHT)
        ),
    )
    zero = callout_pb2.Callout(rule="fuel", kind="template")
    zero.slots.laps = 0
    assert from_message(zero).slots.laps == 0, "set to 0 is not unset"


def test_missing_names_the_slots_a_kind_says() -> None:
    assert missing(Callout("fuel", "template")) == ["laps"]
    assert missing(Callout("fuel", "template", CalloutSlots(laps=0))) == []
    assert missing(Callout("lap_time", "delta_slower", CalloutSlots(lap=3))) == [
        "lap_time_ms",
        "delta_ms",
    ]
    assert missing(Callout("temps", "template_tyre_hot")) == ["corners"]
    assert missing(Callout("start_brief", "template")) == []
    assert missing(Callout("", "", text="Radio check.")) == []
    # Every kind it lists is one radio phrases.
    for rule, kind in NEEDS:
        assert kind in KINDS[rule], (rule, kind)
