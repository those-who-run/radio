"""Pick a phrase for a callout: one template, or one of several variants (paddock#90).

Carried from v1.0.0: the `Phrasebook` is v1's `phrasing.py` as it is, and the
templates are laid out as v1's `config.load_rules_config` laid them: each rule's
built-in phrases (`DEFAULTS`, v1's `DEFAULT_RULES` and the `default` arguments in
its `rules/*.py`), then the bundled script (`data/script.yaml`) unless
`script: false`, then a rules file's own templates, key by key.

Any template key may hold a list instead of a string. A variant is picked at
random, never the one spoken last for that key, so a long race does not hear the
same sentence lap after lap. ``first`` returns the first key that is configured,
which is how a situational pool ("template_gained_lead") stands in for the
general one.

A key set to null (a blank ``template:`` in the YAML) means "use the built-in
phrase", the same as leaving it out; null entries in a list are skipped. Values
that are neither strings, null nor lists of strings are refused when the
templates are loaded (`validate_templates`, v1's `config.validate_templates`).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# Each rule's built-in phrases: the `default` argument of every template() and
# first_template() call in v1's rules/*.py, which v1's DEFAULT_RULES repeats. With
# `script: false` they are v1's whole phrasing. Three keys ecu sends have no
# script.yaml entry and only these: template_joined, template_joined_position and
# delta_first.
DEFAULTS: dict[str, dict[str, str]] = {
    "lap_time": {
        "template": "Lap {lap}. {time}. {delta}",
        "delta_first": "",
        "delta_equal": "equal to best",
        "delta_slower": "{delta} over best",
        "delta_faster": "{delta} under best",
        "template_new_best": "Lap {lap}. {time}. New best by {gap}.",
        "template_in_lap": "In lap, {time}.",
        "template_out_lap": "Out lap, {time}.",
        "template_pit_lap": "Pit lap, {time}.",
    },
    "fuel": {
        "template": "Fuel. {laps} remaining.",
        "template_short": "Fuel. Just under {laps}.",
        "template_critical": "Fuel critical. Less than one lap.",
        "template_pit_next": "Box next lap.",
        "template_pit": "Pit this lap.",
        "template_tight": "Fuel to the flag, just. Save a little.",
        "template_last_lap_enough": "Fuel is fine to the end.",
    },
    "final_lap": {"template": "Final lap."},
    "position": {
        "template_gained": "Up to {position}.",
        "template_lost": "Down to {position}.",
    },
    "start_brief": {
        "template_track": "{track}.",
        "template_laps": "{laps}.",
        "template_laps_to_go": "{laps} to go.",
        "template_timed": "Timed race.",
        "template_position": "P {p} of {total}.",
        "template_joined": "Back with you. Lap {lap}.",
        "template_joined_position": "P {p}.",
    },
    "race_summary": {
        "template": "Chequered flag. {position} {best}",
        "template_position": "Finished {position}.",
        "template_best": "Best lap {time}.",
        "template_quit": "Session done. {laps}. {best}",
    },
    "temps": {
        "template_tyre_hot": "{corners} hot.",
        "template_tyres_cold": "Tyres cold.",
        "template_water": "Water temperature high.",
        "template_oil": "Oil temperature high.",
    },
    "telemetry": {
        "template_lost": "Lost telemetry.",
        "template_back": "Telemetry back.",
    },
}

# v2's own built-in phrases, for keys v1 had none for. race_summary's
# template_track and template_quit_track are its summaries naming the track: ecu
# sends the track slot at the end only when it is sure of it, and names none at
# the start (the maintainer's decision of 2026-10-31, a departure from v1: a wrong
# track in VR is worse than none).
V2_DEFAULTS: dict[str, dict[str, str]] = {
    "race_summary": {
        "template_track": "Chequered flag at {track}. {position} {best}",
        "template_quit_track": "Session done at {track}. {laps}. {best}",
    },
}

# The keys a rules file's template settings start with (v1's TEMPLATE_PREFIXES): the
# rest of a v1 rules file (priorities, thresholds) belongs to ecu, and is ignored.
TEMPLATE_PREFIXES = ("template", "delta_")

# The placeholders each key is formatted with (v1's tests/test_script.py, with the
# keys only built-in phrases have). A key not listed is formatted with none: v1
# spoke fuel's, final_lap's and the alarms' plain phrases unformatted, and a
# placeholder in one would be said as it is.
PLACEHOLDERS: dict[str, dict[str, frozenset[str]]] = {
    "lap_time": {
        "template": frozenset({"lap", "time", "delta"}),
        "template_new_best": frozenset({"lap", "time", "gap", "delta"}),
        "template_new_best_small": frozenset({"lap", "time", "gap", "delta"}),
        "delta_slower": frozenset({"delta"}),
        "delta_slower_small": frozenset({"delta"}),
        "delta_slower_large": frozenset({"delta"}),
        "delta_faster": frozenset({"delta"}),
        "template_in_lap": frozenset({"lap", "time"}),
        "template_out_lap": frozenset({"lap", "time"}),
        "template_pit_lap": frozenset({"lap", "time"}),
    },
    "fuel": {
        "template": frozenset({"laps"}),
        "template_short": frozenset({"laps"}),
    },
    "position": {
        key: frozenset({"position", "p"})
        for key in (
            "template_gained",
            "template_gained_lead",
            "template_gained_podium",
            "template_lost",
            "template_lost_lead",
        )
    },
    "start_brief": {
        "template_track": frozenset({"track"}),
        "template_laps": frozenset({"laps"}),
        "template_laps_to_go": frozenset({"laps"}),
        "template_position": frozenset({"p", "position", "total"}),
        "template_joined": frozenset({"lap"}),
        "template_joined_position": frozenset({"p", "position", "total"}),
    },
    "race_summary": {
        "template": frozenset({"position", "best", "fuel"}),
        "template_position": frozenset({"position", "p"}),
        "template_position_win": frozenset({"position", "p"}),
        "template_position_podium": frozenset({"position", "p"}),
        "template_best": frozenset({"time"}),
        "template_quit": frozenset({"laps", "best"}),
        "template_track": frozenset({"track", "position", "best", "fuel"}),
        "template_quit_track": frozenset({"track", "laps", "best"}),
    },
    "temps": {"template_tyre_hot": frozenset({"corners"})},
}

# Each rule's templates, by key: a string, None (the built-in phrase), or a list of
# variants.
Templates = dict[str, dict[str, Any]]


class TemplateError(ValueError):
    """A template is not text, or the rules file holding it can't be read."""


