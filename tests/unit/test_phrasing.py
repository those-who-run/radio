"""The bundled script, the templates' layers and how variants are picked: v1.0.0's
tests/test_script.py, carried with the code (paddock#90)."""

from __future__ import annotations

import random
import re
import string
from pathlib import Path

import pytest
import yaml

from radio.phrasing import (
    DEFAULTS,
    V2_DEFAULTS,
    Phrasebook,
    TemplateError,
    load_rules_file,
    load_script,
    templates,
    validate_templates,
)
from radio.phrasing import (
    PLACEHOLDERS as FILLED,
)
from radio.verbalise import tidy_sentence

# Placeholders each key is formatted with, and the situational pools a rule knows.
WORDS = {
    "lap": "two",
    "time": "one oh four point six",
    "delta": "zero point eight",
    "gap": "zero point eight",
    "laps": "two laps",
    "position": "fifth",
    "p": "five",
    "total": "sixteen",
    "track": "Sportsland SUGO",
    "best": "Best lap one oh one point two.",
    "corners": "Front left tyre",
}
PLACEHOLDERS: dict[str, dict[str, set[str]]] = {
    "lap_time": {
        "template": {"lap", "time", "delta"},
        "template_new_best": {"lap", "time", "gap", "delta"},
        "template_new_best_small": {"lap", "time", "gap", "delta"},
        "delta_first": set(),
        "delta_slower": {"delta"},
        "delta_slower_small": {"delta"},
        "delta_slower_large": {"delta"},
        "delta_faster": {"delta"},
        "delta_equal": set(),
        "template_in_lap": {"lap", "time"},
        "template_out_lap": {"lap", "time"},
        "template_pit_lap": {"lap", "time"},
    },
    "fuel": {
        "template": {"laps"},
        "template_short": {"laps"},
        "template_critical": set(),
        "template_pit_next": set(),
        "template_pit": set(),
        "template_tight": set(),
        "template_last_lap_enough": set(),
    },
    "final_lap": {"template": set()},
    "position": {
        key: {"position", "p"}
        for key in (
            "template_gained",
            "template_gained_lead",
            "template_gained_podium",
            "template_lost",
            "template_lost_lead",
            "template_rank",
            "template_rank_gained",
            "template_rank_lost",
        )
    },
    "start_brief": {
        "template_track": {"track"},
        "template_laps": {"laps"},
        "template_laps_to_go": {"laps"},
        "template_timed": set(),
        "template_position": {"p", "position", "total"},
        "template_joined": {"lap"},
        "template_joined_position": {"p", "position", "total"},
        "template_ranked": {"total"},
    },
    "race_summary": {
        "template": {"position", "best", "fuel"},
        "template_position": {"position", "p"},
        "template_position_win": {"position", "p"},
        "template_position_podium": {"position", "p"},
        "template_best": {"time"},
        "template_quit": {"laps", "best"},
        "template_at_track": {"track", "position", "best", "fuel"},
        "template_quit_at_track": {"track", "laps", "best"},
        "template_ranked": {"position", "best"},
        "template_ranked_at_track": {"track", "position", "best"},
        "template_rank": {"position", "p"},
    },
    "temps": {
        "template_tyre_hot": {"corners"},
        "template_tyres_cold": set(),
        "template_water": set(),
        "template_oil": set(),
    },
    "telemetry": {"template_lost": set(), "template_back": set()},
}
SCRIPT = load_script()
VARIANTS = [
    (rule, key, text)
    for rule, keys in SCRIPT.items()
    for key, texts in keys.items()
    for text in texts
]
BUILT_IN = [
    (rule, key, text)
    for table in (DEFAULTS, V2_DEFAULTS)
    for rule, keys in table.items()
    for key, text in keys.items()
]


def test_script_only_has_keys_the_rules_read() -> None:
    for rule, keys in SCRIPT.items():
        assert rule in PLACEHOLDERS, rule
        assert set(keys) <= set(PLACEHOLDERS[rule]), (rule, set(keys) - set(PLACEHOLDERS[rule]))


def test_the_built_in_phrases_are_keys_the_rules_read() -> None:
    for rule, keys in DEFAULTS.items():
        assert set(keys) <= set(PLACEHOLDERS[rule]), (rule, set(keys) - set(PLACEHOLDERS[rule]))


