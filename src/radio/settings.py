"""radio's settings, read once at start from the environment.

Every setting is named `TWR_*` (paddock#55, decided 2026-10-28). The shared ones
have the same name in every component; radio's own carry its name, `TWR_RADIO_*`,
and there are none yet. An unset or empty variable takes its default; a value that
can't be read is an error, so a typo stops radio rather than being ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_HEALTH_ADDR = ":8080"

# slog's level names, as the Go components read TWR_LOG_LEVEL.
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
FORMATS = ("text", "json")


class SettingsError(ValueError):
    """A variable holds a value radio can't read."""


@dataclass(frozen=True)
class Settings:
    # The NATS server, or a comma-separated list of them: TWR_NATS_URL.
    nats_url: str = DEFAULT_NATS_URL
    # Where GET /healthz listens, host:port, an empty host for every interface:
    # TWR_HEALTH_ADDR.
    health_host: str = ""
    health_port: int = 8080
    # The lowest level logged: TWR_LOG_LEVEL, debug, info, warn or error.
    log_level: int = logging.INFO
    # How each line is written: TWR_LOG_FORMAT, text (logfmt) or json, one object a
    # line. The image sets json.
    log_format: str = "text"


def parse_addr(addr: str) -> tuple[str, int]:
    """host:port, as Go's net.Listen takes it: `:8080`, `127.0.0.1:0`, `[::1]:8080`."""
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit() or int(port) > 65535:
        raise SettingsError(f"TWR_HEALTH_ADDR: {addr!r} is not host:port")
    return host.removeprefix("[").removesuffix("]"), int(port)


def from_env(environ: Mapping[str, str]) -> Settings:
    """The settings from environ, which is os.environ outside the tests."""

    def get(name: str) -> str:
        return environ.get(name, "").strip()

    nats_url = get("TWR_NATS_URL") or DEFAULT_NATS_URL
    host, port = parse_addr(get("TWR_HEALTH_ADDR") or DEFAULT_HEALTH_ADDR)
    level_name = get("TWR_LOG_LEVEL").lower() or "info"
    if level_name not in LEVELS:
        raise SettingsError(f"TWR_LOG_LEVEL: {level_name!r} is not one of {', '.join(LEVELS)}")
    log_format = get("TWR_LOG_FORMAT").lower() or "text"
    if log_format not in FORMATS:
        raise SettingsError(f"TWR_LOG_FORMAT: {log_format!r} is not one of {', '.join(FORMATS)}")
    return Settings(
        nats_url=nats_url,
        health_host=host,
        health_port=port,
        log_level=LEVELS[level_name],
        log_format=log_format,
    )
