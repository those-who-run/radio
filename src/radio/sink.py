"""Where radio speaks a callout (paddock#56; the sinks are paddock#60, Discord #58).

radio speaks one callout at a time and calls a sink's steps in v1's order:
`connect`, then `prepare` (the synthesis), then `play`. Between them it drops the
callout if its `expires_at` has passed, at the stage it was in (v1's
DiscordPlayer._stale), so a sink holds no staleness rules of its own.
"""

from __future__ import annotations

import logging
from typing import Protocol

from radio.queue import Pending


class Sink[C](Protocol):
    """A sink's steps. C is what `prepare` makes for `play`, such as a WAV."""

    async def connect(self) -> bool:
        """Wait until the sink can speak: True once it can, False if it can't. It is
        cancelled when the callout goes stale meanwhile, so a sink whose join must
        outlive one callout shields it itself."""
        ...

    async def prepare(self, text: str) -> C | None:
        """What play needs to say text; None for nothing to say."""
        ...

    async def play(self, clip: C, callout: Pending) -> None:
        """Say it, returning once it has been said."""
        ...

    def release(self, clip: C) -> None:
        """Let go of what prepare made, played or dropped."""
        ...


class LogSink:
    """Logs each callout it would say, with no audio: radio's sink until paddock#60
    brings the console and Discord ones."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    async def connect(self) -> bool:
        return True

    async def prepare(self, text: str) -> str | None:
        return text or None

    async def play(self, clip: str, callout: Pending) -> None:
        c = callout.callout
        self.logger.info(
            "speaking", extra={"id": c.id, "rule": c.rule, "kind": c.kind, "text": clip}
        )

    def release(self, clip: str) -> None:
        return None
