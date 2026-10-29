"""Text to speech, sentence by sentence, with a sentence cache (paddock#57), ported
from v1's `speech.py` (v1.0.0).

Text is spoken sentence by sentence with `sentence_pause_secs` of silence between
them: Piper's own gap after a full stop is too short to hear, so "Lap two. one oh
four point six. four point three over best." ran together in v1. Each sentence is
cached on its own, so the fixed parts ("Lap two.", "Final lap.") are synthesised
once and only the numbers are new for each lap.

The text is plain text: what to say has already been decided and worded (#90's
phrasebook turns a callout into text, paddock#71), so nothing here reads a callout.

A `Voice` is the engine, Piper once radio can install it, and a fake in the tests.
The `Synthesizer` around it splits, caches and joins:

- `clips(text)` gives each sentence's clip as soon as it is ready, the seam for a
  sink that starts playing the first sentence while the rest are made, and
  `clips_async` does it off the event loop;
- `render(text)` gives one `Utterance`, the sentences joined with the pause, as v1
  plays them; `render_async` runs it off the event loop, on its own thread.

Text with nothing to say (no letters or digits, or audio the voice leaves empty)
gives no clip and leaves no file. The cache (`ClipCache`) is capped in bytes and
evicts the least recently used sentence. A clip of several sentences is joined into
a one-off WAV that the sink hands back with `release` once it has played; it is
never cached, as almost every lap's text is new. Every write goes to a temp file of
its own before an atomic rename, so two threads making the same sentence can't
corrupt a clip.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
import uuid
import wave
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("radio")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLIP_NAME = re.compile(r"^[0-9a-f]{40}\.wav$")
JOINED_PREFIX = "joined-"
# TWR_RADIO_CACHE_MAX_MB's default, as v1's GT7_CACHE_MAX_MB.
DEFAULT_CACHE_MAX_BYTES = 100 * 1024 * 1024
# A joined or temp file older than this was left by a crash. Younger ones may
# belong to another process sharing the directory, so start-up leaves them alone.
STALE_SECS = 600.0
# Said once at start, so the voice is loaded and warm before the first callout
# (v1's App._startup, #57 R7).
WARM_UP = "Race engineer online."


def split_sentences(text: str) -> list[str]:
    """Split on `.`, `!` or `?` followed by space. The text has no decimal points:
    numbers are words."""
    return [part for part in (p.strip() for p in _SENTENCE_END.split(text)) if part]


def speakable(text: str) -> bool:
    """Whether text has anything to say: a letter or a digit."""
    return any(ch.isalnum() for ch in text)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def voice_tag(model_path: Path) -> str:
    """Identifies the voice file as it is now: its path, size and modification time,
    and those of its `.json` config beside it, so replacing either one, even at the
    same path, gives new cache keys. Part of a Piper voice's `key`."""
    parts = []
    for path in (Path(model_path), Path(f"{model_path}.json")):
        try:
            st = path.stat()
        except FileNotFoundError:
            parts.append(f"{path}|missing")
            continue
        parts.append(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}")
    return _sha1("|".join(parts))[:12]


def _unique_tmp(out: Path) -> Path:
    return out.with_name(f"{out.stem}.{uuid.uuid4().hex}.tmp.wav")


class Voice(Protocol):
    """The engine that turns one sentence into audio: Piper's PiperVoice, loaded once,
    with the `voice:` settings."""

    @property
    def key(self) -> str:
        """The voice file and every setting that changes its audio: two voices, or one
        voice with two settings, give two keys, so they never share a clip."""
        ...

    def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
        """Write text's audio to wav, setting its parameters. Writing nothing, with no
        parameters set, means there is no audio for it."""
        ...


@dataclass(frozen=True)
class Utterance:
    """What `render` gives: the WAV file, how long it plays, and the text it says."""

    path: Path
    duration: float
    text: str


