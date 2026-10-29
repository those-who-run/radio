import logging
from pathlib import Path

import pytest
from helpers import DISCORD, TOKEN

from radio.phrasing import templates
from radio.settings import Settings, SettingsError, default_cache_dir, from_env, parse_addr
from radio.voice import VoiceSettings


def test_defaults() -> None:
    assert from_env({}) == Settings()
    assert from_env({"TWR_LOG_LEVEL": " ", "TWR_NATS_URL": ""}) == Settings()


def test_every_setting() -> None:
    s = from_env(
        {
            "TWR_NATS_URL": "nats://nats:4222",
            "TWR_HEALTH_ADDR": "127.0.0.1:9850",
            "TWR_LOG_LEVEL": "WARN",
            "TWR_LOG_FORMAT": "json",
        }
    )
    assert s == Settings("nats://nats:4222", "127.0.0.1", 9850, logging.WARNING, "json")


def test_radios_own(tmp_path: Path) -> None:
    rules = tmp_path / "voice.yaml"
    rules.write_text("voice:\n  length_scale: 1.1\n  sentence_pause_secs: 0.5\n")
    s = from_env(
        {
            "TWR_RADIO_VOICE_MODEL": "/voices/x.onnx",
            "TWR_RADIO_VOICE_FILE": str(rules),
            "TWR_RADIO_CACHE_DIR": "/cache",
            "TWR_RADIO_CACHE_MAX_MB": "5",
        }
    )
    assert s.voice_model == Path("/voices/x.onnx")
    assert s.voice_file == rules
    assert s.voice == VoiceSettings(length_scale=1.1, sentence_pause_secs=0.5)
    assert s.voice.noise_scale == 0.5, "a key left out takes v1's default"
    assert s.cache_dir == Path("/cache")
    assert s.cache_max_mb == 5
    assert s.src == "live"


def test_the_sink_and_the_timing() -> None:
    s = from_env(
        {
            "TWR_RADIO_SINK": "WAV",
            "TWR_RADIO_WAV_DIR": "/out/wav",
            "TWR_RADIO_TIMING": "simulated",
            "TWR_RADIO_TIMELINE_FILE": "/out/timeline.jsonl",
        }
    )
    assert (s.sink, s.wav_dir, s.timing, s.timeline_file) == (
        "wav",
        Path("/out/wav"),
        "simulated",
        Path("/out/timeline.jsonl"),
    )
    # The console, live, with no timeline, by default.
    d = from_env({})
    assert (d.sink, d.wav_dir, d.timing, d.timeline_file) == ("console", None, "live", None)


def test_the_source() -> None:
    assert from_env({"TWR_RADIO_SRC": "replay-sugo-1"}).src == "replay-sugo-1"


def test_the_voice_file_sets_the_phrases(tmp_path: Path) -> None:
    # paddock#90 AC5 at start: the rules file's `script:` and template keys.
    rules = tmp_path / "rules.yaml"
    rules.write_text("script: false\nrules:\n  final_lap:\n    template: Last one.\n")
    s = from_env({"TWR_RADIO_VOICE_FILE": str(rules)})
    assert s.templates["final_lap"]["template"] == "Last one."
    assert s.templates["fuel"]["template_pit"] == "Pit this lap.", "the built-in phrase"
    assert from_env({}).templates == templates()


