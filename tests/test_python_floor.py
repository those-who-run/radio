"""The coverage floor is one number in three places: the coverage-floor ci.yml passes
to python.yml (CI's `python / check`), COVERAGE_FLOOR in the Makefile (`make
check`) and fail_under in pyproject.toml. A floor lowered in one of them alone would
let the other two drift apart. Standard library only; `make test` runs it."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def floors() -> dict[str, float]:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found = {"pyproject.toml": float(pyproject["tool"]["coverage"]["report"]["fail_under"])}
    if m := re.search(r"^\s+coverage-floor:\s*([\d.]+)\s*$", ci, re.M):
        found["ci.yml"] = float(m.group(1))
    if m := re.search(r"^COVERAGE_FLOOR\s*:?=\s*([\d.]+)\s*$", make, re.M):
        found["Makefile"] = float(m.group(1))
    return found


class Floor(unittest.TestCase):
    def test_one_floor_everywhere(self):
        found = floors()
        self.assertEqual(set(found), {"ci.yml", "Makefile", "pyproject.toml"}, found)
        self.assertEqual(len(set(found.values())), 1, found)


if __name__ == "__main__":
    unittest.main()
