"""Piper behind the `Voice` interface, and the synthesiser radio speaks with
(paddock#57).

Piper is loaded here and nowhere else, so everything else is tested with a fake
voice and CI needs no model. `make voice-test` runs the one test that loads the real
model.
"""

from __future__ import annotations

import logging
import wave
from collections.abc import Callable
from pathlib import Path

from piper import PiperVoice as _Piper
from piper import SynthesisConfig

from radio.settings import Settings
from radio.speech import ClipCache, Synthesizer, Voice, voice_tag
from radio.voice import VoiceSettings, check_model


class PiperVoice:
    """A Piper voice file, loaded once, with the `voice:` settings. Synthesis is
    CPU-bound and blocks: the Synthesizer runs it on its own thread."""

    def __init__(self, model: Path, settings: VoiceSettings) -> None:
        self._voice = _Piper.load(model)
        self._syn = SynthesisConfig(
            speaker_id=settings.speaker_id,
            length_scale=settings.length_scale,
            noise_scale=settings.noise_scale,
            noise_w_scale=settings.noise_w_scale,
        )
        self._key = f"{voice_tag(model)}|{settings.synthesis_key()}"

    @property
    def key(self) -> str:
        return self._key

    def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
        self._voice.synthesize_wav(text, wav, syn_config=self._syn)


def open_synthesizer(
    settings: Settings,
    logger: logging.Logger,
    load: Callable[[Path, VoiceSettings], Voice] = PiperVoice,
) -> Synthesizer:
    """The voice in settings, checked against radio's sums and loaded, behind a
    sentence cache. A voice radio has no sums for, a path set by hand, is loaded with
    a warning; one that fails its sum raises VoiceError."""
    if not check_model(settings.voice_model):
        logger.warning("voice model not checked", extra={"model": str(settings.voice_model)})
    logger.info("loading voice", extra={"model": str(settings.voice_model)})
    voice = load(settings.voice_model, settings.voice)
    cache = ClipCache(settings.cache_dir, settings.cache_max_mb * 1024 * 1024)
    return Synthesizer(voice, cache, settings.voice.sentence_pause_secs)
