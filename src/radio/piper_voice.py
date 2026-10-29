"""Piper behind the `Voice` interface, and each engineer's synthesiser
(paddock#57, #60).

Piper is loaded here and nowhere else, so everything else is tested with a fake
voice and CI needs no model. `make voice-test` runs the one test that loads the real
model.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from collections.abc import Callable
from pathlib import Path

from piper import PiperVoice as _Piper
from piper import SynthesisConfig

from radio.engineer import Engineer
from radio.settings import Settings
from radio.speech import ClipCache, Synthesizer, Utterance, Voice, voice_tag
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
    engineer: Engineer,
    cache: ClipCache,
    logger: logging.Logger,
    load: Callable[[Path, VoiceSettings], Voice] = PiperVoice,
) -> Synthesizer:
    """engineer's voice, checked against radio's sums and loaded, behind the sentence
    cache. A voice radio has no sums for, a path set by hand, is loaded with a
    warning; one that fails its sum raises VoiceError."""
    model = engineer.voice_model
    if not check_model(model):
        logger.warning("voice model not checked", extra={"model": str(model)})
    logger.info("loading voice", extra={"engineer": engineer.name, "model": str(model)})
    voice = load(model, engineer.voice)
    return Synthesizer(voice, cache, engineer.voice.sentence_pause_secs)


class Voices:
    """Each engineer's synthesiser, loaded the first time it is asked for, all over
    one sentence cache: a clip's key names its voice and settings, so two voices
    never share one."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        load: Callable[[Path, VoiceSettings], Voice] = PiperVoice,
    ) -> None:
        self.cache = ClipCache(settings.cache_dir, settings.cache_max_mb * 1024 * 1024)
        self.logger = logger
        self._load = load
        self._synths: dict[str, Synthesizer] = {}

    def get(self, engineer: Engineer) -> Synthesizer:
        synth = self._synths.get(engineer.name)
        if synth is None:
            synth = open_synthesizer(engineer, self.cache, self.logger, self._load)
            self._synths[engineer.name] = synth
        return synth

    async def load(self, engineer: Engineer) -> Synthesizer:
        """get, on a thread: checking and loading a model takes a second or more,
        too long for the event loop. radio loads every engineer's at start, so
        get never loads one while callouts are spoken."""
        synth = await asyncio.to_thread(self.get, engineer)
        await synth.warm_up()
        return synth

    def release(self, utterance: Utterance | None) -> None:
        """utterance has played: a one-off clip is removed, a cached one kept."""
        if utterance is not None:
            self.cache.release(utterance.path)

    def close(self) -> None:
        for synth in self._synths.values():
            synth.close()
        self._synths.clear()