def duration(path: Path) -> float:
    """How long a WAV file plays, in seconds."""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class ClipCache:
    """A directory of sentence clips, capped at max_bytes with least recently used
    eviction. The index lives in memory, seeded from the files' modification times at
    start (a hit touches the file, so the order survives a restart). Safe to share
    between threads."""

    def __init__(self, cache_dir: Path, max_bytes: int = DEFAULT_CACHE_MAX_BYTES) -> None:
        self.dir = Path(cache_dir)
        self.max_bytes = max(int(max_bytes), 0)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_owner(self.dir)
        self._lock = threading.Lock()
        self._sizes: OrderedDict[Path, int] = OrderedDict()
        self.total_bytes = 0
        found = []
        stale_before = time.time() - STALE_SECS
        for path in self.dir.iterdir():
            try:
                st = path.stat()
            except FileNotFoundError:  # pragma: no cover - removed while listing
                continue
            if path.name.startswith(JOINED_PREFIX) or path.name.endswith(".tmp.wav"):
                if st.st_mtime < stale_before:
                    path.unlink(missing_ok=True)  # left by a crash: never read again
            elif _CLIP_NAME.match(path.name) and path.is_file():
                found.append((st.st_mtime_ns, path, st.st_size))
        for _, path, size in sorted(found):
            self._sizes[path] = size
            self.total_bytes += size
        self.trim()

    def path(self, key: str) -> Path:
        return self.dir / f"{_sha1(key)}.wav"

    def get(self, key: str) -> Path | None:
        """The cached clip for key, marked as just used, or None."""
        out = self.path(key)
        with self._lock:
            if not out.exists():
                self._forget(out)
                return None
            if out in self._sizes:
                self._sizes.move_to_end(out)
            else:  # written by another process sharing the directory
                self._add(out)
        try:
            os.utime(out)
        except FileNotFoundError:  # pragma: no cover - evicted by another process
            return None
        return out

    def put(self, tmp: Path, key: str) -> Path:
        """Move a finished temp file into the cache as key's clip."""
        out = self.path(key)
        with self._lock:
            os.replace(tmp, out)
            self._forget(out)
            self._add(out)
        return out

    def trim(self) -> None:
        """Evict least recently used clips until the cache is under its cap. The most
        recent clip is always kept, so the one just returned is still there."""
        with self._lock:
            while self.total_bytes > self.max_bytes and len(self._sizes) > 1:
                path, size = self._sizes.popitem(last=False)
                self.total_bytes -= size
                path.unlink(missing_ok=True)

    def one_off(self) -> Path:
        """A fresh path for a clip that is played once and then released."""
        return self.dir / f"{JOINED_PREFIX}{uuid.uuid4().hex}.wav"

    def release(self, clip: Path) -> None:
        if clip.parent == self.dir and clip.name.startswith(JOINED_PREFIX):
            clip.unlink(missing_ok=True)

    def _add(self, path: Path) -> None:
        size = path.stat().st_size
        self._sizes[path] = size
        self.total_bytes += size

    def _forget(self, path: Path) -> None:
        size = self._sizes.pop(path, None)
        if size is not None:
            self.total_bytes -= size


def _check_owner(path: Path) -> None:
    """Refuse a cache directory someone else owns: they could plant clips in it."""
    owner = path.stat().st_uid
    if owner != os.geteuid():
        raise PermissionError(
            f"speech cache {path} is owned by uid {owner}, not this user; set TWR_RADIO_CACHE_DIR"
        )


