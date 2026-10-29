"""The synthesiser with the real Piper voice, TWR_RADIO_VOICE_MODEL: opt in with
`make voice-test` (after `make voice`). It is outside tests/unit/, so `make check` and
CI, which have no model, never collect it; a missing model fails it, never skips it."""

import logging
import os
from pathlib import Path

import pytest

from radio.piper_voice import open_synthesizer
from radio.settings import Settings
from radio.speech import WARM_UP, duration
from radio.voice import VoiceSettings


def test_alba_says_ac2_as_v1_would(tmp_path: Path) -> None:
    model = Path(os.environ["TWR_RADIO_VOICE_MODEL"])
    settings = Settings(voice_model=model, voice=VoiceSettings(), cache_dir=tmp_path)
    s = open_synthesizer(settings, logging.getLogger("radio"))
    try:
        assert s.render(WARM_UP) is not None
        u = s.render("Lap two. One oh four point six.")
        assert u is not None
        first, second = (
            s.cache.get(f"{s.voice.key}|{t}") for t in ("Lap two.", "One oh four point six.")
        )
        assert first is not None and second is not None
        assert u.duration == pytest.approx(duration(first) + 0.35 + duration(second), abs=0.001)
        made = s.render("Lap three. One oh four point six.")
        assert made is not None and 0.5 < made.duration < 5
    finally:
        s.close()
