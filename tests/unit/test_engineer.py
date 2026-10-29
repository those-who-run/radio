"""The engineer and the roster's choice of one for a session (paddock#60)."""

from pathlib import Path

import pytest

from radio.callout import Callout, CalloutSlots
from radio.engineer import DEFAULT, Engineer, Roster, from_settings, roster
from radio.phrases import Phrases
from radio.phrasing import templates
from radio.settings import Settings
from radio.voice import VoiceSettings


def lap(n: int) -> Callout:
    return Callout(
        rule="lap_time",
        kind="delta_slower",
        slots=CalloutSlots(lap=n, lap_time_ms=64611, delta_ms=700),
    )


def test_v1_is_the_settings_voice_and_script() -> None:
    table = templates(script=False)
    voice = VoiceSettings(length_scale=1.1)
    s = Settings(voice_model=Path("mine.onnx"), voice=voice, templates=table)
    e = from_settings(s)
    assert (e.name, e.voice_model, e.voice, e.templates, e.personality) == (
        DEFAULT,
        Path("mine.onnx"),
        voice,
        table,
        None,
    )
    assert roster(s).pick("01SESSION").name == DEFAULT


def test_an_engineers_seeded_words_are_v1s() -> None:
    # The phrasebook seeding is unchanged: v1's engineer with a seed says what
    # Phrases with that seed says.
    e = from_settings(Settings())
    said = [e.phrases("01SESSION").say(lap(n)) for n in range(2, 12)]
    expected = Phrases(templates(), seed="01SESSION")
    assert said != [said[0]] * len(said)
    a, b = e.phrases("01SESSION"), expected
    assert [a.say(lap(n)) for n in range(2, 12)] == [b.say(lap(n)) for n in range(2, 12)]


def test_the_roster_gives_the_configured_one_else_the_default() -> None:
    v1 = from_settings(Settings())
    other = Engineer("other", Path("other.onnx"), VoiceSettings(), templates(script=False))
    assert Roster([v1, other]).pick("a") is v1
    assert Roster([v1, other]).pick("") is v1
    assert Roster([v1, other], configured="other").pick("a") is other
    with pytest.raises(ValueError, match="no engineer 'nobody'"):
        Roster([v1], configured="nobody")
    with pytest.raises(ValueError, match="no engineers"):
        Roster([])
