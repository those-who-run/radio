"""Logging as the Go components do it with log/slog: one line a record, with
`time`, `level` and `msg` first and the record's own key-value pairs after. A pair is
passed as `extra={...}`; `json` writes an object a line, `text` writes logfmt.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import IO, Any

# What every LogRecord carries, so the rest is what the caller passed in `extra`.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
# slog's names for the levels.
_LEVELS = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN"}


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


def setup(level: int, fmt: str, stream: IO[str]) -> logging.Logger:
    """The `radio` logger, writing to stream in fmt from level up. Called again, it
    replaces what the last call set up."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter() if fmt == "json" else TextFormatter())
    logger = logging.getLogger("radio")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    return logger
