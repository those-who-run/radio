"""The voice settings file and the model's sha256 check (#57 R4, R6)."""

import hashlib
from pathlib import Path

import pytest

from radio import voice
from radio.voice import VoiceError, VoiceSettings, check_model, load, sums

V1_RULES = """\
voice:
  length_scale: 0.9     # < 1 speaks faster
  # noise_scale: 0.5     # variation in the voice
  # noise_w_scale: 0.5   # variation in the timing
  sentence_pause_secs: 0.35   # silence between sentences
queue:
  max_age_seconds: 8
rules:
  lap_time:
    enabled: true
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "voice.yaml"
    path.write_text(text)
    return path


def test_defaults_are_v1s() -> None:
    """v1's DEFAULT_RULES["voice"] (config.py at v1.0.0), merged under every rules
    file; Piper's own defaults from alba's .onnx.json (0.667, 0.8) never applied."""
    assert VoiceSettings() == VoiceSettings(None, 0.9, 0.5, 0.5, 0.35)


def test_v1s_example_rules_file_reads_as_v1_tuned_it(tmp_path: Path) -> None:
    """Its noise keys are commented out, so v1's defaults filled them."""
    assert load(write(tmp_path, V1_RULES)) == VoiceSettings(
        speaker_id=None,
        length_scale=0.9,
        noise_scale=0.5,
        noise_w_scale=0.5,
        sentence_pause_secs=0.35,
    )


def test_every_key(tmp_path: Path) -> None:
    got = load(
        write(
            tmp_path,
            "voice:\n  speaker_id: 2\n  length_scale: 1\n  noise_scale: 0.5\n"
            "  noise_w_scale: 0.25\n  sentence_pause_secs: 0\n",
        )
    )
    assert got == VoiceSettings(2, 1.0, 0.5, 0.25, 0.0)


def test_a_null_leaves_the_setting_to_the_model(tmp_path: Path) -> None:
    got = load(write(tmp_path, "voice:\n  length_scale:\n  noise_scale:\n"))
    assert got.length_scale is None and got.noise_scale is None
    assert got.noise_w_scale == 0.5


@pytest.mark.parametrize("text", ["", "rules: {}\n", "voice:\n"])
def test_no_voice_section_is_the_defaults(tmp_path: Path, text: str) -> None:
    assert load(write(tmp_path, text)) == VoiceSettings()


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("voice:\n  lenght_scale: 1\n", "unknown keys lenght_scale"),
        ("voice:\n  length_scale: fast\n", "length_scale"),
        ("voice:\n  length_scale: -1\n", "length_scale"),
        ("voice:\n  length_scale: true\n", "length_scale"),
        ("voice:\n  sentence_pause_secs:\n", "sentence_pause_secs"),
        ("voice:\n  speaker_id: 1.5\n", "speaker_id"),
        ("voice: [1]\n", "voice: is not a mapping"),
        ("- 1\n", "not a mapping"),
        ("voice: {\n", "voice.yaml"),
    ],
)
def test_a_file_radio_cant_read_is_an_error(tmp_path: Path, text: str, want: str) -> None:
    with pytest.raises(VoiceError, match=want):
        load(write(tmp_path, text))


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(VoiceError, match="nope.yaml"):
        load(tmp_path / "nope.yaml")


def test_the_synthesis_key_changes_with_each_setting_but_the_pause() -> None:
    base = VoiceSettings()
    others = [
        VoiceSettings(speaker_id=1),
        VoiceSettings(length_scale=1.0),
        VoiceSettings(noise_scale=0.6),
        VoiceSettings(noise_w_scale=0.6),
    ]
    assert len({base.synthesis_key(), *(o.synthesis_key() for o in others)}) == 5
    assert VoiceSettings(sentence_pause_secs=1).synthesis_key() == base.synthesis_key()


def test_the_sums_are_v1s() -> None:
    assert sums() == {
        "en_GB-alba-medium.onnx": (
            "401369c4a81d09fdd86c32c5c864440811dbdcc66466cde2d64f7133a66ad03b"
        ),
        "en_GB-alba-medium.onnx.json": (
            "aa965a2f02ecced632c2694e1fc72bbff6d65f265fab567ca945918c73dd89f4"
        ),
    }


def fake_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_body: bytes) -> Path:
    """A model and config whose sums radio "ships", the config's from json_body."""
    model = tmp_path / "en_GB-alba-medium.onnx"
    model.write_bytes(b"onnx")
    Path(f"{model}.json").write_bytes(b"{}")
    known = {
        model.name: hashlib.sha256(b"onnx").hexdigest(),
        f"{model.name}.json": hashlib.sha256(json_body).hexdigest(),
    }
    monkeypatch.setattr(voice, "sums", lambda: known)
    return model


def test_a_model_that_matches_its_sums_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert check_model(fake_model(tmp_path, monkeypatch, b"{}")) is True


def test_a_model_that_doesnt_match_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(VoiceError, match=r"onnx.json has sha256"):
        check_model(fake_model(tmp_path, monkeypatch, b"{ }"))


def test_a_known_model_that_is_missing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VoiceError, match="make voice"):
        check_model(tmp_path / "en_GB-alba-medium.onnx")


def test_a_voice_radio_has_no_sums_for_is_not_checked(tmp_path: Path) -> None:
    assert check_model(tmp_path / "my-voice.onnx") is False
