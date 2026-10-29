"""radio's settings, read once at start from the environment.

Every setting is named `TWR_*` (paddock#55, decided 2026-10-28). The shared ones
have the same name in every component; radio's own carry its name, `TWR_RADIO_*`.
An unset or empty variable takes its default; a value that
can't be read is an error, so a typo stops radio rather than being ignored.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from radio.phrasing import TemplateError, Templates, load_rules_file, templates
from radio.voice import DEFAULT_VOICE, VoiceError, VoiceSettings
from radio.voice import load as load_voice

DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_HEALTH_ADDR = ":8080"
# Where `make voice` puts v1's voice, from the repository's root.
DEFAULT_VOICE_MODEL = Path("voices") / f"{DEFAULT_VOICE}.onnx"
# The speech cache's directory under $XDG_CACHE_HOME, or ~/.cache without it.
CACHE_NAME = "twr-radio"
DEFAULT_CACHE_MAX_MB = 100
DEFAULT_SRC = "live"
# A source is one subject token and part of the consumers' names (`radio-<src>-p0`):
# live, parity or replay-<name>.
SRC = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)

# slog's level names, as the Go components read TWR_LOG_LEVEL.
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
FORMATS = ("text", "json")
# Where radio speaks (TWR_RADIO_SINK).
SINKS = ("console", "wav", "discord")
# v1's GT7_IDLE_DISCONNECT_SECS and reconnect backoff (paddock#58).
DEFAULT_IDLE_DISCONNECT_SECS = 600.0
DEFAULT_RECONNECT_BACKOFF_SECS = 15.0
# How long each callout takes (TWR_RADIO_TIMING): live on the wall clock, or a
# replay's own clock, instant or with a simulated speaker (radio.timing).
TIMINGS = ("live", "instant", "simulated")


class SettingsError(ValueError):
    """A variable holds a value radio can't read."""


class Secret:
    """A value that must never be written out, such as the bot's token (paddock#58
    R8): its repr and str hide it, so a log line or an error that prints the
    settings doesn't print it. `reveal` gives it to the one place that needs it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('[redacted]')"

    __str__ = __repr__


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
    # The source whose callouts radio speaks, twr.v1.<src>.callout.>: TWR_RADIO_SRC,
    # live by default, replay-<run> for a replay.
    src: str = DEFAULT_SRC
    # The phrases, by rule and key: v1's script over the built-in phrases, with
    # voice_file's `script:` and template keys over them (radio.phrasing).
    templates: Templates = field(default_factory=templates)
    # Where radio speaks: TWR_RADIO_SINK, console (a line a callout on standard
    # output) or wav (a numbered WAV file a callout in wav_dir, and the line).
    sink: str = "console"
    # TWR_RADIO_WAV_DIR, which the wav sink needs.
    wav_dir: Path | None = None
    # TWR_RADIO_TIMING: live, instant or simulated.
    timing: str = "live"
    # Where each callout's timeline goes, a JSON line each: TWR_RADIO_TIMELINE_FILE.
    # Unset, nowhere.
    timeline_file: Path | None = None
    # The token of the bot the discord sink speaks as: TWR_DISCORD_BOT_TOKEN, from
    # a Kubernetes secret. Which bot it is, is the deployment's choice.
    discord_token: Secret | None = None
    # The voice channel it speaks in: TWR_DISCORD_VOICE_CHANNEL_ID.
    discord_channel_id: int | None = None
    # How long voice stays joined with nothing on: TWR_RADIO_IDLE_DISCONNECT_SECS.
    idle_disconnect_secs: float = DEFAULT_IDLE_DISCONNECT_SECS
    # The least time between two joins: TWR_RADIO_RECONNECT_BACKOFF_SECS.
    reconnect_backoff_secs: float = DEFAULT_RECONNECT_BACKOFF_SECS

    def secrets(self) -> tuple[str, ...]:
        """The values no log line may hold."""
        return (self.discord_token.reveal(),) if self.discord_token is not None else ()


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
    if not (max_mb.isascii() and max_mb.isdigit()):
        raise SettingsError(f"TWR_RADIO_CACHE_MAX_MB: {max_mb!r} is not a whole number of MiB")
    src = get("TWR_RADIO_SRC") or DEFAULT_SRC
    if not SRC.fullmatch(src):
        raise SettingsError(
            f"TWR_RADIO_SRC: {src!r} is not a source: letters, digits, - and _ only"
        )
    try:
        table = load_rules_file(voice_file) if voice_file else templates()
    except TemplateError as err:
        raise SettingsError(f"TWR_RADIO_VOICE_FILE: {err}") from err
    sink = get("TWR_RADIO_SINK").lower() or "console"
    if sink not in SINKS:
        raise SettingsError(f"TWR_RADIO_SINK: {sink!r} is not one of {', '.join(SINKS)}")
    wav_dir = Path(get("TWR_RADIO_WAV_DIR")) if get("TWR_RADIO_WAV_DIR") else None
    if sink == "wav" and wav_dir is None:
        raise SettingsError("TWR_RADIO_WAV_DIR: the wav sink needs a directory")
    timing = get("TWR_RADIO_TIMING").lower() or "live"
    if timing not in TIMINGS:
        raise SettingsError(f"TWR_RADIO_TIMING: {timing!r} is not one of {', '.join(TIMINGS)}")
    timeline = get("TWR_RADIO_TIMELINE_FILE")
    # Never echoed in an error: it is a secret.
    token = get("TWR_DISCORD_BOT_TOKEN")
    channel = get("TWR_DISCORD_VOICE_CHANNEL_ID")
    if channel and not (channel.isascii() and channel.isdigit() and int(channel) > 0):
        raise SettingsError(f"TWR_DISCORD_VOICE_CHANNEL_ID: {channel!r} is not a channel id")
    if sink == "discord":
        if not token:
            raise SettingsError("TWR_DISCORD_BOT_TOKEN: the discord sink needs the bot's token")
        if not channel:
            raise SettingsError(
                "TWR_DISCORD_VOICE_CHANNEL_ID: the discord sink needs a voice channel"
            )
        if timing != "live":
            raise SettingsError(
                f"TWR_RADIO_TIMING: {timing!r}: the discord sink speaks on the wall clock, live"
            )
    idle = seconds(environ, "TWR_RADIO_IDLE_DISCONNECT_SECS", DEFAULT_IDLE_DISCONNECT_SECS)
    backoff = seconds(environ, "TWR_RADIO_RECONNECT_BACKOFF_SECS", DEFAULT_RECONNECT_BACKOFF_SECS)
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
        src=src,
        templates=table,
        sink=sink,
        wav_dir=wav_dir,
        timing=timing,
        timeline_file=Path(timeline) if timeline else None,
        discord_token=Secret(token) if token else None,
        discord_channel_id=int(channel) if channel else None,
        idle_disconnect_secs=idle,
        reconnect_backoff_secs=backoff,
    )


def seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    """A positive number of seconds, default when unset."""
    value = environ.get(name, "").strip()
    if not value:
        return default
    try:
        secs = float(value)
    except ValueError:
        secs = 0.0
    if not 0 < secs < float("inf"):
        raise SettingsError(f"{name}: {value!r} is not a positive number of seconds")
    return secs


def default_cache_dir(environ: Mapping[str, str]) -> Path:
    """twr-radio under $XDG_CACHE_HOME, or under ~/.cache without it."""
    if xdg := environ.get("XDG_CACHE_HOME", "").strip():
        return Path(xdg) / CACHE_NAME
    if home := environ.get("HOME", "").strip():
        return Path(home) / ".cache" / CACHE_NAME
    return Path(".cache") / CACHE_NAME
