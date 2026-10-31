"""A callout's sentence, as v1 would have said it (paddock#90, contracts' Callout).

ecu sends what to say, not how: the rule, the kind (the script key it chose, the
situational one where v1 had one) and the slots. `Phrases.say` turns them into
the sentence v1.0.0's rule would have built on the same values: the same
template calls in the same order, with the same defaults, the same number words
(`radio.verbalise`) and the same tidying, each rule's part here carried from its
`rules/*.py`. The text it gives is what `Synthesizer.render` speaks.

A situational key falls back to its general key when the templates lack it, as
v1's `first_template` did (`FALLBACK`). ecu owns the settings that choose a
situational key (lap_time's small_gap_ms and the rest), so radio never chooses
between pools: it only falls back.

Each rule has a phrasebook of its own, as each v1 rule did, so a key never gives
the same variant twice running within a rule. The random source is seeded per
rule from `seed` when one is given, so a replay with the same seed says the same
words.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from radio.callout import Callout, CalloutSlots, Corner
from radio.phrasing import DEFAULTS, V2_DEFAULTS, Phrasebook, Templates, templates
from radio.verbalise import (
    delta_words,
    lap_time_words,
    laps_words,
    number_words,
    ordinal_words,
    spoken_delta_ms,
    tidy_sentence,
    track_words,
)

# A situational key and the general key it stands in for (callout.proto's list).
FALLBACK: dict[tuple[str, str], str] = {
    ("lap_time", "template_new_best_small"): "template_new_best",
    ("lap_time", "delta_slower_small"): "delta_slower",
    ("lap_time", "delta_slower_large"): "delta_slower",
    ("position", "template_gained_lead"): "template_gained",
    ("position", "template_gained_podium"): "template_gained",
    ("position", "template_lost_lead"): "template_lost",
    ("race_summary", "template_position_win"): "template_position",
    ("race_summary", "template_position_podium"): "template_position",
}

# lap_time: a lap with a pit stop, no delta (v1's PIT_LAPS).
PIT_LAPS = ("template_in_lap", "template_out_lap", "template_pit_lap")
DELTAS = ("delta_first", "delta_equal", "delta_slower", "delta_slower_small", "delta_slower_large")
NEW_BESTS = ("template_new_best", "template_new_best_small")
FUEL_PLAIN = (
    "template_critical",
    "template_pit_next",
    "template_pit",
    "template_last_lap_enough",
    "template_tight",
)
GAINED = ("template_gained", "template_gained_lead", "template_gained_podium")
LOST = ("template_lost", "template_lost_lead")
# position: a ranked session's place, a rank by lap time (V2_DEFAULTS).
RANKS = ("template_rank_gained", "template_rank_lost")
FINISHED = ("template", "template_position", "template_position_win", "template_position_podium")
TEMPS_PLAIN = ("template_tyres_cold", "template_water", "template_oil")

# temps: the corners in GT7's order, as v1's CORNERS.
CORNER_NAMES = {
    Corner.FRONT_LEFT: "front left",
    Corner.FRONT_RIGHT: "front right",
    Corner.REAR_LEFT: "rear left",
    Corner.REAR_RIGHT: "rear right",
}


# The slots each kind's sentence says, by rule and kind (callout.proto's list): one
# that is not set is not said, which leaves a broken sentence ("Fuel. remaining."),
# so radio warns that ecu sent less than its kind needs (`missing`). A slot the
# sentence can do without (the brief's parts, race_summary's best, template_joined's
# laps) or that radio doesn't say (position's previous_position) isn't listed.
NEEDS: dict[tuple[str, str], tuple[str, ...]] = {
    **{("lap_time", k): ("lap", "lap_time_ms") for k in (*PIT_LAPS, "delta_first")},
    **{("lap_time", k): ("lap", "lap_time_ms", "delta_ms") for k in (*DELTAS[1:], *NEW_BESTS)},
    ("fuel", "template"): ("laps",),
    ("fuel", "template_short"): ("laps",),
    **{("position", k): ("position",) for k in (*GAINED, *LOST, *RANKS)},
    ("start_brief", "template_track"): ("track",),
    ("start_brief", "template_ranked"): ("total_cars",),
    ("start_brief", "template_joined"): ("lap",),
    **{("race_summary", k): ("position",) for k in FINISHED[1:]},
    ("race_summary", "template_quit"): ("laps",),
    ("temps", "template_tyre_hot"): ("corners",),
}


def missing(callout: Callout) -> list[str]:
    """The slots callout's kind says that it doesn't set (`NEEDS`)."""
    needs = NEEDS.get((callout.rule, callout.kind), ())
    return [name for name in needs if getattr(callout.slots, name) in (None, "", ())]


class PhraseError(ValueError):
    """A callout radio can't phrase: a rule or kind it doesn't know, with no text, or
    a template with a placeholder its kind doesn't fill."""


def _num(n: int | None) -> str:
    return "" if n is None else number_words(n)


