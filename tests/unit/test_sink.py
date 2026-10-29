"""The console and WAV sinks (paddock#60 R1), with a fake voice."""

import io
import logging
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import RATE, FakeVoice
from twr.events.v1 import callout_pb2
from twr_contracts.callout import Level

from radio.engineer import from_settings
from radio.piper_voice import Voices
from radio.queue import Pending
from radio.settings import Settings
from radio.sink import Clip, ConsoleSink, WavSink, line
from radio.speech import estimate
from radio.voice import VoiceSettings


class Delivery:
    async def ack(self) -> None:
        return None

    async def ack_sync(self, timeout: float = 1.0) -> object:
        return None


def pending(text: str, rule: str = "final_lap", settings: Settings | None = None) -> Pending:
    m = callout_pb2.Callout(id="c1", rule=rule, kind="template", priority=10)
    m.created_at.FromDatetime(datetime(2026, 10, 15, 12, 3, 4, 520000, tzinfo=UTC))
    p = Pending(m, Level.P1, 1, Delivery())
    p.text = text
    # A voice radio has no sums for, so the fake loads without the real model.
    p.engineer = from_settings(settings or Settings(voice_model=Path("fake.onnx")))
    return p


@pytest.fixture
def voices(tmp_path: Path) -> Voices:
    settings = Settings(voice_model=tmp_path / "fake.onnx", cache_dir=tmp_path / "cache")
    return Voices(settings, logging.getLogger("radio"), lambda model, voice: FakeVoice())


def test_the_line_is_v1s_with_the_kind_and_the_id() -> None:
    # v1's ConsolePlayer: `[t] (rule) text`; t is ecu's created_at, in UTC.
    assert (
        line(pending("Final lap.")) == "[12:03:04.52] (final_lap) Final lap.  kind=template id=c1"
    )


async def test_the_console_sink_prints_a_line_and_estimates_without_a_voice() -> None:
    out = io.StringIO()
    sink = ConsoleSink(out)
    assert await sink.connect()
    assert await sink.prepare(pending("")) is None
    p = pending("Lap two. one oh four point six.")
    clip = await sink.prepare(p)
    assert clip == Clip(p.text, estimate(p.text, 0.9, 0.35))
    assert sink.seconds(clip) == clip.seconds
    await sink.play(clip, p)
    sink.release(clip)
    await sink.close()
    assert out.getvalue() == line(p) + "\n"


async def test_the_engineers_voice_settings_scale_the_estimate() -> None:
    slow = Settings(voice=VoiceSettings(length_scale=1.8, sentence_pause_secs=0))
    clip = await ConsoleSink(io.StringIO()).prepare(pending("Final lap.", settings=slow))
    assert clip is not None
    assert clip.seconds == pytest.approx(2 * estimate("Final lap.", 0.9, 0))


async def test_the_console_sink_measures_with_a_voice(voices: Voices) -> None:
    # R3: with the voice, the duration is the synthesiser's.
    sink = ConsoleSink(io.StringIO(), voices)
    clip = await sink.prepare(pending("Final lap."))
    assert clip is not None and clip.utterance is not None
    assert clip.seconds == len("Final lap.") / RATE
    sink.release(clip)
    await sink.close()


async def test_the_wav_sink_writes_numbered_files(voices: Voices, tmp_path: Path) -> None:
    # R1, AC2: one file a callout said, <nnn>-<rule>.wav, numbered in order, and
    # the line printed too.
    out = io.StringIO()
    sink = WavSink(out, voices, tmp_path / "wav")
    said = [("Final lap.", "final_lap"), ("Pit this lap. Box box.", "fuel"), ("Hi.", "../x")]
    for text, rule in said:
        p = pending(text, rule)
        clip = await sink.prepare(p)
        assert clip is not None
        await sink.play(clip, p)
        sink.release(clip)
    await sink.close()
    files = sorted(p.name for p in (tmp_path / "wav").iterdir())
    # A rule off the bus never leaves the directory.
    assert files == ["001-final_lap.wav", "002-fuel.wav", "003-___x.wav"]
    with wave.open(str(tmp_path / "wav" / "001-final_lap.wav"), "rb") as w:
        assert w.getnframes() == len("Final lap.")
    assert len(out.getvalue().splitlines()) == 3


async def test_the_wav_sink_writes_nothing_for_no_audio(voices: Voices, tmp_path: Path) -> None:
    sink = WavSink(io.StringIO(), voices, tmp_path / "wav")
    p = pending("...")
    assert await sink.prepare(p) is None
    await sink.play(Clip("...", 0.0), p)
    assert list((tmp_path / "wav").iterdir()) == []
    await sink.close()


async def test_a_restarted_wav_sink_carries_on_numbering(voices: Voices, tmp_path: Path) -> None:
    # A restart never overwrites what was said before it.
    directory = tmp_path / "wav"
    directory.mkdir()
    (directory / "007-fuel.wav").write_bytes(b"")
    (directory / "notes.txt").write_bytes(b"")
    sink = WavSink(io.StringIO(), voices, directory)
    p = pending("Final lap.")
    clip = await sink.prepare(p)
    assert clip is not None
    await sink.play(clip, p)
    await sink.close()
    assert (directory / "008-final_lap.wav").exists()
