"""The release PR moves the version everywhere `make check` reads it, and the release
publishes the build `image / build` tests. release-please's python type moves
pyproject.toml's version but not uv.lock, which records radio's own version too, so
`uv lock --check` would fail on the release PR without the extra file that moves
radio's entry there. release.yml smoke-tests with the same command as image.yml, so
nothing reaches the registry that the PR's check would not pass. Standard library
only; `make test` runs it."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def smoke(workflow: str) -> list[str]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    return re.findall(r"^\s+smoke:\s*(.+?)\s*$", text, re.M)


class ReleasePlease(unittest.TestCase):
    def setUp(self):
        self.config = load_json("release-please-config.json")
        self.package = self.config["packages"]["."]
        self.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_python_with_pre_major_patch_bumps(self):
        self.assertEqual(self.config["release-type"], "python")
        self.assertTrue(self.config["bump-minor-pre-major"])
        self.assertTrue(self.config["bump-patch-for-minor-pre-major"])
        self.assertEqual(self.package["package-name"], self.pyproject["project"]["name"])

    def test_uv_lock_moves_with_pyproject(self):
        name = self.pyproject["project"]["name"]
        self.assertIn(
            {
                "type": "toml",
                "path": "uv.lock",
                # release-please parses TOML into {value, start, end} nodes.
                "jsonpath": f"$.package[?(@.name.value==='{name}')].version",
            },
            self.package["extra-files"],
        )
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        ours = [p for p in lock["package"] if p["name"] == name]
        self.assertEqual(len(ours), 1, "uv.lock should hold radio once")

    def test_the_manifest_matches_pyproject(self):
        manifest = load_json(".release-please-manifest.json")
        self.assertEqual(manifest, {".": self.pyproject["project"]["version"]})


class Publish(unittest.TestCase):
    def test_release_smoke_tests_as_image_build_does(self):
        self.assertEqual(smoke("image.yml"), ["bash scripts/image_smoke.sh"])
        self.assertEqual(smoke("release.yml"), smoke("image.yml"))

    def test_release_publishes_the_build_it_tested(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        self.assertIn("digest: ${{ needs.image.outputs.digest }}", text)
        self.assertIn("artifact: ${{ needs.image.outputs.artifact }}", text)
        self.assertIn("release-workflow: release.yml", (WORKFLOWS / "release-pr.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
