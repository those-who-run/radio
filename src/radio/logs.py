"""Logging as the Go components do it with log/slog: one line a record, with
`time`, `level` and `msg` first and the record's own key-value pairs after. A pair is
passed as `extra={...}`; `json` writes an object a line, `text` writes logfmt.

Every library's lines (discord.py, asyncio, aiohttp, nats-py) go the same way,
through the root logger, from info up. A secret, such as the bot's
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
# The name radio's handler goes by, so a second setup replaces it.
_HANDLER = "radio"
# The libraries' lines, through the root logger, start at info: discord.py's debug
# lines hold every gateway event.
LIBRARY_LEVEL = logging.INFO


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
    """The `radio` logger, writing to stream in fmt from level up, with every
    library's lines beside it through the root logger, and secrets kept out of
    all of them. Called again, it replaces what the last call set up; a root
    handler radio didn't install, such as pytest's, is left alone."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Redacting(JSONFormatter() if fmt == "json" else TextFormatter(), secrets))
    handler.set_name(_HANDLER)
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if h.get_name() != _HANDLER] + [handler]
    root.setLevel(max(level, LIBRARY_LEVEL))
    logger = logging.getLogger("radio")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    return logger
