"""Voices with a fake loader: the model check, the settings and the cache. The
real voice is `make voice-test`'s (tests/voice/)."""

import io
import logging
from pathlib import Path

import pytest
from helpers import FakeVoice, lines

from radio.engineer import from_settings
from radio.piper_voice import Voices
from radio.settings import Settings
from radio.speech import Voice
from radio.voice import VoiceError, VoiceSettings


def test_a_voice_without_sums_loads_with_a_warning(tmp_path: Path, log: io.StringIO) -> None:
    loaded: list[tuple[Path, VoiceSettings]] = []

    def load(model: Path, settings: VoiceSettings) -> Voice:
        loaded.append((model, settings))
        return FakeVoice()

    settings = Settings(
        voice_model=tmp_path / "mine.onnx",
        voice=VoiceSettings(sentence_pause_secs=0.5),
        cache_dir=tmp_path / "cache",
        cache_max_mb=2,
    )
    voices = Voices(settings, logging.getLogger("radio"), load)
    engineer = from_settings(settings)
    try:
        s = voices.get(engineer)
        # Loaded once, the first time the engineer speaks.
        assert voices.get(engineer) is s
        assert loaded == [(tmp_path / "mine.onnx", VoiceSettings(sentence_pause_secs=0.5))]
        assert s.pause_secs == 0.5
        assert s.cache.dir == tmp_path / "cache" and s.cache.max_bytes == 2 * 1024 * 1024
        u = s.render("Lap two. Final lap.")
        assert u is not None and u.path.exists()
        voices.release(u)
        assert not u.path.exists()
    finally:
        voices.close()
    assert [r["msg"] for r in lines(log)][:2] == ["voice model not checked", "loading voice"]
    assert lines(log)[1]["engineer"] == "v1"


def test_a_known_voice_that_fails_its_sum_is_never_loaded(tmp_path: Path) -> None:
    model = tmp_path / "en_GB-alba-medium.onnx"
    model.write_bytes(b"not alba")
    Path(f"{model}.json").write_bytes(b"{}")

    def load(model: Path, settings: VoiceSettings) -> Voice:
        raise AssertionError("loaded")

    settings = Settings(voice_model=model, cache_dir=tmp_path / "cache")
    with pytest.raises(VoiceError):
        Voices(settings, logging.getLogger("radio"), load).get(from_settings(settings))