def test_every_plain_template_key_has_a_script_list() -> None:
    # The three keys with only a built-in phrase (callout.proto).
    only_built_in = {
        ("lap_time", "delta_first"),
        ("start_brief", "template_joined"),
        ("start_brief", "template_joined_position"),
    }
    for rule, keys in DEFAULTS.items():
        for key in keys:
            if (rule, key) not in only_built_in:
                assert key in SCRIPT[rule], (rule, key)


@pytest.mark.parametrize(("rule", "key", "text"), VARIANTS + BUILT_IN)
def test_variant_is_speakable(rule: str, key: str, text: str) -> None:
    """Every variant formats with worst-case slots, and an unknown slot fails (R6)."""
    used = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    assert used <= PLACEHOLDERS[rule][key], f"{rule}.{key}: unknown slot in {text!r}"
    spoken = tidy_sentence(text.format(**WORDS, fuel=""))
    assert not re.search(r"\d", spoken), spoken
    assert ".." not in spoken and ". ." not in spoken, spoken
    assert len(spoken.split()) <= 20, spoken


def test_script_lists_lead_with_the_built_in_phrase() -> None:
    for rule, keys in SCRIPT.items():
        for key, texts in keys.items():
            default = DEFAULTS[rule].get(key) or V2_DEFAULTS.get(rule, {}).get(key)
            if default:
                assert tidy_sentence(texts[0]) == tidy_sentence(default), (rule, key)


def test_templates_layer_the_script_and_the_file_wins(tmp_path: Path) -> None:
    assert isinstance(templates()["final_lap"]["template"], list)
    f = tmp_path / "rules.yaml"
    f.write_text('rules:\n  final_lap:\n    template: "Last one."\n    priority: 3\n')
    table = load_rules_file(f)
    assert table["final_lap"] == {"template": "Last one."}, "a rule setting is ecu's"
    assert isinstance(table["fuel"]["template_pit"], list)
    f.write_text("script: false\n")
    assert load_rules_file(f)["final_lap"]["template"] == "Final lap."
    assert templates(script=False) == DEFAULTS


def test_an_empty_rules_file_is_the_script(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text("")
    assert load_rules_file(f) == templates()
    f.write_text("rules:\n  final_lap: 5\n")
    assert load_rules_file(f) == templates()


def test_phrasebook_never_repeats_the_last_variant() -> None:
    book = Phrasebook({"k": ["a", "b", "c"]}, random.Random(1))
    picks = [book.pick("k", "x") for _ in range(200)]
    assert set(picks) == {"a", "b", "c"}
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))


def test_phrasebook_strings_lists_and_fallbacks() -> None:
    book = Phrasebook({"one": "only", "single": ["just"], "empty": [], "pool": ["p"]})
    assert book.pick("one", "x") == "only"
    assert [book.pick("single", "x") for _ in range(3)] == ["just"] * 3
    assert book.pick("empty", "fallback") == "fallback"
    assert book.pick("missing", "fallback") == "fallback"
    assert book.first(["missing", "empty", "pool"], "d") == "p"
    assert book.first(["missing"], "d") == "d"


GAINED = (
    "template_gained",
    "template_gained_lead",
    "template_gained_podium",
    "template_rank_gained",
)
LOST = ("template_lost", "template_lost_lead", "template_rank_lost")


def test_gaining_and_losing_a_place_never_sound_the_same() -> None:
    pos = SCRIPT["position"]
    gained = {tidy_sentence(t).lower(): k for k in GAINED for t in pos.get(k, [])}
    lost = {tidy_sentence(t).lower(): k for k in LOST for t in pos.get(k, [])}
    shared = set(gained) & set(lost)
    assert not shared, {t: (gained[t], lost[t]) for t in shared}


@pytest.mark.parametrize(
    ("key", "words"),
    [
        ("template_gained", {"up", "move", "moved", "moving", "pass", "taking", "into"}),
        ("template_gained_podium", {"up", "moved", "into"}),
        ("template_lost", {"down", "fell", "dropped", "lost"}),
        ("template_lost_lead", {"lost", "dropped", "fell"}),
        ("template_rank_gained", {"up", "moved", "puts"}),
        ("template_rank_lost", {"down", "dropped", "quicker"}),
    ],
)
def test_every_place_change_variant_says_which_way(key: str, words: set[str]) -> None:
    for text in SCRIPT["position"][key]:
        said = set(re.findall(r"[a-z]+", text.lower()))
        assert said & words, f"position.{key}: {text!r} does not say gained or lost"