class Phrasebook:
    def __init__(self, cfg: Mapping[str, Any], rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.rng = rng or random.Random()
        self._last: dict[str, str] = {}

    def has(self, key: str) -> bool:
        value = self.cfg.get(key)
        if isinstance(value, list):
            return any(v is not None for v in value)
        return value is not None

    def pick(self, key: str, default: str) -> str:
        value = self.cfg.get(key)
        if value is None:
            return default
        if not isinstance(value, Sequence) or isinstance(value, str):
            return str(value)
        variants = [str(v) for v in value if v is not None]
        if not variants:
            return default
        fresh = [v for v in variants if v != self._last.get(key)] or variants
        choice = self.rng.choice(fresh)
        self._last[key] = choice
        return choice

    def first(self, keys: Sequence[str], default: str) -> str:
        for key in keys:
            if self.has(key):
                return self.pick(key, default)
        return default


def load_script() -> Templates:
    """The bundled script's variants, by rule and key (data/script.yaml's `rules:`)."""
    text = resources.files("radio").joinpath("data", "script.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    return {rule: dict(keys) for rule, keys in doc["rules"].items()}


def templates(script: bool = True, rules: Mapping[str, Any] | None = None) -> Templates:
    """Each rule's templates as v1 laid them out: the built-in phrases, the script's
    variants over them unless script is False, and rules (a rules file's `rules:`
    section, rule by rule) over both, key by key. Only template keys are taken from
    rules; anything else in it is a rule setting, ecu's."""
    out: Templates = {rule: dict(keys) for rule, keys in DEFAULTS.items()}
    layers: list[Mapping[str, Any]] = [load_script()] if script else []
    if rules is not None:
        validate_templates(rules)
        layers.append(rules)
    for layer in layers:
        for rule, keys in layer.items():
            if not isinstance(keys, Mapping):
                continue
            mine = out.setdefault(str(rule), {})
            for key, value in keys.items():
                if str(key).startswith(TEMPLATE_PREFIXES):
                    mine[str(key)] = list(value) if isinstance(value, list) else value
    return out


def validate_templates(rules: Mapping[str, Any]) -> None:
    """Every template key (``template*``, ``delta_*``) holds a string, null (the
    built-in phrase) or a list of strings and nulls; anything else would be spoken as
    its Python repr, so it is a TemplateError naming the key."""
    if not isinstance(rules, Mapping):
        raise TemplateError("rules must be a mapping of rule name to settings")
    for rule, settings in rules.items():
        if not isinstance(settings, Mapping):
            continue
        for key, value in settings.items():
            if not str(key).startswith(TEMPLATE_PREFIXES) or value is None:
                continue
            if isinstance(value, str):
                variants = [value]
            elif isinstance(value, list) and all(v is None or isinstance(v, str) for v in value):
                variants = [v for v in value if v is not None]
            else:
                raise TemplateError(
                    f"rules.{rule}.{key} must be a string or a list of strings, got {value!r}"
                )
            for variant in variants:
                _check_placeholders(str(rule), str(key), variant)


def _check_placeholders(rule: str, key: str, template: str) -> None:
    """Format template as radio will, every slot it fills a word, so a placeholder
    its key doesn't fill ("{lap}" in a position phrase) or can't take ("{p.x}",
    "{p[0]}", "{p:d}") is refused when the templates load, not when it is spoken. A
    key radio says unformatted, as v1 did, or doesn't phrase from, is left alone."""
    names = PLACEHOLDERS.get(rule, {}).get(key)
    if not names:
        return
    try:
        template.format(**dict.fromkeys(names, ""))
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as err:
        raise TemplateError(
            f"rules.{rule}.{key}: {template!r} can't be filled with {sorted(names)}: {err!r}"
        ) from err


def load_rules_file(path: Path) -> Templates:
    """The templates for a v1 rules file at path: its `script:` (true when left out)
    and the template keys of its `rules:` section. A template that is not text is a
    TemplateError, so a typo stops radio at start rather than being spoken."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise TemplateError(f"{path}: {err}") from err
    if doc is None:
        doc = {}
    if not isinstance(doc, Mapping):
        raise TemplateError(f"{path}: not a mapping")
    try:
        return templates(script=bool(doc.get("script", True)), rules=doc.get("rules") or {})
    except TemplateError as err:
        raise TemplateError(f"{path}: {err}") from err
