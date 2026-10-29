import logging
from pathlib import Path

import pytest

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
    rules = tmp_path / "rules.yaml"
    rules.write_text("voice:\n  length_scale: 1.1\n  sentence_pause_secs: 0.5\n")
    s = from_env(
        {
            "TWR_RADIO_VOICE_MODEL": "/voices/x.onnx",
            "TWR_RADIO_RULES_FILE": str(rules),
            "TWR_RADIO_CACHE_DIR": "/cache",
            "TWR_RADIO_CACHE_MAX_MB": "5",
        }
    )
    assert s.voice_model == Path("/voices/x.onnx")
    assert s.rules_file == rules
    assert s.voice == VoiceSettings(length_scale=1.1, sentence_pause_secs=0.5)
    assert s.cache_dir == Path("/cache")
    assert s.cache_max_mb == 5


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
        ({"TWR_RADIO_RULES_FILE": "/no/such/rules.yaml"}, "TWR_RADIO_RULES_FILE"),
    ],
)
def test_a_value_radio_cant_read_stops_it(env: dict[str, str], names: str) -> None:
    with pytest.raises(SettingsError, match=names):
        from_env(env)
