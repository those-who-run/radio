"""The voices the image carries (paddock#392): every voice in
src/radio/voices/SHA256SUMS is baked in and picked by name, so the Dockerfile sets
the directory and never a model path, which would win over TWR_RADIO_VOICE, and
each voice's model card and attribution ship with it. Standard library only;
`make test` runs it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def voices() -> list[str]:
    sums = (ROOT / "src" / "radio" / "voices" / "SHA256SUMS").read_text(encoding="utf-8")
    return [
        line.split()[1].removesuffix(".onnx")
        for line in sums.splitlines()
        if line.endswith(".onnx")
    ]


class Voices(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        # Without its comments, which name the setting the image must not set.
        self.instructions = re.sub(r"^\s*#.*$", "", self.dockerfile, flags=re.M)

    def test_there_are_two_voices(self):
        self.assertEqual(
            voices(), ["en_GB-alba-medium", "en_GB-northern_english_male-medium"]
        )

    def test_the_image_names_the_directory_and_no_model(self):
        self.assertIn("TWR_RADIO_VOICES_DIR=/app/voices", self.instructions)
        self.assertNotIn("TWR_RADIO_VOICE_MODEL", self.instructions)
        self.assertNotRegex(self.instructions, r"TWR_RADIO_VOICE=")

    def test_the_image_fetches_and_checks_every_voice_in_the_sums(self):
        self.assertIn("COPY src/radio/voices/SHA256SUMS /SHA256SUMS", self.instructions)
        self.assertIn("sha256sum -c --strict /SHA256SUMS", self.instructions)
        for voice in voices():
            self.assertNotIn(voice, self.instructions, "a voice named in the Dockerfile")

    def test_every_voice_has_its_model_card_and_its_notice(self):
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for voice in voices():
            with self.subTest(voice=voice):
                card = ROOT / "licenses" / f"{voice}.MODEL_CARD"
                self.assertTrue(card.is_file(), f"{card} is missing")
                self.assertIn(f"Piper's {voice}", notice)
                self.assertIn(f"licenses/{voice}.MODEL_CARD", notice)


if __name__ == "__main__":
    unittest.main()
