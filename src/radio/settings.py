"""radio's settings, read once at start from the environment.

Every setting is named `TWR_*` (paddock#55, decided 2026-10-28). The shared ones
have the same name in every component; radio's own carry its name, `TWR_RADIO_*`.
An unset or empty variable takes its default; a value that
can't be read is an error, so a typo stops radio rather than being ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from radio.voice import DEFAULT_VOICE, VoiceError, VoiceSettings
from radio.voice import load as load_voice

DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_HEALTH_ADDR = ":8080"
# Where `make voice` puts v1's voice, from the repository's root.
DEFAULT_VOICE_MODEL = Path("voices") / f"{DEFAULT_VOICE}.onnx"
# The speech cache's directory under $XDG_CACHE_HOME, or ~/.cache without it.
CACHE_NAME = "twr-radio"
DEFAULT_CACHE_MAX_MB = 100

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
    # The Piper voice, an .onnx file with its .onnx.json beside it:
    # TWR_RADIO_VOICE_MODEL. v1's en_GB-alba-medium by default.
    voice_model: Path = DEFAULT_VOICE_MODEL
    # The YAML file whose `voice:` section tunes the voice, in v1's rules file
    # format: TWR_RADIO_VOICE_FILE. Unset, v1's defaults.
    voice_file: Path | None = None
    # The voice settings read from voice_file.
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    # The sentence cache: TWR_RADIO_CACHE_DIR, by default twr-radio in the user's
    # cache directory, never a shared data volume.
    cache_dir: Path = Path(".cache") / CACHE_NAME
    # Its cap in MiB: TWR_RADIO_CACHE_MAX_MB.
    cache_max_mb: int = DEFAULT_CACHE_MAX_MB


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
    voice_file = Path(get("TWR_RADIO_VOICE_FILE")) if get("TWR_RADIO_VOICE_FILE") else None
    try:
        voice_settings = load_voice(voice_file) if voice_file else VoiceSettings()
    except VoiceError as err:
        raise SettingsError(f"TWR_RADIO_VOICE_FILE: {err}") from err
    max_mb = get("TWR_RADIO_CACHE_MAX_MB") or str(DEFAULT_CACHE_MAX_MB)
    if not max_mb.isdigit():
        raise SettingsError(f"TWR_RADIO_CACHE_MAX_MB: {max_mb!r} is not a whole number of MiB")
    return Settings(
        nats_url=nats_url,
        health_host=host,
        health_port=port,
        log_level=LEVELS[level_name],
        log_format=log_format,
        voice_model=Path(get("TWR_RADIO_VOICE_MODEL") or DEFAULT_VOICE_MODEL),
        voice_file=voice_file,
        voice=voice_settings,
        cache_dir=Path(get("TWR_RADIO_CACHE_DIR") or default_cache_dir(environ)),
        cache_max_mb=int(max_mb),
    )


def default_cache_dir(environ: Mapping[str, str]) -> Path:
    """twr-radio under $XDG_CACHE_HOME, or under ~/.cache without it."""
    if xdg := environ.get("XDG_CACHE_HOME", "").strip():
        return Path(xdg) / CACHE_NAME
    if home := environ.get("HOME", "").strip():
        return Path(home) / ".cache" / CACHE_NAME
    return Path(".cache") / CACHE_NAME
