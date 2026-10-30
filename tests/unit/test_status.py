"""radio's status in the `live` bucket (paddock#62, R9), against a fake bucket: the
service and discord sink tests write it to the real nats-server."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from helpers import lines, until
from twr.state.v1 import live_pb2
from twr_contracts import ce

from radio import status
from radio.status import Status, Writer

SPOKEN = datetime(2026, 10, 29, 13, 52, 30, 875_000, UTC)
NOW = datetime(2026, 10, 29, 13, 52, 31, tzinfo=UTC)


class FakeKV:
    def __init__(self) -> None:
        self.puts: list[tuple[str, live_pb2.RadioStatus]] = []
        self.fail = 0

    async def put(self, key: str, value: bytes) -> int:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("nats: connection closed")
        m = live_pb2.RadioStatus()
        ce.unmarshal(value, m)
        self.puts.append((key, m))
        return len(self.puts)


class FakeJS:
    def __init__(self, kv: FakeKV, missing: int = 0) -> None:
        self.kv = kv
        self.missing = missing
        self.buckets: list[str] = []

    async def key_value(self, bucket: str) -> FakeKV:
        self.buckets.append(bucket)
        if self.missing:
            self.missing -= 1
            raise LookupError("bucket not found")
        return self.kv


class FakeNC:
    def __init__(self, js: FakeJS) -> None:
        self.js = js

    def jetstream(self) -> FakeJS:
        return self.js


class State:
    def __init__(self) -> None:
        self.connected: bool | None = True
        self.listeners: int | None = None
        self.spoken: datetime | None = None


def made(state: State, src: str = "live", sink: str = "discord") -> Status:
    return Status(src, sink, lambda: state.connected, lambda: state.listeners, lambda: state.spoken)


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "CHECK", 0.01)
    monkeypatch.setattr(status, "MIN_GAP", 0.3)
    monkeypatch.setattr(status, "HEARTBEAT", 1.0)
    monkeypatch.setattr(status, "RETRY_WAIT", 0.05)


def test_the_value_and_the_health_body() -> None:
    state = State()
    s = made(state)
    assert s.value() == live_pb2.RadioStatus(
        source="live", sink=live_pb2.RADIO_SINK_DISCORD, voice_connected=True
    )
    assert s.body() == {
        "src": "live",
        "sink": "discord",
        "voice_connected": True,
        "voice_listeners": None,
    }
    state.listeners, state.spoken, state.connected = 0, SPOKEN, False
    v = s.value()
    # False and 0 are values, set as such (contracts' optional fields).
    assert v.HasField("voice_connected") and not v.voice_connected
    assert v.HasField("voice_listeners") and v.voice_listeners == 0
    assert v.last_spoken_at.ToDatetime(tzinfo=UTC) == SPOKEN
    assert s.body()["last_spoken_at"] == "2026-10-29T13:52:30.875+00:00"


def test_without_discord_the_voice_is_absent() -> None:
    state = State()
    state.connected = None
    s = made(state, "replay-suzuka", "console")
    assert s.value() == live_pb2.RadioStatus(
        source="replay-suzuka", sink=live_pb2.RADIO_SINK_CONSOLE
    )
    assert s.body() == {"src": "replay-suzuka", "sink": "console"}


async def test_written_on_change_at_most_once_a_gap_under_the_sources_key() -> None:
    state = State()
    kv = FakeKV()
    js = FakeJS(kv)
    w = Writer(made(state, "replay-x"), logging.getLogger("radio"), clock=lambda: NOW)
    task = asyncio.create_task(w.run(FakeNC(js)))  # type: ignore[arg-type]
    try:
        await until(lambda: len(kv.puts) == 1)
        # A replay writes its own key, never the live source's.
        key, first = kv.puts[0]
        assert (key, js.buckets) == ("radio.status.replay-x", ["live"])
        assert first.updated_at.ToDatetime(tzinfo=UTC) == NOW
        # Three changes inside the gap are one write, of the last.
        state.connected = False
        await asyncio.sleep(0.02)
        state.connected = True
        await asyncio.sleep(0.02)
        state.spoken = SPOKEN
        await until(lambda: len(kv.puts) == 2)
        assert kv.puts[1][1].voice_connected and kv.puts[1][1].HasField("last_spoken_at")
        # Nothing changed: nothing written until the heartbeat, which writes the
        # same value again.
        await asyncio.sleep(0.5)
        assert len(kv.puts) == 2
        await until(lambda: len(kv.puts) == 3)
        assert kv.puts[2] == kv.puts[1]
    finally:
        task.cancel()


async def test_a_failed_write_is_logged_once_and_tried_again(log: io.StringIO) -> None:
    state = State()
    kv = FakeKV()
    kv.fail = 3
    js = FakeJS(kv, missing=2)
    w = Writer(made(state), logging.getLogger("radio"), clock=lambda: NOW)
    task = asyncio.create_task(w.run(FakeNC(js)))  # type: ignore[arg-type]
    try:
        await until(lambda: len(kv.puts) == 1)
    finally:
        task.cancel()
    msgs: list[Any] = [x["msg"] for x in lines(log)]
    assert msgs.count("status: write") == 1
    assert msgs.count("status: written again") == 1
    assert len(js.buckets) == 3
    assert json.loads(ce.marshal(kv.puts[0][1]))["source"] == "live"
