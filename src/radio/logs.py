"""Logging as the Go components do it with log/slog: one line a record, with
`time`, `level` and `msg` first and the record's own key-value pairs after. A pair is
passed as `extra={...}`; `json` writes an object a line, `text` writes logfmt.

discord.py's own lines go the same way, from info up. A secret, such as the bot's
token, never reaches a line: wherever it turns up, in a message, a field or a
traceback, the line has `[redacted]` in its place (paddock#58 R8).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import IO, Any

# What every LogRecord carries, so the rest is what the caller passed in `extra`.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
# slog's names for the levels.
_LEVELS = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN"}
REDACTED = "[redacted]"
# The libraries whose lines radio writes beside its own: discord.py's debug lines
# hold every gateway event, so they start at info.
LIBRARIES = ("discord",)


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        "level": _LEVELS.get(record.levelno, "ERROR"),
        "msg": record.getMessage(),
    }
    fields.update((k, v) for k, v in record.__dict__.items() if k not in _STANDARD)
    if record.exc_info:
        fields["err"] = logging.Formatter().formatException(record.exc_info)
    return fields


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_fields(record), default=str)


class TextFormatter(logging.Formatter):
    """logfmt, as slog's TextHandler writes it: a value with a space, a quote or an
    `=` in it is quoted."""

    def format(self, record: logging.LogRecord) -> str:
        return " ".join(f"{k}={_value(v)}" for k, v in _fields(record).items())


def _value(v: object) -> str:
    s = str(v)
    if s == "" or any(c in s for c in ' "=\n\t'):
        return json.dumps(s)
    return s


class Redacting(logging.Formatter):
    """formatter's line with each secret replaced."""

    def __init__(self, formatter: logging.Formatter, secrets: Iterable[str]) -> None:
        super().__init__()
        self.formatter = formatter
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        line = self.formatter.format(record)
        for secret in self.secrets:
            line = line.replace(secret, REDACTED)
        return line


def setup(level: int, fmt: str, stream: IO[str], secrets: Iterable[str] = ()) -> logging.Logger:
    """The `radio` logger, writing to stream in fmt from level up, with discord.py's
    lines beside it and secrets kept out of both. Called again, it replaces what
    the last call set up."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Redacting(JSONFormatter() if fmt == "json" else TextFormatter(), secrets))
    for name in LIBRARIES:
        lib = logging.getLogger(name)
        lib.handlers[:] = [handler]
        lib.setLevel(max(level, logging.INFO))
        lib.propagate = False
    logger = logging.getLogger("radio")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    return logger