class Synthesizer:
    """A Voice, sentence by sentence, through a ClipCache, with pause_secs of silence
    between sentences."""

    def __init__(self, voice: Voice, cache: ClipCache, pause_secs: float) -> None:
        self.voice = voice
        self.cache = cache
        self.pause_secs = max(pause_secs, 0.0)
        # One thread: synthesis is CPU-bound, and one sentence at a time keeps a
        # callout's sentences in order.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synth")

    def clips(self, text: str) -> Iterator[Path]:
        """Each sentence's clip, in order, made or found as it is asked for: the first
        can play while the next is made. A sentence with nothing to say, or no audio,
        gives none. The cache is trimmed to its cap before each clip is given, so a
        clip is kept until the next one is asked for, and no longer if the cap is
        tight. It blocks while it synthesises: on the event loop, use clips_async."""
        for clip in self._clips(text):
            self.cache.trim()  # keeps the most recent: the clip about to be given
            yield clip

    async def clips_async(self, text: str) -> AsyncIterator[Path]:
        """clips, each sentence made on the synthesiser's own thread, never on the
        event loop."""
        loop = asyncio.get_running_loop()
        clips = self.clips(text)
        while (clip := await loop.run_in_executor(self._executor, next, clips, None)) is not None:
            yield clip

    def _clips(self, text: str) -> Iterator[Path]:
        for sentence in split_sentences(text):
            if speakable(sentence) and (clip := self._sentence(sentence)) is not None:
                yield clip

    def render(self, text: str) -> Utterance | None:
        """The whole text as one clip, its sentences joined with the pause, or None
        when there is nothing to say. Blocks while it synthesises: on the event loop,
        use render_async."""
        if not speakable(text):
            return None
        started = time.perf_counter()
        # Untrimmed until joined: a tight cap must not evict a part before the join.
        parts = list(self._clips(text))
        if not parts:
            out = None
        elif len(parts) == 1:
            out = parts[0]
        else:
            out = self.cache.one_off()
            join_wavs(parts, out, self.pause_secs)
        self.cache.trim()
        if out is None:
            return None
        log.debug(
            "rendered",
            extra={"text": text, "secs": round(time.perf_counter() - started, 3)},
        )
        return Utterance(out, duration(out), text)

    async def render_async(self, text: str) -> Utterance | None:
        """render on the synthesiser's own thread, never on the event loop."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, self.render, text)

    async def warm_up(self) -> None:
        """Say WARM_UP and throw it away, so the first callout is not late."""
        self.release(await self.render_async(WARM_UP))

    def release(self, utterance: Utterance | None) -> None:
        """utterance has played: a one-off clip is removed, a cached one kept, and None
        (nothing was said) is ignored."""
        if utterance is not None:
            self.cache.release(utterance.path)

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def _sentence(self, sentence: str) -> Path | None:
        """The clip for one sentence, from the cache or made now; None when the voice
        gives no audio for it."""
        key = f"{self.voice.key}|{sentence}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        tmp = _unique_tmp(self.cache.path(key))
        try:
            wav = wave.open(str(tmp), "wb")  # noqa: SIM115 - closed below, see except
            try:
                self.voice.synthesize_wav(sentence, wav)
            except BaseException:
                # Closing a WAV with no parameters raises too; keep the voice's error.
                with contextlib.suppress(wave.Error):
                    wav.close()
                raise
            wav.close()
        except wave.Error as err:
            # The voice wrote no audio, so the WAV header was never set.
            tmp.unlink(missing_ok=True)
            log.warning("no audio", extra={"text": sentence, "err": str(err)})
            return None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self.cache.put(tmp, key)


def join_wavs(parts: list[Path], out: Path, pause_secs: float) -> None:
    """Concatenate WAV files that share a format, with silence between them."""
    tmp = _unique_tmp(out)
    silence = b""
    try:
        with wave.open(str(tmp), "wb") as dst:
            for i, part in enumerate(parts):
                with wave.open(str(part), "rb") as src:
                    if i == 0:
                        dst.setparams(src.getparams())
                        frame_bytes = src.getsampwidth() * src.getnchannels()
                        silence = b"\0" * (int(src.getframerate() * pause_secs) * frame_bytes)
                    elif silence:
                        dst.writeframes(silence)
                    dst.writeframes(src.readframes(src.getnframes()))
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
