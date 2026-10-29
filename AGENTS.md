# Agents and contributors

`radio` is part of Those Who Run v2; [`README.md`](README.md) says what it is.

The rules for every v2 repository live once, in paddock's
[`AGENTS.md`](https://github.com/those-who-run/paddock/blob/main/AGENTS.md), and apply here in full, for
coding agents and for people. This file adds only what is specific to this
repository, and never repeats or weakens a rule from there.

## Issues

Issues are off here: every issue lives in
[those-who-run/paddock](https://github.com/those-who-run/paddock/issues). Read one with
`gh issue view N -R those-who-run/paddock --comments`. A PR here says
`Closes those-who-run/paddock#N.` only when every acceptance criterion is met,
and `Part of those-who-run/paddock#N` otherwise, as paddock's `AGENTS.md` says.

## Definition of done

`make check` passes. It runs what CI runs: the commit hooks on every file
(CI's `checks / checks`), the Python checks (CI's `python / check`) and
`make test` (CI's `test`): every internal Markdown link and anchor resolves,
the guard and settings in `.claude/` are `those-who-run/.github`'s `agent/`
at the commit CI pins, the external commit hooks are pinned by commit SHA,
the coverage floor is the same in its three places, and the tests of those
checks. It needs `uv`, `python3`, `pre-commit` and the network.

A change also carries a test for each acceptance criterion of its issue,
docs changed in the same PR as what they describe, and a recording that
pins a behaviour where one applies.

## Python

- The package is `radio`, in `src/radio/`, with its tests in `tests/unit/`
  (pytest). The tests beside them in `tests/` are the repository's own checks
  (unittest, standard library only), copied from paddock and `.github`; ruff
  and mypy leave them and `scripts/` alone. `scripts/v1_phrases.py` is radio's
  own: it writes the phrase goldens from v1.0.0's rules, run by hand.
- Python is the version in `.python-version`, 3.14 for now: Piper needs
  onnxruntime, which has no wheel for 3.15 yet
  ([microsoft/onnxruntime#33271](https://github.com/microsoft/onnxruntime/issues/33271)),
  and `renovate.json` holds Python below 3.15 until it has. uv manages the
  environment: dependencies are declared in `pyproject.toml` and locked in
  `uv.lock`, never installed by hand. After changing `pyproject.toml`, run
  `make lock` and commit both. `make venv` installs `.venv` from the lock.
- The checks, in `make python` and CI's `python / check` (`python.yml` in
  `those-who-run/.github`): `uv lock --check`, `ruff format --check`,
  `ruff check`, mypy in strict mode, and pytest under coverage with the floor
  (`COVERAGE_FLOOR`, 90). A skipped test, or an expected failure (`xfail`),
  fails the run. `make fmt` formats and fixes.
- Settings come from the environment, read once at start in
  `src/radio/settings.py`, and are all named `TWR_*`: the shared ones with the
  same name as in every component (`TWR_NATS_URL`, `TWR_HEALTH_ADDR`,
  `TWR_LOG_LEVEL`, `TWR_LOG_FORMAT`), radio's own as `TWR_RADIO_*`. A value
  radio can't read stops it at start. The README lists them.
- Logs are one line a record, `time`, `level` and `msg` first, as the Go
  components write them: logfmt by default, JSON in the image.

## Make targets

- `make check`: everything a PR must pass, as CI runs it.
- `make test`: the repository's own checks, CI's `test` job, while iterating.
- `make python`: the Python checks, CI's `python / check`; `make py-test` the
  package's tests alone.
- `make run`: radio on this machine, against `TWR_NATS_URL`.
- `make image`: the image, with podman.
- `make voice`: fetches the Piper voice into `voices/` and checks its sha256;
  `make voice-test` runs the synthesiser against it. Neither is in
  `make check`: CI has no model, and the tests use a fake voice.
- `make hooks`: installs the commit hooks. A new clone or worktree runs it
  once, so the hooks run on every commit and commit message.

## The guard

`.claude/settings.json` and `.claude/hooks/guard.py` are copies: a change to
them is made in `those-who-run/.github`, then copied here unchanged with the
bump of the `checks.yml` pin in `.github/workflows/ci.yml`.
`.claude/guard.json` holds this repository's own values.
