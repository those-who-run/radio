import logging

import pytest

from radio.settings import Settings, SettingsError, from_env, parse_addr


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
    ],
)
def test_a_value_radio_cant_read_stops_it(env: dict[str, str], names: str) -> None:
    with pytest.raises(SettingsError, match=names):
        from_env(env)
