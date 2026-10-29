"""The estimate a simulated speaker uses without a voice, against the real voice
(paddock#60 AC5): opt in with `make voice-test`, as test_piper.py."""

import logging
import os
from pathlib import Path

import pytest

from radio.engineer import from_settings
from radio.piper_voice import Voices
from radio.settings import Settings
from radio.speech import estimate
from radio.voice import VoiceSettings

# Ten of v1's phrases (v1_phrases.json) left out of the estimate's fit.
HELD_OUT = [
    "Fuel is safe.",
    "Lap fifty. eight ten point six. fifty point eight over best.",
    "In this lap for fuel.",
    "Circuit de Spa Francorchamps.",
    "That is the in lap. eight ten point six.",
    "Willow Springs International Raceway. Big Willow. Twenty-five laps. "
    "P twenty-three of twenty-eight.",
    "Engine water is warm, watch it.",
    "Kyoto Driving Park. Yamagiwa and Miyabi.",
    "Race over. Final classification fifteenth. Fastest lap was five thirty-seven point five.",
    "Oil getting hot.",
]


def test_the_estimate_is_within_a_fifth_of_alba(tmp_path: Path) -> None:
    model = Path(os.environ["TWR_RADIO_VOICE_MODEL"])
    voice = VoiceSettings()
    settings = Settings(voice_model=model, voice=voice, cache_dir=tmp_path)
    voices = Voices(settings, logging.getLogger("radio"))
    s = voices.get(from_settings(settings))
    try:
        for text in HELD_OUT:
            u = s.render(text)
            assert u is not None
            guess = estimate(text, voice.length_scale, voice.sentence_pause_secs)
            print(f"{u.duration:5.2f} s, estimated {guess:5.2f} s: {text}")
            assert guess == pytest.approx(u.duration, rel=0.2), text
    finally:
        voices.close()