def _ordinal(n: int | None) -> str:
    return "" if n is None else ordinal_words(n)


def _time(ms: int | None) -> str:
    return "" if ms is None else lap_time_words(ms)


def _gap(s: CalloutSlots) -> str:
    """The delta as v1 said it: the spoken difference between the lap and the best
    before it (lap_time_ms less delta_ms), each rounded half up to tenths."""
    if s.delta_ms is None or s.lap_time_ms is None:
        return ""
    return delta_words(spoken_delta_ms(s.lap_time_ms, s.lap_time_ms - s.delta_ms))


def _sentences(parts: list[str]) -> str:
    """Each part is a sentence of its own; templates built from number words start
    lowercase (v1's start_brief._sentences)."""
    return tidy_sentence(" ".join(p[:1].upper() + p[1:] for p in parts if p))


def corners_phrase(corners: tuple[Corner, ...]) -> str:
    """The tyres in an alarm as v1 said them: "All tyres", "Rear tyres", "Front left
    and rear left tyres" (v1's temps._corners_phrase)."""
    names = [name for corner, name in CORNER_NAMES.items() if corner in corners]
    if len(names) == len(CORNER_NAMES):
        return "All tyres"
    if names == ["rear left", "rear right"]:
        return "Rear tyres"
    if names == ["front left", "front right"]:
        return "Front tyres"
    if not names:
        return ""
    if len(names) == 1:
        text = f"{names[0]} tyre"
    else:
        text = f"{', '.join(names[:-1])} and {names[-1]} tyres"
    return text[:1].upper() + text[1:]


