"""The synthesiser with a fake voice: v1's tests/test_speech.py, ported, and #57's
AC2, AC3 and AC4."""

import os
import threading
import time
import wave
from pathlib import Path

import pytest
from helpers import RATE, FakeVoice, frames, write_wav

from radio.speech import (
    DEFAULT_CACHE_MAX_BYTES,
    STALE_SECS,
    WARM_UP,
    ClipCache,
    Synthesizer,
    join_wavs,
    speakable,
    split_sentences,
    voice_tag,
)

ONE = b"\x01\x00"
SILENT = b"\x00\x00"


def synth(
    tmp_path: Path,
    pause: float = 0.1,
    max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
    voice: FakeVoice | None = None,
) -> Synthesizer:
    return Synthesizer(voice or FakeVoice(), ClipCache(tmp_path, max_bytes), pause)


def calls(s: Synthesizer) -> list[str]:
    assert isinstance(s.voice, FakeVoice)
    return s.voice.calls


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.iterdir())


def test_split_sentences() -> None:
    assert split_sentences("Lap two. one oh four point six. four point three over best.") == [
        "Lap two.",
        "one oh four point six.",
        "four point three over best.",
    ]
    assert split_sentences("Final lap.") == ["Final lap."]
    assert split_sentences("Box now! Really?  Yes") == ["Box now!", "Really?", "Yes"]


def test_speakable() -> None:
    assert speakable("Box.") and speakable("4")
    assert not speakable(" .!? ")


def test_join_wavs_puts_silence_between_sentences(tmp_path: Path) -> None:
    a, b = write_wav(tmp_path / "a.wav", 10), write_wav(tmp_path / "b.wav", 20)
    join_wavs([a, b], tmp_path / "out.wav", 0.35)
    assert frames(tmp_path / "out.wav") == ONE * 10 + SILENT * 350 + ONE * 20


def test_ac2_two_cached_sentences_with_the_pause_and_only_the_new_one_made(
    tmp_path: Path,
) -> None:
    s = synth(tmp_path, pause=0.35)
    first = s.render("Lap two. One oh four point six.")
    assert first is not None
    assert frames(first.path) == ONE * 8 + SILENT * 350 + ONE * 22
    assert first.duration == pytest.approx((8 + 350 + 22) / RATE)
    assert first.text == "Lap two. One oh four point six."
    assert len([f for f in tmp_path.iterdir() if not f.name.startswith("joined-")]) == 2
    s.render("Lap three. One oh four point six.")
    assert calls(s) == ["Lap two.", "One oh four point six.", "Lap three."]


