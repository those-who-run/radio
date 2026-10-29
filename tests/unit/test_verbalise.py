"""v1.0.0's tests/test_verbalise.py, carried with the code (paddock#90)."""

from __future__ import annotations

import random

import pytest

from radio.verbalise import (
    delta_words,
    lap_time_words,
    laps_words,
    number_words,
    percent_words,
    spoken_delta_ms,
)


def test_tenths_round_half_up_not_to_even() -> None:
    assert lap_time_words(102250) == "one forty-two point three"
    assert lap_time_words(102350) == "one forty-two point four"
    assert lap_time_words(102249) == "one forty-two point two"
    assert delta_words(50) == "zero point one"
    assert delta_words(-50) == "zero point one"
    assert delta_words(49) == "zero point zero"


def test_number_words_thousands_and_limits() -> None:
    assert number_words(1000) == "one thousand"
    assert number_words(1005) == "one thousand and five"
    assert number_words(1234) == "one thousand two hundred and thirty-four"
    assert number_words(7500) == "seven thousand five hundred"
    assert number_words(999_999) == (
        "nine hundred and ninety-nine thousand nine hundred and ninety-nine"
    )
    assert number_words(-1234) == "minus one thousand two hundred and thirty-four"
    with pytest.raises(ValueError):
        number_words(1_000_000)
    with pytest.raises(ValueError):
        number_words(1.5)  # type: ignore[arg-type]


def test_percent_words_negatives_and_non_finite() -> None:
    assert percent_words(-3) == "minus three percent"
    assert percent_words(-2.5) == "minus three percent"
    assert percent_words(-0.4) == "zero percent"
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            percent_words(bad)


_WORDS = {number_words(n): n for n in range(100)}


def _words_to_tenths(words: str) -> int:
    """Invert lap_time_words / delta_words for the test: back to whole tenths."""
    head, tenths = words.split(" point ")
    nums = [_WORDS[w] for w in head.split() if w != "oh"]
    minutes, seconds = (0, nums[0]) if len(nums) == 1 else nums
    return (minutes * 60 + seconds) * 10 + _WORDS[tenths]


def test_spoken_delta_matches_the_spoken_times() -> None:
    rng = random.Random(150)
    pairs = [(rng.randint(1, 600_000), rng.randint(1, 600_000)) for _ in range(5000)]
    pairs += [(102_250, 102_350), (102_049, 101_950), (93_896, 93_892), (59_950, 60_049)]
    for ms, best in pairs:
        diff = spoken_delta_ms(ms, best)
        if abs(diff) >= 60_000:
            assert delta_words(diff) == "over a minute"
            continue
        spoken = _words_to_tenths(lap_time_words(ms)) - _words_to_tenths(lap_time_words(best))
        assert abs(spoken) == _words_to_tenths(delta_words(diff)), (ms, best)
        assert (diff == 0) == (lap_time_words(ms) == lap_time_words(best)), (ms, best)


def test_number_words() -> None:
    assert number_words(0) == "zero"
    assert number_words(7) == "seven"
    assert number_words(13) == "thirteen"
    assert number_words(20) == "twenty"
    assert number_words(42) == "forty-two"
    assert number_words(100) == "one hundred"
    assert number_words(115) == "one hundred and fifteen"


def test_lap_time_words() -> None:
    assert lap_time_words(102300) == "one forty-two point three"
    assert lap_time_words(67200) == "one oh seven point two"
    assert lap_time_words(42300) == "forty-two point three"
    assert lap_time_words(100000) == "one forty point zero"
    assert lap_time_words(0) == "no time"


def test_lap_time_rounds_to_tenths() -> None:
    assert lap_time_words(102349) == "one forty-two point three"
    assert lap_time_words(102350) == "one forty-two point four"
    assert lap_time_words(119960) == "two oh zero point zero"


def test_delta_words() -> None:
    assert delta_words(-412) == "zero point four"
    assert delta_words(1500) == "one point five"
    assert delta_words(61000) == "over a minute"


def test_laps_words() -> None:
    assert laps_words(1) == "one lap"
    assert laps_words(3) == "three laps"


def test_percent_words_rounds_half_up() -> None:
    from radio.verbalise import percent_words

    assert percent_words(37.5) == "thirty-eight percent"
    assert percent_words(0.4) == "zero percent"


def test_track_words_spells_out_what_the_voice_would_mangle() -> None:
    from radio.verbalise import track_words

    assert track_words("Nurburgring 24h") == "Nurburgring twenty-four hour"
    assert track_words("Nurburgring GP") == "Nurburgring G P"
    assert track_words("Alsace - Village Reverse") == "Alsace. Village Reverse"
    assert track_words("Willow Springs International Raceway: Big Willow") == (
        "Willow Springs International Raceway. Big Willow"
    )
    assert (
        track_words("Kyoto Driving Park - Yamagiwa+Miyabi")
        == "Kyoto Driving Park. Yamagiwa and Miyabi"
    )
    assert track_words("24 Heures du Mans Racing Circuit No Chicane") == "Le Mans No Chicane"
    assert track_words("Fuji International Speedway (Short)") == "Fuji International Speedway Short"


def test_tidy_sentence() -> None:
    from radio.verbalise import tidy_sentence

    assert tidy_sentence("Lap one.  one forty.  ") == "Lap one. one forty."
    assert tidy_sentence("Final lap") == "Final lap."


def test_tidy_sentence_drops_empty_slots_and_doubled_stops() -> None:
    from radio.verbalise import tidy_sentence

    assert tidy_sentence("Lap one. fifty-seven point one. .") == "Lap one. fifty-seven point one."
    assert tidy_sentence("Lap two. x. zero point eight over best..") == (
        "Lap two. x. zero point eight over best."
    )
    assert tidy_sentence("Race over. . Best lap y.. ") == "Race over. Best lap y."
    assert tidy_sentence("Box!") == "Box!"
    assert tidy_sentence("Final lap") == "Final lap."


def test_tidy_sentence_with_nothing_left_is_empty() -> None:
    from radio.verbalise import tidy_sentence

    assert tidy_sentence("") == ""
    assert tidy_sentence(" . . ") == ""