class Phrases:
    """Turns callouts into sentences with v1's templates: `templates()` by default,
    the script over the built-in phrases."""

    def __init__(self, table: Templates | None = None, seed: int | str | None = None) -> None:
        self.table = templates() if table is None else table
        self._books: dict[str, Phrasebook] = {}
        self._seed = seed
        self._rules: dict[str, Callable[[Phrasebook, str, CalloutSlots], str | None]] = {
            "lap_time": self._lap_time,
            "fuel": self._fuel,
            "final_lap": self._final_lap,
            "position": self._position,
            "start_brief": self._start_brief,
            "race_summary": self._race_summary,
            "temps": self._temps,
            "telemetry": self._telemetry,
        }

    def say(self, callout: Callout) -> str:
        """The sentence for callout: phrased from its kind and slots when radio knows
        the rule and kind, else its text as it is. "" is nothing to say."""
        phrase = self._rules.get(callout.rule)
        if callout.kind and phrase is not None:
            try:
                text = phrase(self._book(callout.rule), callout.kind, callout.slots)
            except (KeyError, IndexError, ValueError, AttributeError, TypeError) as err:
                raise PhraseError(f"{callout.rule}.{callout.kind}: {err!r}") from err
            if text is not None:
                return text
        if callout.text:
            return callout.text
        raise PhraseError(f"no phrase for rule {callout.rule!r} kind {callout.kind!r}")

    def _book(self, rule: str) -> Phrasebook:
        book = self._books.get(rule)
        if book is None:
            rng = random.Random() if self._seed is None else random.Random(f"{self._seed}:{rule}")
            book = self._books[rule] = Phrasebook(self.table.get(rule, {}), rng)
        return book

    @staticmethod
    def _first(book: Phrasebook, rule: str, kind: str, default: str) -> str:
        """v1's first_template: the situational key if configured, else its general."""
        general = FALLBACK.get((rule, kind))
        keys = [kind] if general is None else [kind, general]
        return book.first(keys, default)

    # Each rule's part follows its v1 rule line by line, so the picks come in v1's
    # order: with the same seed, the same variants.

    def _lap_time(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        d = DEFAULTS["lap_time"]
        lap, time = _num(s.lap), _time(s.lap_time_ms)
        if kind in PIT_LAPS:
            text = book.pick(kind, d[kind]).format(lap=lap, time=time)
            return tidy_sentence(tidy_sentence(text))
        if kind in NEW_BESTS:
            gap = _gap(s)
            text = self._first(book, "lap_time", kind, d["template_new_best"]).format(
                lap=lap,
                time=time,
                gap=gap,
                delta=book.pick("delta_faster", d["delta_faster"]).format(delta=gap),
            )
            return tidy_sentence(tidy_sentence(text))
        if kind == "delta_first":
            delta = book.pick("delta_first", d["delta_first"])
        elif kind == "delta_equal":
            delta = book.pick("delta_equal", d["delta_equal"])
        elif kind in DELTAS:
            delta = self._first(book, "lap_time", kind, d["delta_slower"]).format(delta=_gap(s))
        else:
            return None
        text = book.pick("template", d["template"]).format(lap=lap, time=time, delta=delta)
        return tidy_sentence(tidy_sentence(text))

    def _fuel(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        d = DEFAULTS["fuel"]
        if kind in ("template", "template_short"):
            laps = "" if s.laps is None else laps_words(s.laps)
            return tidy_sentence(book.pick(kind, d[kind]).format(laps=laps))
        if kind in FUEL_PLAIN:
            return tidy_sentence(book.pick(kind, d[kind]))
        return None

    def _final_lap(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        if kind != "template":
            return None
        return tidy_sentence(book.pick("template", DEFAULTS["final_lap"]["template"]))

    def _position(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        if kind in RANKS:
            text = book.pick(kind, V2_DEFAULTS["position"][kind])
            return tidy_sentence(text.format(position=_ordinal(s.position), p=_num(s.position)))
        if kind in GAINED:
            default = DEFAULTS["position"]["template_gained"]
        elif kind in LOST:
            default = DEFAULTS["position"]["template_lost"]
        else:
            return None
        text = self._first(book, "position", kind, default).format(
            position=_ordinal(s.position), p=_num(s.position)
        )
        return tidy_sentence(text)

    def _start_brief(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        d = DEFAULTS["start_brief"]

        def track_phrase() -> str:
            text = book.pick("template_track", d["template_track"])
            return tidy_sentence(text.format(track=track_words(s.track)))

        def position_phrase(key: str) -> str:
            return book.pick(key, d[key]).format(
                p=_num(s.position), position=_ordinal(s.position), total=_num(s.total_cars)
            )

        if kind == "template_track":
            return tidy_sentence(track_phrase()) if s.track else ""
        if kind == "template_ranked":
            text = book.pick(kind, V2_DEFAULTS["start_brief"][kind])
            return tidy_sentence(text.format(total=_num(s.total_cars)))
        if kind == "template_joined":
            lap = _num(s.lap)
            if s.laps is not None:
                lap += f" of {number_words(s.laps)}"
            parts = [book.pick("template_joined", d["template_joined"]).format(lap=lap)]
            if s.position is not None:
                parts.append(position_phrase("template_joined_position"))
            return tidy_sentence(_sentences(parts))
        if kind != "template":
            return None
        parts = []
        if s.track:
            parts.append(track_phrase())
        if s.laps is not None:
            parts.append(
                book.pick("template_laps", d["template_laps"]).format(laps=laps_words(s.laps))
            )
        elif s.laps_left:
            text = book.pick("template_laps_to_go", d["template_laps_to_go"])
            parts.append(text.format(laps=laps_words(s.laps_left)))
        elif s.timed:
            parts.append(book.pick("template_timed", d["template_timed"]))
        if s.position is not None:
            parts.append(position_phrase("template_position"))
        return tidy_sentence(_sentences(parts)) if parts else ""

    def _race_summary(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        d = DEFAULTS["race_summary"]

        def best_words() -> str:
            if s.best_lap_ms is None or s.best_lap_ms <= 0:
                return ""
            text = book.pick("template_best", d["template_best"])
            return text.format(time=lap_time_words(s.best_lap_ms))

        # The summary names the track when ecu sends it, sure of it (V2_DEFAULTS).
        track = track_words(s.track) if s.track else ""

        def opening(key: str) -> str:
            if not track:
                return book.pick(key, d[key])
            key += "_at_track"
            return book.pick(key, V2_DEFAULTS["race_summary"][key])

        if kind == "template_quit":
            best = best_words()
            laps = "" if s.laps is None else laps_words(s.laps)
            text = opening("template_quit").format(
                laps=laps[:1].upper() + laps[1:], best=best, track=track
            )
            return tidy_sentence(tidy_sentence(text))
        if kind == "template_ranked":
            v2 = V2_DEFAULTS["race_summary"]
            position = ""
            if s.position is not None:
                position = book.pick("template_rank", v2["template_rank"]).format(
                    position=_ordinal(s.position), p=_num(s.position)
                )
            key = "template_ranked_at_track" if track else "template_ranked"
            text = book.pick(key, v2[key]).format(position=position, best=best_words(), track=track)
            return tidy_sentence(tidy_sentence(text))
        if kind not in FINISHED:
            return None
        position = ""
        if kind != "template":
            position = self._first(book, "race_summary", kind, d["template_position"]).format(
                position=_ordinal(s.position), p=_num(s.position)
            )
        best = best_words()
        text = opening("template").format(position=position, best=best, fuel="", track=track)
        return tidy_sentence(tidy_sentence(text))

    def _temps(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        d = DEFAULTS["temps"]
        if kind == "template_tyre_hot":
            text = book.pick(kind, d[kind]).format(corners=corners_phrase(s.corners))
            return tidy_sentence(tidy_sentence(text))
        if kind in TEMPS_PLAIN:
            return tidy_sentence(book.pick(kind, d[kind]))
        return None

    def _telemetry(self, book: Phrasebook, kind: str, s: CalloutSlots) -> str | None:
        # v1's TelemetryVoice spoke the template as it is, untidied.
        if kind in ("template_lost", "template_back"):
            return book.pick(kind, DEFAULTS["telemetry"][kind])
        return None
