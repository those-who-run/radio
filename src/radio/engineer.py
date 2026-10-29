"""The race engineer who says the callouts: a voice and a script (paddock#60).

ecu decides which callouts are raised, and when; an engineer decides only how one
is said: the words (its templates, and the phrasebook's picks from them) and the
voice that speaks them. Two engineers given the same callouts say the same things
in their own way, never different things.

Today there is one engineer, `v1`: v1's voice, v1's script, and the settings
radio has always read (`TWR_RADIO_VOICE_MODEL` and `TWR_RADIO_VOICE_FILE`).

The engineer is chosen per session, never once for the process, by `Roster.pick`,
so that a later roster of several can give each race its own: the one the driver
picked, else one drawn at random, seeded by the session's id so that two replays
of one recording get the same engineer and the same words. Adding a second
engineer is adding an `Engineer` to the roster `roster()` builds: its voice model
and settings and its templates; nothing that takes or orders callouts changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from radio.phrases import Phrases
from radio.phrasing import Templates
from radio.settings import Settings
from radio.voice import VoiceSettings

# v1's engineer, the only one so far.
DEFAULT = "v1"


@dataclass(frozen=True, eq=False)
class Engineer:
    """One engineer: a name, a voice and a script."""

    name: str
    # The Piper voice, an .onnx file with its .onnx.json beside it.
    voice_model: Path
    # The voice's settings, the `voice:` section of a v1 rules file.
    voice: VoiceSettings
    # The phrases, by rule and key (radio.phrasing).
    templates: Templates
    # How the engineer says things beyond the script: its wording, its choice of
    # template variants, its voice. Never which callouts are said, nor how many:
    # that is ecu's. A later issue; nothing reads it yet.
    personality: None = None

    def phrases(self, seed: str | None) -> Phrases:
        """A phrasebook for one session. Seeded, the same callouts give the same
        words: each engineer's picks come from its own phrasebook, so a second
        engineer never changes the first one's."""
        return Phrases(self.templates, seed=seed)


def from_settings(settings: Settings) -> Engineer:
    """v1's engineer, with the voice and phrases the settings name."""
    return Engineer(DEFAULT, settings.voice_model, settings.voice, settings.templates)


class Roster:
    """The engineers radio can speak with, and the choice of one for a session."""

    def __init__(self, engineers: Sequence[Engineer], configured: str | None = None) -> None:
        if not engineers:
            raise ValueError("no engineers")
        self.engineers = {e.name: e for e in engineers}
        if configured is not None and configured not in self.engineers:
            raise ValueError(
                f"no engineer {configured!r}: one of {', '.join(sorted(self.engineers))}"
            )
        self.configured = configured
        self.default = engineers[0]

    def all(self) -> list[Engineer]:
        """Every engineer, sorted by name: a stable order for a seeded pick, and
        what radio loads and checks at start."""
        return [self.engineers[name] for name in sorted(self.engineers)]

    def pick(self, session_id: str) -> Engineer:
        """The engineer for session_id: the configured one, else the default. With
        several, the else becomes a random pick seeded by session_id from `all()`,
        sorted by name so the order the roster was built in can't change it, and a
        replay of the session gets the same one."""
        if self.configured is not None:
            return self.engineers[self.configured]
        return self.default


def roster(settings: Settings) -> Roster:
    return Roster([from_settings(settings)])
