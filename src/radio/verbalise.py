"""Turn numbers into short spoken phrases (paddock#90), carried from v1.0.0's
`verbalise.py` as it is: radio says a callout's slots in v1's words.

Piper reads digits acceptably but ``1:42.317`` comes out oddly, and at racing
speed a short phrase matters more than natural prose. Lap times are spoken to
one decimal place, deltas likewise.
"""

from __future__ import annotations

import math
import re

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


NUMBER_WORDS_LIMIT = 1_000_000


def number_words(n: int) -> str:
    """Integers with magnitude under a million as words, British style:
    ``1234`` -> "one thousand two hundred and thirty-four", ``1005`` -> "one
    thousand and five". Raises ``ValueError`` on anything it cannot say."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"number_words needs an int, got {n!r}")
    if n < 0:
        return "minus " + number_words(-n)
    if n >= NUMBER_WORDS_LIMIT:
        raise ValueError(f"number_words cannot say {n}: limit is {NUMBER_WORDS_LIMIT - 1}")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head if rest == 0 else f"{head} and {number_words(rest)}"
    thousands, rest = divmod(n, 1000)
    head = f"{number_words(thousands)} thousand"
    if rest == 0:
        return head
    return f"{head} and {number_words(rest)}" if rest < 100 else f"{head} {number_words(rest)}"


def tenths_half_up(ms: int) -> int:
    """Milliseconds to whole tenths of a second, ties up, in integer arithmetic:
    ``102250`` -> 1023, ``102350`` -> 1024 (``round()`` would give 1022 and 1024)."""
    return (int(ms) + 50) // 100


def spoken_delta_ms(ms: int, ref_ms: int) -> int:
    """The difference between two times as they are spoken, in milliseconds (a
    multiple of 100): the spoken delta always matches the spoken times."""
    return (tenths_half_up(ms) - tenths_half_up(ref_ms)) * 100


def _tenths(ms: int) -> tuple[int, int]:
    """Split milliseconds into (whole seconds, tenths), rounding half up."""
    return divmod(tenths_half_up(ms), 10)


def lap_time_words(ms: int) -> str:
    """``102300`` -> "one forty-two point three"; ``67200`` -> "one oh seven point two"."""
    if ms <= 0:
        return "no time"
    seconds, tenths = _tenths(ms)
    minutes, seconds = divmod(seconds, 60)
    if minutes == 0:
        return f"{number_words(seconds)} point {number_words(tenths)}"
    sec_words = f"oh {number_words(seconds)}" if seconds < 10 else number_words(seconds)
    return f"{number_words(minutes)} {sec_words} point {number_words(tenths)}"


def delta_words(ms: int) -> str:
    """Unsigned delta: ``-412`` -> "zero point four"; ``61000`` -> "over a minute"."""
    seconds, tenths = _tenths(abs(ms))
    if seconds >= 60:
        return "over a minute"
    return f"{number_words(seconds)} point {number_words(tenths)}"


def laps_words(n: int) -> str:
    return "one lap" if n == 1 else f"{number_words(n)} laps"


_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh",
    12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
    16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
    20: "twentieth",
}  # fmt: skip


def ordinal_words(n: int) -> str:
    """``6`` -> "sixth"; beyond twenty falls back to "P twenty-three"."""
    if n in _ORDINALS:
        return _ORDINALS[n]
    return f"P {number_words(n)}"


def percent_words(value: float) -> str:
    """``37.5`` -> "thirty-eight percent" (one word for the speech model, not "per
    cent"). Halves round away from zero, so ``-2.5`` is "minus three percent".
    Raises ``ValueError`` on NaN or infinity."""
    if not math.isfinite(value):
        raise ValueError(f"percent_words needs a finite value, got {value!r}")
    whole = math.floor(abs(value) + 0.5)
    return f"{number_words(-whole if value < 0 else whole)} percent"


_TRACK_SUBS = (
    ("24 Heures du Mans Racing Circuit", "Le Mans"),
    (" 24h", " twenty-four hour"),
    (" GP ", " G P "),
    (" GP", " G P"),
    ("Spa-Francorchamps", "Spa Francorchamps"),
    ("Barcelona-Catalunya", "Barcelona Catalunya"),
    (" (Short)", " Short"),
    ("+", " and "),
    (": ", ". "),
    (" - ", ". "),
)


def track_words(name: str) -> str:
    """Table names as the speech model should read them: "Nurburgring 24h" ->
    "Nurburgring twenty-four hour", "Alsace - Village" -> "Alsace. Village"."""
    for src, dst in _TRACK_SUBS:
        name = name.replace(src, dst)
    return " ".join(name.split())


_EMPTY_SENTENCES = re.compile(r"([.!?])(?:\s*\.)+")


def tidy_sentence(text: str) -> str:
    """Collapse whitespace and the empty sentences left by empty template slots
    ("{delta}." with no delta, or a slot that already ends in a full stop), and
    end with one full stop unless the text ends in ! or ?. Nothing left is "",
    not a lone full stop."""
    text = " ".join(text.split())
    text = _EMPTY_SENTENCES.sub(r"\1", text).lstrip(" .")
    text = text.rstrip(" ,")
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else text + "."
