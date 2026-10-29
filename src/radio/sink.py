"""Where radio speaks a callout (paddock#56; the console and WAV sinks paddock#60,
Discord #58).

radio speaks one callout at a time and calls a sink's steps in v1's order:
`connect`, then `prepare` (the synthesis), then `play`. Between them it drops the
callout if its `expires_at` has passed, at the stage it was in (v1's
DiscordPlayer._stale), so a sink holds no staleness rules of its own. On a
replay's clock (radio.timing) the simulated speaker holds the channel for
`seconds(clip)` instead of waiting for `play`.

The console and WAV sinks are v1's `ConsolePlayer` and `WavDirPlayer`, for replays
and the ephemeral environment: they need no Discord.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from radio.piper_voice import Voices
from radio.queue import Pending
from radio.speech import Utterance, estimate
from radio.voice import VoiceSettings


class Sink[C](Protocol):
    """A sink's steps. C is what `prepare` makes for `play`, such as a WAV."""

    async def connect(self) -> bool:
        """Wait until the sink can speak: True once it can, False if it can't. It is
        cancelled when the callout goes stale meanwhile, so a sink whose join must
        outlive one callout shields it itself."""
        ...

    async def prepare(self, callout: Pending) -> C | None:
        """What play needs to say the callout's text, in its engineer's voice; None
        for nothing to say."""
        ...

    def seconds(self, clip: C) -> float:
        """How long clip takes to say."""
        ...

    async def play(self, clip: C, callout: Pending) -> None:
        """Say it, returning once it has been said."""
        ...

    def release(self, clip: C) -> None:
        """Let go of what prepare made, played or dropped."""
        ...

    async def close(self) -> None:
        """radio is stopping."""
        ...


_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_NUMBERED = re.compile(r"^(\d{3,})-.*\.wav$")


@dataclass(frozen=True)
class Clip:
    """What the console and WAV sinks say: the text, how long it takes to say, and
    its audio when it was synthesised."""

    text: str
    seconds: float
    utterance: Utterance | None = None


def line(callout: Pending) -> str:
    """v1's ConsolePlayer line, `[t] (rule) text`, with the kind and the id. t is
    the time ecu created the callout, in UTC."""
    c = callout.callout
    t = callout.created_at.strftime("%H:%M:%S.%f")[:-4]
    return f"[{t}] ({c.rule}) {callout.text}  kind={c.kind} id={c.id}"


class ConsoleSink:
    """A line on out for each callout. With voices, each is synthesised to know how
    long it takes, for the simulated speaker; without, that is estimated."""

    def __init__(self, out: IO[str], voices: Voices | None = None) -> None:
        self.out = out
        self.voices = voices

    async def connect(self) -> bool:
        return True

    async def prepare(self, callout: Pending) -> Clip | None:
        if not callout.text:
            return None
        if self.voices is None or callout.engineer is None:
            voice = callout.engineer.voice if callout.engineer is not None else VoiceSettings()
            seconds = estimate(callout.text, voice.length_scale, voice.sentence_pause_secs)
            return Clip(callout.text, seconds)
        u = await self.voices.get(callout.engineer).render_async(callout.text)
        return None if u is None else Clip(callout.text, u.duration, u)

    def seconds(self, clip: Clip) -> float:
        return clip.seconds

    async def play(self, clip: Clip, callout: Pending) -> None:
        print(line(callout), file=self.out, flush=True)

    def release(self, clip: Clip) -> None:
        if self.voices is not None:
            self.voices.release(clip.utterance)

    async def close(self) -> None:
        if self.voices is not None:
            self.voices.close()


class WavSink(ConsoleSink):
    """v1's WavDirPlayer: each callout said is written to `<dir>/<nnn>-<rule>.wav`,
    numbered in the order said, and its line printed. Synthesised off the event
    loop. The numbers carry on after the highest already in the directory, so a
    restarted radio never overwrites what it said before."""

    def __init__(self, out: IO[str], voices: Voices, directory: Path) -> None:
        super().__init__(out, voices)
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.written = max(
            (int(m[1]) for f in self.dir.iterdir() if (m := _NUMBERED.match(f.name))),
            default=0,
        )

    async def play(self, clip: Clip, callout: Pending) -> None:
        await super().play(clip, callout)
        if clip.utterance is None:
            return
        self.written += 1
        # The rule comes off the bus: only its safe characters go in a file name.
        rule = _UNSAFE.sub("_", callout.callout.rule) or "text"
        dst = self.dir / f"{self.written:03d}-{rule}.wav"
        await asyncio.to_thread(shutil.copyfile, clip.utterance.path, dst)