def test_phrasebook_null_is_the_default() -> None:
    book = Phrasebook({"k": None, "pool": [None, "p"], "nulls": [None]})
    assert book.pick("k", "d") == "d"
    assert not book.has("k")
    assert book.pick("pool", "d") == "p"
    assert book.pick("nulls", "d") == "d"
    assert book.first(["k", "nulls", "pool"], "d") == "p"


def test_rules_file_null_template_falls_back_to_the_built_in(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text("rules:\n  final_lap:\n    template:\n")
    table = load_rules_file(f)
    assert table["final_lap"]["template"] is None
    assert Phrasebook(table["final_lap"]).pick("template", "Final lap.") == "Final lap."


@pytest.mark.parametrize("value", ["{a: b}", "42", "[Final lap., {a: b}]", "[1, 2]"])
def test_rules_file_refuses_a_template_that_is_not_text(tmp_path: Path, value: str) -> None:
    """AC5: a template of 42 is refused when the file is loaded, at start."""
    f = tmp_path / "rules.yaml"
    f.write_text(f"rules:\n  final_lap:\n    template: {value}\n")
    with pytest.raises(TemplateError, match=r"rules\.final_lap\.template"):
        load_rules_file(f)


@pytest.mark.parametrize("text", ["[not, closed", "- a list\n", "\xff"])
def test_a_rules_file_radio_cannot_read(tmp_path: Path, text: str) -> None:
    f = tmp_path / "rules.yaml"
    f.write_bytes(text.encode("latin-1"))
    with pytest.raises(TemplateError, match=str(f)):
        load_rules_file(f)
    with pytest.raises(TemplateError, match="missing"):
        load_rules_file(tmp_path / "missing.yaml")


def test_rules_must_be_a_mapping() -> None:
    with pytest.raises(TemplateError, match="mapping"):
        validate_templates([])  # type: ignore[arg-type]


def test_a_rules_file_list_replaces_the_scripts_list(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text("rules:\n  final_lap:\n    template: [Last one., null, One to go.]\n")
    assert load_rules_file(f)["final_lap"]["template"] == ["Last one.", None, "One to go."]


def test_radio_fills_the_placeholders_v1_filled() -> None:
    """The load-time check's table is this test's, less the keys said unformatted."""
    for rule, keys in PLACEHOLDERS.items():
        for key, names in keys.items():
            expected = set() if key in UNFORMATTED.get(rule, set()) else names
            assert set(FILLED.get(rule, {}).get(key, frozenset())) == expected, (rule, key)
    for rule, filled in FILLED.items():
        assert set(filled) <= set(PLACEHOLDERS[rule]), rule


# Keys v1 said as they are: a placeholder in one is not filled.
UNFORMATTED = {
    "lap_time": {"delta_first", "delta_equal"},
    "fuel": {
        "template_critical",
        "template_pit_next",
        "template_pit",
        "template_tight",
        "template_last_lap_enough",
    },
    "final_lap": {"template"},
    "start_brief": {"template_timed"},
    "temps": {"template_tyres_cold", "template_water", "template_oil"},
    "telemetry": {"template_lost", "template_back"},
}


@pytest.mark.parametrize(
    ("rule", "key", "value"),
    [
        ("position", "template_lost", "Down to {position.x}."),
        ("position", "template_lost", "Down to {position[x]}."),
        ("position", "template_gained", "Up to {p:d}."),
        ("position", "template_gained", "Up to {lap}."),
        ("lap_time", "delta_slower", "{delta} over best on lap {lap}."),
        ("race_summary", "template_best", "Best lap {time"),
        ("start_brief", "template_joined", ["Back. Lap {lap}.", "Lap {0}."]),
    ],
)
def test_a_template_with_a_bad_placeholder_is_refused_at_load(
    tmp_path: Path, rule: str, key: str, value: str | list[str]
) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text(yaml.safe_dump({"rules": {rule: {key: value}}}))
    with pytest.raises(TemplateError, match=rf"rules\.{rule}\.{key}"):
        load_rules_file(f)


def test_a_placeholder_in_a_phrase_said_as_it_is_is_left_alone(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text(
        "rules:\n  fuel:\n    template_pit: 'Pit {now}.'\n  my_rule:\n    template: '{x.y}'\n"
    )
    assert load_rules_file(f)["fuel"]["template_pit"] == "Pit {now}."