def test_each_sentence_is_synthesised_once_and_cached(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.render("Lap two. one oh four point six.")
    s.render("Lap two. one oh five point one.")
    s.render("Lap two. one oh four point six.")
    assert calls(s) == ["Lap two.", "one oh four point six.", "one oh five point one."]


def test_clips_come_one_sentence_at_a_time(tmp_path: Path) -> None:
    """The streaming seam: the first clip is ready before the second is made."""
    s = synth(tmp_path)
    clips = s.clips("Lap two. Box. .")
    first = next(clips)
    assert frames(first) == ONE * 8
    assert calls(s) == ["Lap two."]
    assert [frames(c) for c in clips] == [ONE * 4]
    assert calls(s) == ["Lap two.", "Box."]


def test_a_single_sentence_is_its_own_wav(tmp_path: Path) -> None:
    s = synth(tmp_path)
    u = s.render("Final lap.")
    assert u is not None and frames(u.path) == ONE * 10
    assert calls(s) == ["Final lap."]


class SilentVoice(FakeVoice):
    """Piper on text it has no phonemes for: writes nothing, sets no parameters."""

    def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
        self.calls.append(text)


def test_ac3_text_with_nothing_to_say_gives_no_utterance_and_no_file(tmp_path: Path) -> None:
    s = synth(tmp_path)
    for text in ("", ".", " . ! ", "..."):
        assert s.render(text) is None
    assert calls(s) == []
    assert list(tmp_path.iterdir()) == []


def test_empty_audio_from_the_voice_leaves_the_cache_clean(tmp_path: Path) -> None:
    s = synth(tmp_path, voice=SilentVoice())
    assert s.render("Hmm.") is None
    assert s.render("Hmm. Hmm again.") is None
    assert list(tmp_path.iterdir()) == []


def test_a_failing_voice_leaves_no_temp_file(tmp_path: Path) -> None:
    class Broken(FakeVoice):
        def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
            raise RuntimeError("onnx")

    s = synth(tmp_path, voice=Broken())
    with pytest.raises(RuntimeError):
        s.render("Final lap.")
    assert list(tmp_path.iterdir()) == []


def test_a_voice_failing_after_its_header_leaves_no_temp_file(tmp_path: Path) -> None:
    class HalfWay(FakeVoice):
        def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
            super().synthesize_wav(text, wav)
            raise RuntimeError("onnx")

    with pytest.raises(RuntimeError):
        synth(tmp_path, voice=HalfWay()).render("Final lap.")
    assert list(tmp_path.iterdir()) == []


def test_unspeakable_sentences_are_dropped(tmp_path: Path) -> None:
    s = synth(tmp_path)
    u = s.render("Final lap. .")
    assert u is not None and frames(u.path) == ONE * 10
    assert calls(s) == ["Final lap."]


def test_a_joined_utterance_is_removed_once_played_and_its_sentences_kept(
    tmp_path: Path,
) -> None:
    s = synth(tmp_path)
    joined = s.render("Lap two. one oh four point six.")
    assert joined is not None and joined.path.exists()
    s.release(joined)
    assert not joined.path.exists()
    assert len(list(tmp_path.iterdir())) == 2, "one clip per sentence"
    single = s.render("Final lap.")
    s.release(single)
    assert single is not None and single.path.exists(), "a cached sentence survives release"


def test_a_hundred_laps_stay_under_the_cap(tmp_path: Path) -> None:
    cap = 4000
    s = synth(tmp_path, max_bytes=cap)
    for lap in range(1, 101):
        u = s.render(f"Lap {lap}. one oh {lap * 7}. one over best.")
        assert u is not None and u.path.exists()
        s.release(u)
        assert dir_bytes(tmp_path) <= cap, lap
    assert s.cache.total_bytes == dir_bytes(tmp_path)
    assert all(f.name.endswith(".wav") and "-" not in f.name for f in tmp_path.iterdir())


def test_eviction_is_least_recently_used(tmp_path: Path) -> None:
    clip = 44 + 2 * len("Lap one.")  # the header plus one frame a character
    s = synth(tmp_path, max_bytes=3 * clip)
    for text in ("Lap one.", "Lap two.", "Lap six."):
        s.render(text)
    s.render("Lap one.")  # a hit: now the most recent
    s.render("Lap ten.")  # over the cap: "Lap two." is the oldest
    made = len(calls(s))
    s.render("Lap one.")
    s.render("Lap six.")
    assert len(calls(s)) == made, "kept"
    s.render("Lap two.")
    assert calls(s)[-1] == "Lap two.", "evicted, so made again"


def test_the_cache_reopens_with_its_clips_and_drops_stale_leftovers(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.render("Final lap.")
    joined = s.render("Lap two. Box.")  # never released: a crash mid-playback
    assert joined is not None
    tmp = tmp_path / "abc.1234.tmp.wav"
    tmp.write_bytes(b"half")
    (tmp_path / "notes.txt").write_text("not a clip")
    old = time.time() - STALE_SECS - 60
    for f in (joined.path, tmp):
        os.utime(f, (old, old))
    again = synth(tmp_path)
    clips = sorted(f.name for f in tmp_path.iterdir() if f.suffix == ".wav")
    assert len(clips) == 3 and all(len(n) == 44 for n in clips), clips
    assert again.cache.total_bytes == dir_bytes(tmp_path) - len("not a clip")
    again.render("Final lap.")
    assert calls(again) == []


def test_a_clip_written_by_another_process_is_found(tmp_path: Path) -> None:
    s = synth(tmp_path)
    other = synth(tmp_path)
    other.render("Final lap.")
    s.render("Final lap.")
    assert calls(s) == []
    assert s.cache.total_bytes == dir_bytes(tmp_path)


def test_a_clip_removed_behind_the_cache_is_made_again(tmp_path: Path) -> None:
    s = synth(tmp_path)
    u = s.render("Final lap.")
    assert u is not None
    u.path.unlink()
    s.render("Final lap.")
    assert calls(s) == ["Final lap.", "Final lap."]
    assert s.cache.total_bytes == dir_bytes(tmp_path)


def test_ac4_two_voices_give_two_keys_for_one_sentence(tmp_path: Path) -> None:
    alba = synth(tmp_path, voice=FakeVoice("alba"))
    other = synth(tmp_path, voice=FakeVoice("other"))
    a, b = alba.render("Box."), other.render("Box.")
    assert a is not None and b is not None and a.path != b.path
    assert calls(other) == ["Box."]


def test_touching_the_voice_file_changes_its_tag(tmp_path: Path) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"onnx")
    before = voice_tag(model)
    assert voice_tag(model) == before
    st = model.stat()
    os.utime(model, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert voice_tag(model) != before
    touched = voice_tag(model)
    (tmp_path / "voice.onnx.json").write_text("{}")
    assert voice_tag(model) != touched, "the voice config counts too"


def test_two_threads_making_the_same_sentence_both_get_a_valid_wav(tmp_path: Path) -> None:
    both_inside = threading.Barrier(2, timeout=5)

    class SlowVoice(FakeVoice):
        def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            both_inside.wait()  # both threads are writing the same sentence now
            for _ in text:
                wav.writeframes(ONE)
                time.sleep(0.001)

    s = synth(tmp_path, voice=SlowVoice())
    results: list[Path] = []

    def run() -> None:
        u = s.render("Final lap.")
        assert u is not None
        results.append(u.path)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2
    for clip in results:
        assert frames(clip) == ONE * 10
    assert [f.name for f in tmp_path.iterdir()] == [results[0].name]


def test_a_cache_dir_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(PermissionError, match="TWR_RADIO_CACHE_DIR"):
        ClipCache(tmp_path / "cache")


def test_reopening_leaves_another_process_s_live_files_alone(tmp_path: Path) -> None:
    running = synth(tmp_path)
    joined = running.render("Lap two. Box.")
    tmp = tmp_path / "abc.1234.tmp.wav"
    tmp.write_bytes(b"being written")
    synth(tmp_path)
    assert joined is not None and joined.path.exists() and tmp.exists()


def test_a_silent_sentence_is_left_out_of_the_join(tmp_path: Path) -> None:
    class PartlySilent(FakeVoice):
        def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
            if text != "Hmm.":
                super().synthesize_wav(text, wav)

    s = synth(tmp_path, voice=PartlySilent())
    u = s.render("Final lap. Hmm.")
    assert u is not None and frames(u.path) == ONE * 10, "one sentence left: no join"
    s.release(u)
    assert u.path.exists()
    u = s.render("Lap two. Hmm. Box.")
    assert u is not None and frames(u.path) == ONE * 8 + SILENT * 100 + ONE * 4
    s.release(u)
    assert not u.path.exists()


def test_release_of_nothing_is_a_no_op(tmp_path: Path) -> None:
    s = synth(tmp_path)
    s.release(None)
    s.release(s.render("."))


async def test_render_async_runs_off_the_event_loop(tmp_path: Path) -> None:
    seen: list[threading.Thread] = []

    class Where(FakeVoice):
        def synthesize_wav(self, text: str, wav: wave.Wave_write) -> None:
            seen.append(threading.current_thread())
            super().synthesize_wav(text, wav)

    s = synth(tmp_path, voice=Where())
    try:
        u = await s.render_async("Box.")
        assert u is not None and frames(u.path) == ONE * 4
        assert seen and seen[0] is not threading.main_thread()
    finally:
        s.close()


async def test_warm_up_says_its_line_and_keeps_nothing_but_the_cache(tmp_path: Path) -> None:
    s = synth(tmp_path)
    try:
        await s.warm_up()
    finally:
        s.close()
    assert calls(s) == split_sentences(WARM_UP)
    assert not [f for f in tmp_path.iterdir() if f.name.startswith("joined-")]