def test_a_bad_template_stops_radio(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n  position:\n    template_lost: Down to {lap}.\n")
    with pytest.raises(SettingsError, match="TWR_RADIO_VOICE_FILE.*template_lost"):
        from_env({"TWR_RADIO_VOICE_FILE": str(rules)})


def test_the_cache_goes_in_the_users_cache_directory() -> None:
    assert default_cache_dir({"XDG_CACHE_HOME": "/x", "HOME": "/h"}) == Path("/x/twr-radio")
    assert default_cache_dir({"HOME": "/h"}) == Path("/h/.cache/twr-radio")
    assert default_cache_dir({}) == Settings().cache_dir
    assert from_env({"HOME": "/h"}).cache_dir == Path("/h/.cache/twr-radio")


def test_the_unprefixed_names_are_not_read() -> None:
    assert (
        from_env({"NATS_URL": "nats://x:1", "LOG_LEVEL": "debug", "LOG_FORMAT": "json"})
        == Settings()
    )


@pytest.mark.parametrize(
    ("addr", "want"),
    [(":8080", ("", 8080)), ("127.0.0.1:0", ("127.0.0.1", 0)), ("[::1]:80", ("::1", 80))],
)
def test_addresses(addr: str, want: tuple[str, int]) -> None:
    assert parse_addr(addr) == want


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({"TWR_LOG_LEVEL": "verbose"}, "TWR_LOG_LEVEL"),
        ({"TWR_LOG_FORMAT": "yaml"}, "TWR_LOG_FORMAT"),
        ({"TWR_HEALTH_ADDR": "8080"}, "TWR_HEALTH_ADDR"),
        ({"TWR_HEALTH_ADDR": ":http"}, "TWR_HEALTH_ADDR"),
        ({"TWR_HEALTH_ADDR": ":70000"}, "TWR_HEALTH_ADDR"),
        ({"TWR_RADIO_CACHE_MAX_MB": "lots"}, "TWR_RADIO_CACHE_MAX_MB"),
        ({"TWR_RADIO_CACHE_MAX_MB": "-1"}, "TWR_RADIO_CACHE_MAX_MB"),
        ({"TWR_RADIO_CACHE_MAX_MB": "²"}, "TWR_RADIO_CACHE_MAX_MB"),
        ({"TWR_RADIO_VOICE_FILE": "/no/such/voice.yaml"}, "TWR_RADIO_VOICE_FILE"),
        ({"TWR_RADIO_SRC": "replay.sugo"}, "TWR_RADIO_SRC"),
        ({"TWR_RADIO_SRC": "a/b"}, "TWR_RADIO_SRC"),
        ({"TWR_RADIO_SRC": "*"}, "TWR_RADIO_SRC"),
        ({"TWR_RADIO_SINK": "speaker"}, "TWR_RADIO_SINK"),
        # R9: the discord sink without the bot's settings.
        ({"TWR_RADIO_SINK": "discord"}, "TWR_DISCORD_BOT_TOKEN: the discord sink needs"),
        (
            {"TWR_RADIO_SINK": "discord", "TWR_DISCORD_BOT_TOKEN": TOKEN},
            "TWR_DISCORD_VOICE_CHANNEL_ID: the discord sink needs",
        ),
        ({"TWR_DISCORD_VOICE_CHANNEL_ID": "general"}, "TWR_DISCORD_VOICE_CHANNEL_ID"),
        ({"TWR_DISCORD_VOICE_CHANNEL_ID": "0"}, "TWR_DISCORD_VOICE_CHANNEL_ID"),
        ({**DISCORD, "TWR_RADIO_TIMING": "instant"}, "TWR_RADIO_TIMING: 'instant': the discord"),
        ({"TWR_RADIO_IDLE_DISCONNECT_SECS": "soon"}, "TWR_RADIO_IDLE_DISCONNECT_SECS"),
        ({"TWR_RADIO_IDLE_DISCONNECT_SECS": "0"}, "TWR_RADIO_IDLE_DISCONNECT_SECS"),
        ({"TWR_RADIO_RECONNECT_BACKOFF_SECS": "inf"}, "TWR_RADIO_RECONNECT_BACKOFF_SECS"),
        ({"TWR_RADIO_RECONNECT_BACKOFF_SECS": "nan"}, "TWR_RADIO_RECONNECT_BACKOFF_SECS"),
        ({"TWR_RADIO_SINK": "wav"}, "TWR_RADIO_WAV_DIR"),
        ({"TWR_RADIO_TIMING": "fast"}, "TWR_RADIO_TIMING"),
    ],
)
def test_a_value_radio_cant_read_stops_it(env: dict[str, str], names: str) -> None:
    with pytest.raises(SettingsError, match=names) as err:
        from_env(env)
    assert TOKEN not in str(err.value)


def test_the_discord_settings() -> None:
    s = from_env(
        {
            **DISCORD,
            "TWR_RADIO_IDLE_DISCONNECT_SECS": "90",
            "TWR_RADIO_RECONNECT_BACKOFF_SECS": "2.5",
        }
    )
    assert s.sink == "discord"
    assert s.discord_token is not None and s.discord_token.reveal() == TOKEN
    assert s.discord_channel_id == 1234567890
    assert (s.idle_disconnect_secs, s.reconnect_backoff_secs) == (90.0, 2.5)
    assert s.secrets() == (TOKEN,)
    # R8: printing the settings never prints the token.
    assert TOKEN not in repr(s) and TOKEN not in str(s.discord_token)
    defaults = from_env({})
    assert (defaults.idle_disconnect_secs, defaults.reconnect_backoff_secs) == (600.0, 15.0)
    assert defaults.discord_token is None and defaults.secrets() == ()
