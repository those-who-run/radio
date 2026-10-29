"""The voice: its settings from a file, and its model checked against the sha256 sums
radio ships (paddock#57).

The settings are the `voice:` section of a YAML file, TWR_RADIO_RULES_FILE, with
the keys of v1's `rules.example.yaml`, so a v1 rules file that tuned the voice still
applies; radio reads that section and nothing else in the file. The `config` KV
replaces the file later (the Schumacher milestone).

The model is Piper's `en_GB-alba-medium`, as in v1: an `.onnx` file and its
`.onnx.json` config beside it, never committed. `make voice` fetches both from the
pinned rhasspy/piper-voices release and checks them against `voices/SHA256SUMS`
here, v1's file unchanged; `check_model` checks them again when radio loads them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# The voice v1 speaks with, and the one radio fetches and checks.
DEFAULT_VOICE = "en_GB-alba-medium"


class VoiceError(ValueError):
    """The voice settings file can't be read, or a model file is not the one its sum
    names."""


@dataclass(frozen=True)
class VoiceSettings:
    """The `voice:` keys, with v1's defaults (#57 R3, R4). None leaves the setting to
    the model's own config."""

    # Which speaker, in a voice that has several; alba has one.
    speaker_id: int | None = None
    # Piper's speaking rate: under 1 speaks faster.
    length_scale: float | None = 0.9
    # Variation in the voice.
    noise_scale: float | None = None
    # Variation in the timing.
    noise_w_scale: float | None = None
    # Silence between sentences, in seconds.
    sentence_pause_secs: float = 0.35

    def synthesis_key(self) -> str:
        """The settings that change a sentence's audio, for a voice's cache key. The
        pause is left out: it goes between clips, not into them."""
        return (
            f"speaker_id={self.speaker_id}|length_scale={self.length_scale}"
            f"|noise_scale={self.noise_scale}|noise_w_scale={self.noise_w_scale}"
        )


_KEYS = {f.name for f in fields(VoiceSettings)}


def load(path: Path) -> VoiceSettings:
    """The `voice:` section of the YAML file at path; a file with none gives the
    defaults. An unknown key, or a value that is not a number, is an error, so a typo
    is not silently ignored."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise VoiceError(f"{path}: {err}") from err
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise VoiceError(f"{path}: not a mapping")
    section = doc.get("voice") or {}
    if not isinstance(section, dict):
        raise VoiceError(f"{path}: voice: is not a mapping")
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise VoiceError(f"{path}: voice: unknown keys {', '.join(map(str, unknown))}")
    values: dict[str, Any] = {}
    for key, value in section.items():
        if value is None and key != "sentence_pause_secs":
            values[key] = None
        elif key == "speaker_id":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise VoiceError(f"{path}: voice: speaker_id: {value!r} is not a speaker number")
            values[key] = value
        elif isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            values[key] = float(value)
        else:
            raise VoiceError(f"{path}: voice: {key}: {value!r} is not a number of 0 or more")
    return VoiceSettings(**values)


def sums() -> dict[str, str]:
    """The sha256 of each voice file radio knows, by file name: `voices/SHA256SUMS`,
    in sha256sum's format."""
    text = resources.files("radio").joinpath("voices", "SHA256SUMS").read_text(encoding="utf-8")
    known = {}
    for line in text.splitlines():
        digest, _, name = line.partition("  ")
        known[name.strip()] = digest.strip()
    return known


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def check_model(model: Path) -> bool:
    """Check the model and its `.json` config against the sums radio ships. True when
    both are known and match; False for a voice radio has no sums for (a path set by
    hand, #57 R6), which the caller logs. A known file that doesn't match, or one
    missing, raises VoiceError."""
    known = sums()
    files = (model, Path(f"{model}.json"))
    if not all(f.name in known for f in files):
        return False
    for f in files:
        try:
            got = sha256(f)
        except OSError as err:
            raise VoiceError(f"voice model: {err} (run `make voice`)") from err
        if got != known[f.name]:
            raise VoiceError(f"voice model: {f} has sha256 {got}, not {known[f.name]}")
    return True
